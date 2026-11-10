"""
工作區設定：<工作區>/.agora/config.json

{
  "build": "west build",
  "check": ["python", "-m", "unittest", "discover", "-s", "tests"],
  "add_dirs": ["../shared-cache"],
  "parties": {
    "claude":      {"enabled": true,  "model": "opus", "effort": "high", "share": 2, "subagents": false},
    "antigravity": {"enabled": false},
    "codex":       {"enabled": true,  "model": "gpt-6.1-sol", "share": 1, "sandbox": "danger-full-access"}
  }
}

build / check 是 `agora build` / `agora check` 執行的指令：字串交給 shell，字串清單直接執行，清單的清單依序執行。
check 沒設定時，有 tests/ 資料夾才跑 unittest。
add_dirs 是 Codex 接手工作（workspace-write）時額外可寫的目錄，相對路徑以工作區為準，不存在的略過。

parties 沒寫到的 AI 視為啟用、模型與推理強度沿用各 CLI 自己的預設。
討論串可以另外指定模型（new --model），優先於這裡的設定。
share 是自動挑選接手方時的工作分配比例（預設 1，0 表示不自動分派給它），見 shares.py。
subagents 是被 Agora 呼叫時能否啟動自己的內建子代理（預設 true）：Claude Code 禁用 Task 工具；
Antigravity 由 install 裝的 PreToolUse hook 擋下 invoke_subagent 等工具；Codex 目前無法強制關閉，只在提示中要求。
sandbox 只有 Codex 能設定，是接手工作時的沙箱（預設 workspace-write）；討論與總結一律 read-only。
build、check、add_dirs、sandbox 會影響接手方能執行的指令與能寫的範圍，接手方執行的 agora build / check 用該輪開始時的指令（cli.command_config），
被改動的設定在該輪結束後還原（cli.guard_config）。
"""

import json
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from agoralib.parties import AI_PARTIES

FIELDS = ("model", "effort")
# 依任務選模型的等級：分派方依改動範圍、風險、能否驗證選一級（assign --tier）；沒設定的等級沿用目前的模型
TIER_LEVELS = ("light", "standard", "heavy")
TIER_LABELS = {"light": "輕量", "standard": "標準", "heavy": "重度"}
SANDBOX_MODES = ("workspace-write", "danger-full-access", "read-only")
SANDBOX_PARTIES = ("codex",)


def config_path(root: Path) -> Path:
    return root / ".agora" / "config.json"


def load(root: Path) -> dict:
    try:
        return json.loads(config_path(root).read_text(encoding="utf-8"))
    except Exception:
        return {}


def save(root: Path, data: dict):
    path = config_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _model_spec(value) -> Optional[dict]:
    """{"model", "effort"}；沒有模型時為 None"""
    if not isinstance(value, dict) or not value.get("model"):
        return None
    return {"model": value["model"], "effort": value.get("effort") or None}


def _tiers(item: dict) -> Dict[str, Optional[dict]]:
    raw = item.get("tiers") if isinstance(item.get("tiers"), dict) else {}
    out = {}
    for level in TIER_LEVELS:
        spec = _model_spec(raw.get(level))
        if spec:
            spec["fallback"] = _model_spec(raw[level].get("fallback"))
        out[level] = spec
    return out


def party_settings(root: Path, data: Optional[dict] = None) -> Dict[str, dict]:
    raw = (load(root) if data is None else data).get("parties") or {}
    out = {}
    for p in AI_PARTIES:
        item = raw.get(p) or {}
        share = item.get("share", 1)
        out[p] = {"enabled": item.get("enabled", True) is not False,
                  **{f: item.get(f) or None for f in FIELDS},
                  "sandbox": item.get("sandbox") if p in SANDBOX_PARTIES and item.get("sandbox") in SANDBOX_MODES
                  else None,
                  "share": share if isinstance(share, int) and share >= 0 else 1,
                  # 預設不允許：子代理是否值得用還在評估（用了反而要別的 AI 找錯、修正，會更浪費）
                  "subagents": item.get("subagents", False) is True,
                  "tiers": _tiers(item)}
    return out


def parse_model_spec(value: str) -> Optional[dict]:
    """「模型[:推理強度]」；空字串或 default 表示清除（回傳 None）"""
    value = (value or "").strip()
    if value in ("", "default"):
        return None
    model, _, effort = value.partition(":")
    if not model.strip():
        raise ValueError(f"模型不能是空的：{value}")
    return {"model": model.strip(), **({"effort": effort.strip()} if effort.strip() else {})}


