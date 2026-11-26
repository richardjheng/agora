"""隔離分派的局部相關測試（`agora test`）。

提供接手方在開發中快速驗證相關測試的能力：
- 只在隔離分派的 worktree 內可用（不開放非隔離分派）。
- 優先讀取分派控制目錄下的 `test-targets.txt`；無檔或為空時依 `git diff <base_sha>` 與未追蹤 `.py` 自動挑選。
- 絕不退回執行完整測試；任何非法目標即整批拒絕。
- 使用獨立的 `.agora/test-slots/`（全域並行上限 2），同一分派同時只允許執行一次。
- 單次硬逾時 300 秒，逾時以 `jobs.kill_tree` 終止整棵程序樹。
- 結果僅寫入分派控制目錄的 `test-<時間>.log`，不寫 `meta["check_result"]`、不設 `RECORD_ENV`，不作為提交依據。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from agoralib import baseline, checkqueue, config, jobs, quota, worktree

TARGETS_FILENAME = "test-targets.txt"
TEST_ROOT = "tests"
MAX_TARGETS = 20
TEST_CONCURRENCY = 2
DEFAULT_TIMEOUT = 300.0
TAIL_CHARS = 3000

_WILDCARDS = set("*?[]")
_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_NODE_ID_RE = re.compile(
    r"^[A-Za-z0-9_]+(?:\[[A-Za-z0-9_]+\])?(?:::[A-Za-z0-9_]+(?:\[[A-Za-z0-9_]+\])?)*$"
)
_UNITTEST_ZERO_RE = re.compile(r"^Ran 0 tests\b", re.M)
_PYTEST_ZERO_RE = re.compile(r"(?:^no tests ran\b|\bcollected 0 items\b)", re.M | re.I)


class TargetedError(RuntimeError):
    """局部測試前置檢查或執行設定失敗。"""


class TargetValidationError(TargetedError):
    """目標清單包含不合格項目，整批拒絕。"""

    def __init__(self, errors: List[Tuple[str, str]]):
        self.errors = errors
        lines = ["測試目標驗證失敗，整批拒絕："]
        for target, reason in errors:
            lines.append(f"  - {target}: {reason}")
        super().__init__("\n".join(lines))


def targets_file_path(meta: dict) -> Path:
    """回傳此分派控制目錄下的 `test-targets.txt` 完整路徑。"""
    return Path(meta["manifest"]).parent / TARGETS_FILENAME


def parse_targets_file(path: Path) -> List[str]:
    """讀取目標檔（一行一個目標，忽略空行與 `#` 開頭的註解行）。"""
    if not path.is_file():
        return []
    targets: List[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        targets.append(line)
    return targets


def _is_test_filename(name: str) -> bool:
    return (name.startswith("test_") or name.endswith("_test.py")) and name.endswith(".py")


def auto_pick_targets(
    worktree_root: Path,
    base_sha: str,
    test_root: str = TEST_ROOT,
) -> List[str]:
    """依 `git diff <base_sha>` 與未追蹤 `.py` 自動挑選相關測試檔。"""
    root = Path(worktree_root).resolve()
    changed: set = set()
    for args in (
        ("diff", "--name-only", "--no-renames", "-z", base_sha),
        ("diff", "--cached", "--name-only", "--no-renames", "-z", base_sha),
        ("ls-files", "--others", "--exclude-standard", "--exclude=.venv/", "-z"),
    ):
        out = worktree.git(root, *args)
        for name in out.split("\0"):
            if not name:
                continue
            parts = name.split("/")
            if any(p in (".git", ".agora", ".venv", "venv") or worktree.rebuildable_directory(p) for p in parts[:-1]):
                continue
            changed.add(name)

    picked: List[str] = []
    seen: set = set()
    test_prefix = test_root.strip("/") + "/"

    for rel in sorted(changed):
        if not rel.endswith(".py"):
            continue
        path = root / rel
        if not path.is_file():
            continue
        name = Path(rel).name
        if rel.startswith(test_prefix) and _is_test_filename(name):
            if rel not in seen:
                seen.add(rel)
                picked.append(rel)
            continue
        stem = Path(rel).stem
        if stem and stem != "__init__":
            candidate_rel = f"{test_prefix}test_{stem}.py"
            if (root / candidate_rel).is_file() and candidate_rel not in seen:
                seen.add(candidate_rel)
                picked.append(candidate_rel)
    return picked


def validate_targets(
    raw_targets: Sequence[str],
    worktree_root: Path,
    framework: str,
    *,
    test_root: str = TEST_ROOT,
    max_targets: int = MAX_TARGETS,
) -> List[str]:
    """整批驗證目標；任何一項不合格即拋出 TargetValidationError 並逐項列出原因。"""
    root = Path(worktree_root).resolve()
    test_root_norm = test_root.strip("/\\") or TEST_ROOT
    test_root_path = (root / test_root_norm).resolve()
    errors: List[Tuple[str, str]] = []
    validated: List[str] = []
    seen: set = set()

    if len(raw_targets) > max_targets:
        errors.append((
            f"目標總數 ({len(raw_targets)})",
            f"超過上限 {max_targets} 個",
        ))

    for raw in raw_targets:
        if not isinstance(raw, str) or not raw:
            errors.append((repr(raw), "目標不能是空值"))
            continue
        if raw.startswith("-"):
            errors.append((raw, "不可使用以 '-' 開頭的選項"))
            continue
        if any(ch.isspace() for ch in raw):
            errors.append((raw, "不可包含空白字元"))
            continue

        if "::" in raw:
            file_part, node_part = raw.split("::", 1)
            if framework != "pytest":
                errors.append((raw, "只有 pytest 支援 路徑::名稱 形式"))
                continue
            if not node_part or not _NODE_ID_RE.fullmatch(node_part):
                errors.append((raw, "名稱段只允許 [A-Za-z0-9_]、:: 與 [參數]"))
                continue
        else:
            file_part, node_part = raw, None

        if any(ch in _WILDCARDS for ch in file_part):
            errors.append((raw, "不可包含萬用字元（* ? [ ]）"))
            continue
        if file_part.startswith(("/", "\\")) or _DRIVE_RE.match(file_part) or ":" in file_part:
            errors.append((raw, "不可使用絕對路徑或磁碟代號"))
            continue

        norm_posix = file_part.replace("\\", "/")
        parts = norm_posix.split("/")
        if any(p in ("", ".", "..") for p in parts):
            errors.append((raw, "不可包含 '..'、'.' 或空路徑段"))
            continue
        if not norm_posix.endswith(".py"):
            errors.append((raw, "目標檔案必須是 .py 檔"))
            continue
        if parts[0] != test_root_norm:
            errors.append((raw, f"目標必須位於 {test_root_norm}/ 目錄底下"))
            continue

        candidate = root / norm_posix
        if not candidate.exists() or not candidate.is_file():
            errors.append((raw, "檔案不存在於 worktree 內"))
            continue

        try:
            resolved = candidate.resolve()
        except OSError as exc:
            errors.append((raw, f"無法解析路徑：{exc}"))
            continue
        if test_root_path not in resolved.parents or root not in resolved.parents:
            errors.append((raw, f"解析後路徑超出測試根目錄 {test_root_norm}/"))
            continue

        normalized = f"{norm_posix}::{node_part}" if node_part is not None else norm_posix
        if normalized not in seen:
            seen.add(normalized)
            validated.append(normalized)

    if errors:
        raise TargetValidationError(errors)
    return validated


def resolve_runner(
    worktree_root: Path,
    check_spec,
    *,
    env: Optional[dict] = None,
) -> Dict[str, object]:
    """從凍結的 check 指令（或 default_check）推導 Python 與測試框架。"""
    from agoralib import cli

    root = Path(worktree_root).resolve()
    spec = check_spec
    if not spec:
        try:
            spec, _ = cli.default_check(root, env=env, strict_pytest=True)
        except ValueError as exc:
            raise TargetedError(f"分派環境缺失：{exc}") from exc
        if not spec:
            raise TargetedError("無法推導測試框架：未設定 check 且找不到 tests/ 目錄。")

    if isinstance(spec, list) and spec and all(isinstance(x, list) for x in spec):
        raise TargetedError("無法從複合 check 指令清單推導單一測試框架。")

    try:
        planned = baseline.plan(spec)
    except ValueError as exc:
        raise TargetedError(f"無法推導測試框架：{exc}") from exc

    framework = planned["framework"]
    python = cli.command_python(spec)
    if not python:
        venv_py = worktree.python_path(root)
        python = str(venv_py) if venv_py.is_file() else sys.executable

    if not Path(python).is_file() and not shutil.which(python):
        raise TargetedError(f"找不到測試用 Python 執行檔：{python}")

    if framework == "pytest":
        if not cli.has_module(python, "pytest", root=root, env=env):
            raise TargetedError(f"缺少測試套件：{python} 未安裝 pytest。")

    return {
        "python": python,
        "framework": framework,
        "start": planned["start"],
        "top": planned["top"],
        "flags": planned["flags"],
    }


def _has_package_chain(worktree_root: Path, directory: Path) -> bool:
    """從 directory 往上至 worktree_root（不含）是否每層皆有 `__init__.py`。"""
    root = worktree_root.resolve()
    current = directory.resolve()
    if current == root:
        return False
    while current != root:
        if not (current / "__init__.py").is_file():
            return False
        if current.parent == current:
            return False
        current = current.parent
    return True


def build_commands(
    python: str,
    framework: str,
    targets: Sequence[str],
    worktree_root: Path,
) -> List[List[str]]:
    """依測試框架與目標清單組成 argv 清單（一律以 shell=False 執行）。"""
    root = Path(worktree_root).resolve()
    if framework == "pytest":
        return [[python, "-m", "pytest", "-p", "no:cacheprovider", *targets]]
    if framework == "unittest":
        commands: List[List[str]] = []
        for target in targets:
            rel = Path(target)
            target_dir = rel.parent.as_posix()
            filename = rel.name
            cmd = [python, "-m", "unittest", "discover", "-s", target_dir, "-p", filename]
            if _has_package_chain(root, root / rel.parent):
                cmd.extend(["-t", str(root)])
            commands.append(cmd)
        return commands
    raise TargetedError(f"不支援的測試框架：{framework}")


def _run_one_command(
    argv: List[str],
    cwd: Path,
    env: dict,
    timeout: float,
) -> Tuple[int, str, bool]:
    """以 shell=False 執行單一指令，逾時以 jobs.kill_tree 終止整棵程序樹。"""
    proc = subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        **quota.NO_WINDOW,
    )
    try:
        output, _ = proc.communicate(timeout=max(0.01, timeout))
        return proc.returncode, output or "", False
    except subprocess.TimeoutExpired as exc:
        partial = exc.output or ""
        if isinstance(partial, bytes):
            partial = partial.decode("utf-8", errors="replace")
        detail = jobs.kill_tree(proc.pid, proc)
        tail = ""
        try:
            rest, _ = proc.communicate(timeout=2)
            if rest:
                tail = rest.decode("utf-8", errors="replace") if isinstance(rest, bytes) else rest
        except (subprocess.TimeoutExpired, OSError):
            pass
        combined = partial + tail
        if detail:
            combined += f"\n{detail}\n"
        return 124, combined, True


def _detect_zero_tests(framework: str, exit_code: int, output: str) -> Optional[str]:
    if framework == "pytest":
        if exit_code == 5 or _PYTEST_ZERO_RE.search(output):
            return "收集到零個測試（pytest 未收集到任何測試）。"
    elif framework == "unittest":
        if _UNITTEST_ZERO_RE.search(output):
            return "收集到零個測試（unittest 執行了 0 個測試）。"
    return None


@contextmanager
def dispatch_test_lock(dispatch_dir: Path):
    """同一分派同時只允許執行一次 `agora test`。"""
    lock_key = dispatch_dir / "test.json"
    try:
        with config._config_lock(lock_key, timeout=0):
            yield
    except ValueError as exc:
        raise TargetedError("此分派已有進行中的 agora test，同時只能執行一次。") from exc


def execute_commands(
    commands: Sequence[List[str]],
    worktree_root: Path,
    framework: str,
    *,
    env: Optional[dict] = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> Dict[str, object]:
    """依序執行所有 argv 指令，共用單次硬逾時並檢查零測試收集。"""
    root = Path(worktree_root).resolve()
    run_env = worktree.project_environment(os.environ if env is None else env)
    start_ts = time.monotonic()
    deadline = start_ts + timeout
    chunks: List[str] = []
    exit_code = 0
    timed_out = False
    zero_note: Optional[str] = None

    for argv in commands:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
            exit_code = 124
            chunks.append(f"\n[逾時] 局部測試超過 {timeout:g} 秒上限，未繼續執行後續目標。\n")
            break
        chunks.append(f"$ {' '.join(argv)}\n")
        code, output, hit_timeout = _run_one_command(list(argv), root, run_env, remaining)
        chunks.append(output)
        if output and not output.endswith("\n"):
            chunks.append("\n")
        if hit_timeout:
            timed_out = True
            exit_code = 124
            chunks.append(f"\n[逾時] 局部測試超過 {timeout:g} 秒上限，已終止整棵程序樹。\n")
            break
        zero_msg = _detect_zero_tests(framework, code, output)
        if zero_msg:
            zero_note = zero_msg
            chunks.append(f"\n[失敗] {zero_msg}\n")
            if exit_code == 0:
                exit_code = 1
        elif code != 0:
            exit_code = code

    elapsed = round(max(0.0, time.monotonic() - start_ts), 3)
    return {
        "exit_code": exit_code,
        "output": "".join(chunks),
        "timed_out": timed_out,
        "zero_tests": zero_note,
        "elapsed": elapsed,
    }


def _next_log_path(dispatch_dir: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    candidate = dispatch_dir / f"test-{stamp}.log"
    if not candidate.exists():
        return candidate
    return dispatch_dir / f"test-{stamp}-{uuid.uuid4().hex[:6]}.log"


def run_targeted_tests(
    ws,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    concurrency: int = TEST_CONCURRENCY,
) -> int:
    """執行隔離分派的局部測試流程，回傳 exit code。"""
    from agoralib import cli

    task = cli.active_task(ws)
    if task is None:
        sys.exit("agora test 只能在隔離分派中使用，請改用 agora check。")

    worktree_root = Path(task["worktree"]).resolve()
    dispatch_dir = Path(task["manifest"]).parent
    targets_file = targets_file_path(task)

    raw_targets = parse_targets_file(targets_file)
    if raw_targets:
        source_label = "目標檔"
    else:
        raw_targets = auto_pick_targets(worktree_root, task["base_sha"])
        source_label = "依變更自動挑選"

    if not raw_targets:
        sys.exit(
            f"找不到可執行的相關測試目標（沒有變更對應到測試檔，絕不退回完整測試）。\n"
            f"請將要執行的測試目標（每行一個）寫入：{targets_file}"
        )

    try:
        with dispatch_test_lock(dispatch_dir):
            with cli.checked_task_environment(ws, refresh=True):
                check_spec = cli.command_config(ws).get("check")
                runner = resolve_runner(worktree_root, check_spec)
                validated = validate_targets(raw_targets, worktree_root, runner["framework"])
                commands = build_commands(runner["python"], runner["framework"], validated, worktree_root)

                first_line = f"測試目標（{source_label}，共 {len(validated)} 個）：{', '.join(validated)}"
                print(first_line, flush=True)

                control_root, task_thread, slot_label = cli.check_slot_context(ws)
                stop_check = (lambda: cli.stop_requested(task_thread)) if task_thread else None
                try:
                    with checkqueue.acquire_slot(
                        control_root,
                        limit=concurrency,
                        label=f"{slot_label} test",
                        stop_check=stop_check,
                        dirname=checkqueue.TEST_SLOTS_DIRNAME,
                    ):
                        result = execute_commands(
                            commands,
                            worktree_root,
                            runner["framework"],
                            timeout=timeout,
                        )
                except checkqueue.CheckStopped as exc:
                    sys.exit(str(exc))
                except PermissionError as exc:
                    slots = checkqueue.slots_dir(control_root, dirname=checkqueue.TEST_SLOTS_DIRNAME)
                    sys.exit(f"局部測試槽位目錄不可寫：{slots}（{exc}）；未執行測試。")
    except TargetedError as exc:
        sys.exit(str(exc))

    log_path = _next_log_path(dispatch_dir)
    header = (
        f"{first_line}\n"
        f"框架：{runner['framework']}｜Python：{runner['python']}\n\n"
    )
    full_output = header + result["output"]
    log_path.write_text(full_output, encoding="utf-8")

    tail = result["output"].strip()[-TAIL_CHARS:]
    if tail:
        print(tail, flush=True)
    summary = (
        f"局部測試結果：exit={result['exit_code']}｜耗時 {result['elapsed']:.1f}s｜"
        f"紀錄：{log_path}（局部測試，不作為提交依據）"
    )
    print(summary, flush=True)
    return 0 if result["exit_code"] == 0 else (124 if result["timed_out"] else 1)


def cmd_test(ws, args):
    """`agora test` 子指令進入點。"""
    sys.exit(run_targeted_tests(ws))
