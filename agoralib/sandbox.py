"""Detect the current Codex sandbox token without trusting user environment names."""

import ctypes
import os
import sys
from typing import Optional


MESSAGE = ("目前在 Codex 沙箱（網路受限）中執行，無法呼叫其他 AI。"
           "請讓 Codex 以沙箱外執行此 agora 指令（核准提權），"
           "或改由 Claude Code／VSCode 面板發起。")
AUTH_NOTE = "以 Codex 沙箱身分執行，可能與沙箱有關（未證實）"
NETWORK_DISABLED_ENV = "CODEX_SANDBOX_NETWORK_DISABLED"


def token_username() -> Optional[str]:
    """Return the Windows token user, or unknown if the platform/query fails."""
    if not sys.platform.startswith("win"):
        return None
    try:
        size = ctypes.c_ulong(257)  # UNLEN + terminating null
        name = ctypes.create_unicode_buffer(size.value)
        if ctypes.windll.advapi32.GetUserNameW(name, ctypes.byref(size)):
            return name.value
    except Exception:
        pass
    return None


def is_codex_sandbox() -> bool:
    return (token_username() or "").startswith("CodexSandbox")


def _network_restriction_reason() -> Optional[str]:
    username = token_username()
    if username:
        if username.startswith("CodexSandboxOffline"):
            return f"token={username}"
    elif os.environ.get(NETWORK_DISABLED_ENV) == "1":
        return f"環境變數 {NETWORK_DISABLED_ENV}=1"
    return None


def network_restricted() -> bool:
    return _network_restriction_reason() is not None


def require_network():
    if network_restricted():
        from agoralib.parties import CallError
        reason = _network_restriction_reason() or "判斷後狀態已變更"
        raise CallError(f"{MESSAGE}（依據：{reason}）", kind="sandbox")