def enabled(root: Path) -> List[str]:
    return [p for p, s in party_settings(root).items() if s["enabled"]]


def shares(root: Path) -> Dict[str, int]:
    return {p: s["share"] for p, s in party_settings(root).items()}


def update_parties(root: Path, enable: Iterable[str] = (), disable: Iterable[str] = (),
                   models: Optional[Dict[str, str]] = None, efforts: Optional[Dict[str, str]] = None,
                   shares: Optional[Dict[str, str]] = None,
                   subagents: Optional[Dict[str, str]] = None,
                   sandboxes: Optional[Dict[str, str]] = None,
                   tiers: Optional[Dict[str, str]] = None,
                   tier_fallbacks: Optional[Dict[str, str]] = None) -> Dict[str, dict]:
    """更新參與方設定；模型、推理強度、沙箱給空字串或 default 表示改回 CLI 預設，比例給 default 表示改回 1，
    子代理給 on / off（default 表示改回不允許）。等級的鍵是「AI.等級」（例如 antigravity.heavy），值是「模型[:推理強度]」，
    default 清除。至少要保留一個 AI。"""
    data = load(root)
    parties = data.setdefault("parties", {})
    for p, value in [(p, True) for p in enable] + [(p, False) for p in disable]:
        parties.setdefault(p, {})["enabled"] = value
    for field, values in (("model", models or {}), ("effort", efforts or {})):
        for p, value in values.items():
            item = parties.setdefault(p, {})
            if value and value != "default":
                item[field] = value
            else:
                item.pop(field, None)
    for p, value in (sandboxes or {}).items():
        if p not in SANDBOX_PARTIES:
            raise ValueError(f"只有 {', '.join(SANDBOX_PARTIES)} 可以設定沙箱模式：{p}={value}")
        item = parties.setdefault(p, {})
        if value in ("", "default"):
            item.pop("sandbox", None)
        elif value in SANDBOX_MODES:
            item["sandbox"] = value
        else:
            raise ValueError(f"沙箱模式設定只能是 {', '.join(SANDBOX_MODES)}：{p}={value}")
    for p, value in (shares or {}).items():
        item = parties.setdefault(p, {})
        if value in ("", "default"):
            item.pop("share", None)
            continue
        try:
            share = int(value)
        except ValueError:
            share = -1
        if share < 0:
            raise ValueError(f"分配比例必須是非負整數：{p}={value}")
        item["share"] = share
    for p, value in (subagents or {}).items():
        item = parties.setdefault(p, {})
        value = str(value).strip().lower()
        if value in ("on", "true", "1", "yes"):
            item["subagents"] = True
        elif value in ("", "default", "off", "false", "0", "no"):
            item.pop("subagents", None)
        else:
            raise ValueError(f"子代理設定只能是 on 或 off：{p}={value}")
    for key, value, fallback in [(k, v, False) for k, v in (tiers or {}).items()] \
            + [(k, v, True) for k, v in (tier_fallbacks or {}).items()]:
        p, _, level = key.partition(".")
        if p not in AI_PARTIES or level not in TIER_LEVELS:
            raise ValueError(f"等級的格式是 <AI>.<{'|'.join(TIER_LEVELS)}>=<模型>[:<推理強度>]：{key}={value}")
        item_tiers = parties.setdefault(p, {}).setdefault("tiers", {})
        spec = parse_model_spec(value)
        if fallback:
            if not (item_tiers.get(level) or {}).get("model"):
                raise ValueError(f"先設定 {p}.{level} 的模型，才能設定替代模型")
            if spec:
                item_tiers[level]["fallback"] = spec
            else:
                item_tiers[level].pop("fallback", None)
        elif spec:
            item_tiers[level] = {**spec, **({"fallback": item_tiers[level]["fallback"]}
                                            if (item_tiers.get(level) or {}).get("fallback") else {})}
        else:
            item_tiers.pop(level, None)
        if not item_tiers:
            parties[p].pop("tiers", None)
    settings = party_settings(root, data)
    if not any(s["enabled"] for s in settings.values()):
        raise ValueError("至少要啟用一個 AI。")
    save(root, data)
    return settings


def find_root(start: Path) -> Optional[Path]:
    """從 start 往上找含有 .agora 的資料夾（hook 用來判斷目前專案啟用了哪些 AI）"""
    for p in [start, *start.parents]:
        if (p / ".agora").is_dir():
            return p
    return None
