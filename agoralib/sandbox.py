"""Detect the current Codex sandbox token without trusting user environment names."""

import ctypes
import os
import sys
from typing import Optional


MESSAGE = ("目前在 Codex 沙箱（網路受限）中執行，無法呼叫其他 AI。"
           "請讓 Codex 以沙箱外執行此 agora 指令（核准提權），"
           "或改由 Claude Code／VSCode 面板發起。")
AUTH_NOTE = "以 Codex 沙箱身分執行，可能與沙箱有關（未證實）"


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


def network_restricted() -> bool:
    return (os.environ.get("CODEX_SANDBOX_NETWORK_DISABLED") == "1"
            or (token_username() or "").startswith("CodexSandboxOffline"))


def require_network():
    if network_restricted():
        from agoralib.parties import CallError
        raise CallError(MESSAGE, kind="sandbox")
