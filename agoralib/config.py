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
share 是成功自動派工件數的權重（預設 1），按比例輪流，不保證 token 比例；0 只討論、主持、交接與審查，
不自行或透過子代理實作，使用者明確指定才可覆寫。額度只當資格篩選，見 shares.py。
subagents 是被 Agora 呼叫時能否啟動自己的內建子代理（預設 true）：Claude Code 禁用 Task 工具；
Antigravity 由 install 裝的 PreToolUse hook 擋下 invoke_subagent 等工具；Codex 目前無法強制關閉，只在提示中要求。
sandbox 只有 Codex 能設定，是接手工作時的沙箱（預設 workspace-write）；討論與總結一律 read-only。
build、check、add_dirs、sandbox 會影響接手方能執行的指令與能寫的範圍，接手方執行的 agora build / check 用該輪開始時的指令（cli.command_config），
被改動的設定在該輪結束後還原（cli.guard_config）。
"""

import errno
import json
import os
import posixpath
import re
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from agoralib.parties import AI_PARTIES

FIELDS = ("model", "effort")
# 依任務選模型的等級：分派方依改動範圍、風險、能否驗證選一級（assign --tier）；沒設定的等級沿用目前的模型
TIER_LEVELS = ("light", "standard", "heavy")
TIER_LABELS = {"light": "輕量", "standard": "標準", "heavy": "重度"}
SANDBOX_MODES = ("workspace-write", "danger-full-access", "read-only")
SANDBOX_PARTIES = ("codex",)
CHECK_FRAMEWORK_OUTPUTS = ("unittest", "pytest")
CHECK_FRAMEWORK_TARGET_FORMATS = ("path", "module")
_ENV_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_DRIVE_RE = re.compile(r"^[A-Za-z]:")


def config_path(root: Path) -> Path:
    return root / ".agora" / "config.json"


def load(root: Path, *, strict: bool = False) -> dict:
    try:
        return json.loads(config_path(root).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception:
        if strict:
            raise
        return {}


@contextmanager
def _config_lock(path: Path, timeout: float = 5.0):
    path.parent.mkdir(parents=True, exist_ok=True)
    # Keep the lock file in place so every writer locks the same file across replacements.
    # Closing the handle (including on process exit) releases the OS lock.
    with path.with_suffix(".lock").open("a+b") as handle:
        deadline = time.monotonic() + timeout
        while True:
            try:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ValueError(f"等待設定檔鎖逾時：{path}，請稍後重試。") from exc
                time.sleep(min(0.05, remaining))
        yield


def _write_config(path: Path, data: dict):
    content = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.name}.", suffix=".tmp", delete=False) as tmp:
            temp_path = Path(tmp.name)
            tmp.write(content)
            tmp.flush()
            os.fsync(tmp.fileno())
        for attempt in range(6):
            try:
                os.replace(temp_path, path)
                return
            except PermissionError as exc:
                # A Windows reader or scanner may briefly hold the destination open.
                if getattr(exc, "winerror", None) not in (5, 32, 33) or attempt == 5:
                    raise
                time.sleep(0.05 * (attempt + 1))
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def save(root: Path, data: dict):
    path = config_path(root)
    with _config_lock(path):
        _write_config(path, data)


def _model_spec(value) -> Optional[dict]:
    """{"model", "effort"}；沒有模型時為 None"""
    if not isinstance(value, dict) or not value.get("model"):
        return None
    return {"model": value["model"], "effort": value.get("effort") or None}


def _tiers(item: dict) -> Dict[str, Optional[dict]]:
    raw = item.get("tiers") if isinstance(item.get("tiers"), dict) else {}
    return {level: _model_spec(raw.get(level)) for level in TIER_LEVELS}


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
                   tiers: Optional[Dict[str, str]] = None) -> Dict[str, dict]:
    """更新參與方設定；模型、推理強度、沙箱給空字串或 default 表示改回 CLI 預設，比例給 default 表示改回 1，
    子代理給 on / off（default 表示改回不允許）。等級的鍵是「AI.等級」（例如 antigravity.heavy），值是「模型[:推理強度]」，
    default 清除。至少要保留一個 AI。"""
    path = config_path(root)
    with _config_lock(path):
        data = load(root, strict=True)
        settings = _update_party_data(root, data, enable, disable, models, efforts, shares,
                                      subagents, sandboxes, tiers)
        _write_config(path, data)
        return settings


def _update_party_data(root, data, enable, disable, models, efforts, shares, subagents, sandboxes, tiers):
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
    for key, value in (tiers or {}).items():
        p, _, level = key.partition(".")
        if p not in AI_PARTIES or level not in TIER_LEVELS:
            raise ValueError(f"等級的格式是 <AI>.<{'|'.join(TIER_LEVELS)}>=<模型>[:<推理強度>]：{key}={value}")
        item_tiers = parties.setdefault(p, {}).setdefault("tiers", {})
        spec = parse_model_spec(value)
        if spec:
            item_tiers[level] = spec
        else:
            item_tiers.pop(level, None)
        if not item_tiers:
            parties[p].pop("tiers", None)
    settings = party_settings(root, data)
    if not any(s["enabled"] for s in settings.values()):
        raise ValueError("至少要啟用一個 AI。")
    for item in parties.values():
        item_tiers = item.get("tiers") if isinstance(item, dict) else None
        if isinstance(item_tiers, dict):
            for spec in item_tiers.values():
                if isinstance(spec, dict):
                    spec.pop("fallback", None)
    return settings


def check_concurrency(root: Path, data: Optional[dict] = None) -> int:
    """跨分派 `agora check` 的同時執行上限（預設 1，非正整數視為 1）"""
    raw = (load(root) if data is None else data).get("check_concurrency", 1)
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        return 1
    return raw


def validate_check_framework(spec, root: Optional[Path] = None) -> Optional[dict]:
    """驗證 .agora/config.json 的 check_framework 設定；未設定（None）時回傳 None。"""
    if spec is None:
        return None
    if not isinstance(spec, dict):
        raise ValueError("check_framework 必須是 JSON 物件（dict）")

    output = spec.get("output")
    if not isinstance(output, str) or output not in CHECK_FRAMEWORK_OUTPUTS:
        raise ValueError("check_framework.output 必填，且只能是 'unittest' 或 'pytest'")

    rerun = spec.get("rerun")
    if not isinstance(rerun, list) or not rerun or not all(isinstance(x, str) for x in rerun):
        raise ValueError("check_framework.rerun 必填，且必須是非空的字串清單")

    script = rerun[0]
    if not script:
        raise ValueError("check_framework.rerun 的第一個元素不能是空字串")
    if script.startswith("-"):
        raise ValueError(f"check_framework.rerun 的腳本路徑不可用 '-' 開頭：{script}")
    if "{" in script or "}" in script:
        raise ValueError(f"check_framework.rerun 的腳本路徑不可包含 '{{' 或 '}}'：{script}")
    if script.startswith(("/", "\\")) or _DRIVE_RE.match(script) or ":" in script or Path(script).is_absolute():
        raise ValueError(f"check_framework.rerun 的腳本路徑不可為絕對路徑：{script}")
    norm = script.replace("\\", "/")
    parts = norm.split("/")
    if any(p == ".." for p in parts) or ".." in Path(script).parts:
        raise ValueError(f"check_framework.rerun 的腳本路徑不可包含 '..'：{script}")
    if not norm.endswith(".py"):
        raise ValueError(
            f"check_framework.rerun 的第一個元素必須是工作區內的 .py 腳本相對路徑（不需寫 python）：{script}"
        )

    normalized_script = posixpath.normpath(norm)
    if normalized_script in ("", ".", "..") or normalized_script.startswith("../") or normalized_script.startswith("/"):
        raise ValueError(f"check_framework.rerun 的腳本路徑無效：{script}")

    if root is not None:
        root_resolved = Path(root).resolve()
        script_path = root_resolved / normalized_script
        if not script_path.is_file():
            raise ValueError(f"check_framework.rerun 腳本不存在：{script}")
        try:
            resolved = script_path.resolve()
        except OSError as exc:
            raise ValueError(f"check_framework.rerun 腳本路徑無法解析：{script}（{exc}）") from exc
        if resolved == root_resolved or root_resolved not in resolved.parents:
            raise ValueError(f"check_framework.rerun 腳本指向工作區外：{script}")

    ids_indices = [i for i, item in enumerate(rerun) if item == "{ids}"]
    if len(ids_indices) != 1:
        raise ValueError("check_framework.rerun 必須恰好有一個元素等於 '{ids}'")
    ids_index = ids_indices[0]
    for i, item in enumerate(rerun):
        if i == ids_index:
            continue
        if "{" in item or "}" in item:
            raise ValueError(f"check_framework.rerun 固定參數不可包含 '{{' 或 '}}'：{item}")

    raw_env = spec.get("env")
    env: Dict[str, str] = {}
    if raw_env is not None:
        if not isinstance(raw_env, dict):
            raise ValueError("check_framework.env 必須是字串對字串的物件（dict）")
        for k, v in raw_env.items():
            if not isinstance(k, str) or not isinstance(v, str):
                raise ValueError("check_framework.env 的鍵與值都必須是字串")
            if not _ENV_KEY_RE.fullmatch(k):
                raise ValueError(f"check_framework.env 鍵名不合法：{k}（必須符合 ^[A-Z][A-Z0-9_]*$）")
            if k in ("PATH", "VIRTUAL_ENV") or k.startswith(("PYTHON", "AGORA_")):
                raise ValueError(f"check_framework.env 不得覆蓋受保護的環境變數：{k}")
            env[k] = v

    target_format = spec.get("target_format")
    if target_format is not None:
        if not isinstance(target_format, str) or target_format not in CHECK_FRAMEWORK_TARGET_FORMATS:
            raise ValueError("check_framework.target_format 只能是 'path' 或 'module'")

    return {
        "output": output,
        "rerun": list(rerun),
        "script": normalized_script,
        "ids_index": ids_index,
        "env": env,
        "target_format": target_format,
    }


def _same_path(a: Path, b: Path) -> bool:
    import os
    try:
        return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))
    except OSError:
        return a.as_posix().lower() == b.as_posix().lower()


def _resolve_home_agora() -> Optional[Path]:
    try:
        return (Path.home() / ".agora").resolve()
    except (OSError, RuntimeError):
        return None


_DEFAULT_GLOBAL_AGORA = _resolve_home_agora()


def is_global_agora_dir(path: Path) -> bool:
    """判斷 path 是否為全域安裝目錄 ~/.agora。"""
    for global_agora in (_resolve_home_agora(), _DEFAULT_GLOBAL_AGORA):
        if global_agora is not None and _same_path(path, global_agora):
            return True
    return False


def find_root(start: Path) -> Optional[Path]:
    """從 start 往上找最近含有 .agora 的專案資料夾，自動排除全域安裝目錄 ~/.agora。"""
    start = Path(start).resolve()
    for p in [start, *start.parents]:
        candidate = p / ".agora"
        if candidate.is_dir():
            if is_global_agora_dir(candidate):
                continue
            return p
    return None


def _git_root(start: Path) -> Optional[Path]:
    import subprocess
    from agoralib import quota
    try:
        top = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=start,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
            **quota.NO_WINDOW,
        ).stdout.strip()
        if top:
            return Path(top).resolve()
    except Exception:
        pass
    return None


def resolve_root(workspace: Optional[str] = None, start: Optional[Path] = None) -> Path:
    """統一解析工作區根目錄（CLI 與 hook 共用）。

    優先順序：
    1. 明確傳入的 workspace（例如 CLI 的 --workspace）
    2. worktree 環境變數 AGORA_WORKTREE
    3. 環境變數 AGORA_WORKSPACE
    4. 自動搜尋（從 start 或目前工作目錄開始）：
       a. 若位於 .agora/worktrees/<id> 內，使用該分派 worktree 的根目錄
       b. 往上找最近含有 .agora 的專案目錄（find_root，排除全域 ~/.agora）
       c. git 倉庫根目錄（git rev-parse --show-toplevel）
       d. 退回起點目錄（start 或 cwd）
    """
    import os
    if workspace:
        return Path(workspace).resolve()
    worktree_dir = os.getenv("AGORA_WORKTREE")
    if worktree_dir:
        return Path(worktree_dir).resolve()
    env_workspace = os.getenv("AGORA_WORKSPACE")
    if env_workspace:
        return Path(env_workspace).resolve()
    base = Path(start).resolve() if start is not None else Path.cwd().resolve()
    for candidate in (base, *base.parents):
        worktrees = candidate.parent
        if worktrees == worktrees.parent.parent / ".agora" / "worktrees":
            return candidate
    return find_root(base) or _git_root(base) or base


def resolve_control_root(root: Path) -> Path:
    """從分派目錄結構或隔離環境取得控制根目錄。"""
    import os
    resolved = Path(root).resolve()
    absolute = Path(root).absolute()
    for candidate in (absolute, *absolute.parents):
        worktrees = candidate.parent
        if worktrees.name == "worktrees" and worktrees.parent.name == ".agora":
            return worktrees.parent.parent.resolve()
    worktree_dir = os.getenv("AGORA_WORKTREE")
    control_dir = os.getenv("AGORA_CONTROL_ROOT")
    if worktree_dir and control_dir:
        wt = Path(worktree_dir).resolve()
        if _same_path(resolved, wt) or any(_same_path(p, wt) for p in resolved.parents):
            return Path(control_dir).resolve()
    return root
