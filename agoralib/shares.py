"""
自動挑選接手方與用量紀錄。

- 挑選（assign 沒指定 --to、額度不足的交接提醒）：在勾選、可用、分配比例 > 0 的 AI 中，挑「這次等級對應模型的額度池」
  剩餘 % 最高的一方（Antigravity 的 Argon 與 Flash 是不同額度池，見 quota.usage_for）。不同 AI 的百分比不等值，這是經驗法則；
  額度低於門檻、被標記無法使用、額度未知的不挑。
- 分配比例（.agora/config.json 的 parties.<AI>.share）：0 表示不自動分派給它；其他值只當面板上的 token 用量參考，不影響挑選。
- 紀錄（.agora/work_log.jsonl，每行一筆，都有 party 與 time）：
  - 接手輪數：{"party", "thread", "time"}（沒有 kind，或 kind 為 round）
  - 每次呼叫 AI 的用量：kind=call，見 record_call
  - 分派決策：kind=dispatch，見 cli.cmd_assign
"""

import json
import os
import time
import uuid
from pathlib import Path
from typing import Dict, Iterable, Optional

from agoralib import quota

SHARE_DAYS = float(os.getenv("AGORA_SHARE_DAYS", "7"))
ACTIVE_RELAY = ("starting", "running", "waiting")
TOKEN_FIELDS = ("input_tokens", "output_tokens", "cached_tokens", "total_tokens")


def log_path(root: Path) -> Path:
    return root / ".agora" / "work_log.jsonl"


def append(root: Path, entry: dict):
    log_path(root).parent.mkdir(parents=True, exist_ok=True)
    with open(log_path(root), "a", encoding="utf-8") as f:
        f.write(json.dumps({"time": time.time(), **entry}, ensure_ascii=False) + "\n")


def record(root: Path, party: str, thread: str):
    """接手方跑完一輪"""
    append(root, {"party": party, "thread": thread})


def record_call(root: Path, party: str, thread: str, kind: str, stats: Optional[dict], ok: bool) -> dict:
    """一次呼叫 AI 的用量（stats 是 parties 的 _stats；成功與失敗都記，重試是另一筆）；回傳寫入的紀錄"""
    stats = stats or {}
    entry = {"kind": "call", "call_id": uuid.uuid4().hex, "party": party, "thread": thread, "call_kind": kind,
             "ok": ok, "model": stats.get("model"), **{f: stats.get(f) for f in TOKEN_FIELDS},
             "cached_in_input": stats.get("cached_in_input"), "includes_subagents": stats.get("includes_subagents"),
             "usage_source": stats.get("usage_source"),
             "complete": bool(stats.get("complete")), "subagents": stats.get("subagents")}
    try:
        append(root, entry)
    except OSError:
        pass
    return entry


def _entries(root: Path, since: float):
    try:
        lines = log_path(root).read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict) and entry.get("party") and entry.get("time", 0) >= since:
            yield entry


def recent_work(root: Path, days: float = SHARE_DAYS, include_active: bool = True) -> Dict[str, int]:
    """最近 days 天各 AI 的接手輪數（include_active：進行中的分派各加 1 輪）"""
    work: Dict[str, int] = {}
    for entry in _entries(root, time.time() - days * 86400):
        if entry.get("kind", "round") == "round":
            work[entry["party"]] = work.get(entry["party"], 0) + 1
    if include_active:
        for state_file in (root / ".agora" / "threads").glob("*/state.json"):
            try:
                relay = json.loads(state_file.read_text(encoding="utf-8")).get("relay") or {}
            except Exception:
                continue
            if relay.get("status") in ACTIVE_RELAY and relay.get("worker"):
                work[relay["worker"]] = work.get(relay["worker"], 0) + 1
    return work


def call_tokens(entry: dict) -> Optional[int]:
    """一次呼叫的 token：有 total 用 total；否則 input＋output（cached 已含在 input 內或不知道時不另加，避免重複計算）"""
    if isinstance(entry.get("total_tokens"), int):
        return entry["total_tokens"]
    if isinstance(entry.get("input_tokens"), int) and isinstance(entry.get("output_tokens"), int):
        cached = entry.get("cached_tokens") if entry.get("cached_in_input") is False else 0
        return entry["input_tokens"] + entry["output_tokens"] + (cached if isinstance(cached, int) else 0)
    return None


def recent_usage(root: Path, parties: Iterable[str], days: float = SHARE_DAYS) -> Dict[str, dict]:
    """最近 days 天各 AI 的用量摘要（面板與 agora parties 顯示）"""
    out = {p: {"tokens_7d": 0, "calls_7d": 0, "calls_missing_tokens": 0, "rounds_7d": 0, "dispatch_auto": 0,
               "dispatch_explicit": 0, "subagents_started_7d": 0, "subagents_unobserved": 0} for p in parties}
    for entry in _entries(root, time.time() - days * 86400):
        u = out.get(entry["party"])
        if u is None:
            continue
        kind = entry.get("kind", "round")
        if kind == "round":
            u["rounds_7d"] += 1
        elif kind == "dispatch":
            u["dispatch_auto" if entry.get("selection") == "auto" else "dispatch_explicit"] += 1
        elif kind == "call":
            u["calls_7d"] += 1
            tokens = call_tokens(entry)
            if tokens is None:
                u["calls_missing_tokens"] += 1
            else:
                u["tokens_7d"] += tokens
            sub = entry.get("subagents")
            if sub is None:
                u["subagents_unobserved"] += 1
            else:
                u["subagents_started_7d"] += int(sub.get("started") or 0)
    return out


def pick(candidates: Iterable[str], shares: Dict[str, int], work: Dict[str, int],
         usages: Dict[str, Optional[dict]]) -> Optional[str]:
    """在 candidates 中挑額度剩餘 % 最高的一方（usages 應是這次要用的模型所屬的額度池，見 quota.usage_for）；
    分配比例為 0、額度低於門檻、被標記無法使用（blocked）或額度未知的不挑，沒有人可挑時回傳 None。
    work 保留在介面上（舊呼叫者），不影響挑選"""
    eligible = [p for p in candidates
                if shares.get(p, 1) > 0 and usages.get(p) is not None and not quota.unavailable(usages.get(p))]
    if not eligible:
        return None
    return max(eligible, key=lambda p: usages[p].get("remaining_pct", 0))
