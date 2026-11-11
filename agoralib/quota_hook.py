"""
額度提醒 hook：讀快取（必要時在背景更新），把三方剩餘額度與交接指示注入給 agent。

  python <agora>/agoralib/quota_hook.py claude-prompt   # Claude Code UserPromptSubmit：每次都附一行額度
  python <agora>/agoralib/quota_hook.py claude-tool     # Claude Code PostToolUse：只在需要交接/停工時提醒
  python <agora>/agoralib/quota_hook.py agy-pre         # Antigravity PreInvocation：首次呼叫附額度，需要時提醒
  python <agora>/agoralib/quota_hook.py agy-tool        # Antigravity PreToolUse（子代理工具）：Agora 設定不允許時擋下
  python <agora>/agoralib/quota_hook.py codex-prompt    # Codex UserPromptSubmit：同 claude-prompt

使用者自己開的對話（不是被 Agora 呼叫的）另外附上依勾選分工的指示（見 routing.py）。

只讀檔案、不做網路請求，確保 hook 本身很快；快取過期時另開背景程序更新。
"""

from __future__ import annotations   # 支援 Python 3.9（型別標註用到 X | None）

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agoralib import config, quota, routing, shares  # noqa: E402

STATE_FILE = quota.AGORA_HOME / "hook_state.json"
NAMES = quota.LABELS


def load_state() -> dict:
    return quota._read_json(STATE_FILE) or {}


def save_state(state: dict):
    quota._write_json(STATE_FILE, state)


def advice(me: str, usages: dict, weights: dict | None = None, work: dict | None = None) -> tuple[str | None, str | None]:
    """回傳 (提醒等級, 指示文字)；等級用來避免同一個重置週期內重複提醒。
    hook 只檢查是否有人合格；實際接手方由 assign 在專案鎖內依比例輪流挑選，不預先消耗排程。"""
    weights, work = weights or {}, work or {}
    mine = usages.get(me)
    if not quota.low(mine):
        return None, None
    if os.getenv("AGORA_INVOKED"):
        # 自己就是接手方：不再往回交接，寫好進度就停
        return f"worker-{mine.get('resets_at')}", (
            f"⚠️ 你（{NAMES[me]}）的額度剩 {mine['remaining_pct']:.0f}%。完成目前這一小步後，"
            f"把交接說明寫進進度檔，回覆最後一行寫「【額度不足】」並結束。")
    others = {p: u for p, u in usages.items() if p != me and u and weights.get(p, 1) > 0 and not u.get("blocked")}
    my_reset = mine.get("resets_at")
    if not others:
        return f"none-{my_reset}", (
            f"⚠️ {NAMES[me]} 額度剩 {mine['remaining_pct']:.0f}%（{quota.fmt_time(my_reset)} 重置），"
            f"此專案沒有其他可接手的 AI（未啟用或分配比例為 0）。依 relay skill 把目前進度寫進 Agora 討論串，"
            f"回報沒有合格接手方後退出，不等待重置，也不自行實作。")
    if not shares.eligible(others, weights, others):
        return f"all-{my_reset}", (
            f"⚠️ 沒有合格接手方（額度不足、未知、過期或無法使用）。"
            f"依 relay skill 把目前進度寫進 Agora 討論串後回報退出，不等待重置，也不自行實作。")
    return f"self-{my_reset}", (
        f"⚠️ {NAMES[me]} 額度剩 {mine['remaining_pct']:.0f}%（{quota.fmt_time(my_reset)} 重置），"
        f"立即依 relay skill 寫工作單交接（assign 預設不寫 --to，由 Agora 額度篩選後依比例輪流挑選），然後停止工作。")


def project_root() -> Path | None:
    start = os.getenv("CLAUDE_PROJECT_DIR") or os.getenv("AGORA_WORKSPACE") or os.getcwd()
    return config.find_root(Path(start).resolve())


def project_parties() -> list:
    """目前專案啟用的 AI：只在這些 AI 之間提醒交接（找不到專案設定時視為全部啟用）"""
    root = project_root()
    return config.enabled(root) if root else list(quota.USAGE)


def routing_line(me: str, prompt: str = "") -> str | None:
    """依勾選分工的指示；被 Agora 呼叫的回覆、斜線指令（例如 /code-review）不附"""
    root = project_root()
    if os.getenv("AGORA_INVOKED") or prompt.lstrip().startswith("/") or not root:
        return None
    return routing.instruction(me, config.enabled(root), config.shares(root))


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "claude-prompt"
    if mode == "agy-tool":
        # 只有被 Agora 呼叫、且此專案設定不允許內建子代理時（環境變數 AGORA_SUBAGENTS=0）才擋；使用者自己開的不受影響
        out = ({"decision": "deny", "reason": "使用者設定這次不允許啟動內建子代理，請改用自己的工具完成。"}
               if os.getenv("AGORA_SUBAGENTS") == "0" else {"decision": "allow"})
        sys.stdout.buffer.write(json.dumps(out, ensure_ascii=False).encode("utf-8"))
        return
    (quota.AGORA_HOME / f"last_hook_{mode}").touch()  # 診斷用：確認 hook 有在執行
    if os.getenv(quota.PROBE_ENV):
        print("{}")
        return
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8") or "{}")
    except Exception:
        payload = {}

    me = "antigravity" if mode.startswith("agy") else "codex" if mode.startswith("codex") else "claude"
    parties = set(project_parties()) | {me}
    usages = {p: u for p, u in quota.all_usage().items() if p in parties}
    max_age = quota.wanted_max_age(*usages.values())
    if any(quota.is_stale(usages[p], max_age) for p in ("claude", "antigravity") if p in usages):
        quota.refresh_in_background()

    root = project_root()
    level, text = advice(me, usages, config.shares(root) if root else None, shares.recent_work(root) if root else None)
    status = "【額度】" + "｜".join(quota.describe(NAMES[k], u) for k, u in usages.items())

    state = load_state()
    key = f"{me}-alerted"
    already = level is not None and state.get(key) == level
    if level and not already:
        state[key] = level
        save_state(state)

    if mode in ("claude-prompt", "codex-prompt"):
        route = routing_line(me, str(payload.get("prompt") or ""))
        lines = [status] + ([text] if text else []) + ([route] if route else [])
        out = {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "\n".join(lines)}}
    elif mode == "claude-tool":
        out = {}
        if text and not already:
            out = {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": f"{status}\n{text}"}}
    else:  # agy-pre
        first = payload.get("invocationNum", 1) <= 1
        msgs = []
        if first:
            msgs.append(status)
        if text and (first or not already):
            msgs.append(text)
        route = routing_line(me) if first else None
        if route:
            msgs.append(route)
        out = {"injectSteps": [{"ephemeralMessage": "\n".join(msgs)}]} if msgs else {}

    sys.stdout.buffer.write(json.dumps(out, ensure_ascii=False).encode("utf-8"))


if __name__ == "__main__":
    main()
