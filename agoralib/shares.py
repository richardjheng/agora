"""
自動挑選接手方與用量紀錄。

- 挑選：額度只篩選資格；合格 AI 按 share 平滑加權輪詢。share 是成功自動派工件數的權重，不保證 token 比例。
- 專案鎖與 dispatch.json 保存輪詢及交易日誌；同識別碼重試不重計，候選集合或權重改變即重置。
- 紀錄（.agora/work_log.jsonl，每行一筆，都有 party 與 time）：
  - 接手輪數：{"party", "thread", "time"}（沒有 kind，或 kind 為 round）
  - 每次呼叫 AI 的用量：kind=call，見 record_call
  - 分派決策：kind=dispatch，見 cli.cmd_assign
"""

import json
import os
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterable, Optional

from agoralib import quota

SHARE_DAYS = float(os.getenv("AGORA_SHARE_DAYS", "7"))
ACTIVE_RELAY = ("starting", "running", "waiting")
TOKEN_FIELDS = ("input_tokens", "output_tokens", "cached_tokens", "total_tokens")


def dispatch_path(root: Path) -> Path:
    return root / ".agora" / "dispatch.json"


@contextmanager
def dispatch_lock(root: Path, wait: float = 120):
    """跨討論串／程序的專案鎖。OS 在程序退出時釋放，不能以檔案年齡搶走活鎖。"""
    path = root / ".agora" / "dispatch.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+b") as f:
        if f.seek(0, os.SEEK_END) == 0:
            f.write(b"0")
            f.flush()
        deadline = time.monotonic() + wait
        while True:
            try:
                f.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("此專案正建立其他分派，請稍後重試。")
                time.sleep(0.05)
        try:
            yield
        finally:
            f.seek(0)
            if os.name == "nt":
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(f, fcntl.LOCK_UN)


def dispatch_state(root: Path) -> dict:
    try:
        data = json.loads(dispatch_path(root).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"schedule": {}, "dispatches": {}}
    if not isinstance(data, dict) or not isinstance(data.get("dispatches"), dict):
        raise ValueError("派工日誌格式錯誤，請先修復 .agora/dispatch.json。")
    return data


def save_dispatch_state(root: Path, data: dict):
    """呼叫者持有 dispatch_lock；先同步暫存檔，再原子替換。"""
    path = dispatch_path(root)
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)


def quota_info(usage: Optional[dict]) -> dict:
    """派工共用的額度判定：過期沿用已知值，重置時間已過則恢復；封鎖仍優先。"""
    u = usage or {}
    pct = u.get("remaining_pct")
    known = isinstance(pct, (int, float))
    now = time.time()
    reset = known and bool(u.get("resets_at") and u["resets_at"] <= now)
    stale = known and bool(u.get("updated_at") and quota.is_stale(u, quota.wanted_max_age(u)))
    info = {"remaining_pct": pct, "effective_remaining_pct": 100 if reset else pct,
            "resets_at": u.get("resets_at"), "updated_at": u.get("updated_at"),
            "reset_elapsed": reset, "is_stale": stale}
    if stale:
        info["age_minutes"] = max(0, int((now - u["updated_at"]) / 60))
        info["quota_note"] = f"額度資料過期（{info['age_minutes']} 分鐘前）"
    if u.get("blocked"):
        info["excluded"] = f"被封鎖（{u['blocked'].get('reason') or '帳務或權限問題'}）"
    elif not known:
        info["excluded"] = "額度未知（沒有剩餘額度資料）"
    elif info["effective_remaining_pct"] < quota.THRESHOLD_PCT:
        info["excluded"] = f"額度不足（剩 {pct:g}%，低於 {quota.THRESHOLD_PCT:g}%）"
    return info


def eligible(candidates: Iterable[str], weights: dict, usages: dict) -> list:
    """額度只篩選資格；過期有值仍判斷，恰等於門檻合格。"""
    return sorted({p for p in candidates if weights.get(p, 1) > 0
                   and not quota_info(usages.get(p)).get("excluded")})


def schedule_next(candidates: Iterable[str], weights: dict, usages: dict, schedule: dict) -> tuple:
    parties = eligible(candidates, weights, usages)
    signature = [[p, weights.get(p, 1)] for p in parties]
    current = schedule.get("current", {}) if schedule.get("signature") == signature else {}
    current = {p: current.get(p, 0) + weights.get(p, 1) for p in parties}
    if not parties:
        return None, {"signature": [], "current": {}}
    worker = max(parties, key=lambda p: current[p])
    current[worker] -= sum(weights.get(p, 1) for p in parties)
    return worker, {"signature": signature, "current": current}


def recover_dispatch(root: Path) -> dict:
    """持有專案鎖時恢復中斷的交易。成功標記先存 relay，才提交輪詢與計件。
    未標記的背景程序必須退出（cli.cmd_work 的啟動閘門），故不會執行未計件的工作。"""
    data = dispatch_state(root)
    pending = data.get("pending")
    if pending:
        entry = pending["entry"]
        state_file = root / ".agora" / "threads" / entry["thread"] / "state.json"
        try:
            relay = json.loads(state_file.read_text(encoding="utf-8")).get("relay") or {}
        except FileNotFoundError:
            relay = {}
        if relay.get("dispatch_id") == entry["dispatch_id"] and relay.get("dispatch_created"):
            data["dispatches"][entry["dispatch_id"]] = entry
            data["schedule"] = pending["schedule"]
        data.pop("pending")
        save_dispatch_state(root, data)
    # work_log 是相容用的投影。即使追加前後中斷，usage 仍由識別碼去重並補入日誌。
    logged = {e.get("dispatch_id") for e in _entries(root, 0) if e.get("kind") == "dispatch"}
    for key, entry in data["dispatches"].items():
        if key not in logged:
            append(root, entry)
    return data


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
    since = time.time() - days * 86400
    entries = list(_entries(root, since))
    entries += [e for e in dispatch_state(root)["dispatches"].values() if e.get("time", 0) >= since]
    seen = set()
    for entry in entries:
        u = out.get(entry["party"])
        if u is None:
            continue
        kind = entry.get("kind", "round")
        if kind == "round":
            u["rounds_7d"] += 1
        elif kind == "dispatch":
            key = entry.get("dispatch_id")
            if key and key in seen:
                continue
            if key:
                seen.add(key)
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
    """單步平滑加權輪詢；work 保存排程。CLI 只在成功建立派工後持久化下一步。"""
    worker, schedule = schedule_next(candidates, shares, usages, work)
    work.clear()
    work.update(schedule)
    return worker
