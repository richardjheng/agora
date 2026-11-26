"""Controlled `--no-ff` merge and post-merge cleanup for isolated dispatches.

Dispatches and finalize never auto-merge. Merging happens only when explicitly
requested by the user via `agora merge`. Cleanup removes only verified
rebuildable artifacts (virtualenv, Python caches, egg-info, and manifest-recorded
`build_outputs`), then removes the worktree while keeping the branch by default
(deleting it with `git branch -d` only when `--delete-branch` is specified).
"""

from __future__ import annotations

import json
import locale
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from agoralib import __version__, baseline, completions, config, jobs, parties, quota, shares, worktree

FAILED_TESTS_PREVIEW_LIMIT = 20
ENV_SUMMARY_KEYS = (
    "PYTHONPATH",
    "PYTHONUTF8",
    "PYTHONIOENCODING",
    "VIRTUAL_ENV",
    "PYTHONNOUSERSITE",
    "PYTHONHOME",
)

IN_PROGRESS_FILES = (
    ("MERGE_HEAD", "merge"),
    ("REBASE_HEAD", "rebase"),
    ("rebase-merge", "rebase"),
    ("rebase-apply", "rebase"),
    ("CHERRY_PICK_HEAD", "cherry-pick"),
)


class MergeError(RuntimeError):
    pass


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _git_run(root: Path, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    task_roots = [env.get(worktree.CONTROL_ENV), env.get(worktree.WORK_ENV)]
    if all(task_roots) and Path(root).resolve() in [Path(p).resolve() for p in task_roots]:
        env = worktree.git_environment(env, task_roots)
    return subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
        env=env,
        **quota.NO_WINDOW,
    )


def ref_exists(root: Path, ref: str) -> bool:
    return _git_run(root, "rev-parse", "--verify", "--quiet", ref).returncode == 0


def is_ancestor(root: Path, ancestor_ref: str, descendant_ref: str) -> bool:
    proc = _git_run(root, "merge-base", "--is-ancestor", ancestor_ref, descendant_ref)
    return proc.returncode == 0


def _git_dir(root: Path) -> Path:
    raw = worktree.git(root, "rev-parse", "--git-dir")
    path = Path(raw)
    return (path if path.is_absolute() else (root / path)).resolve()


def in_progress_git_ops(root: Path) -> List[str]:
    gdir = _git_dir(root)
    ops: List[str] = []
    for marker, label in IN_PROGRESS_FILES:
        if (gdir / marker).exists() and label not in ops:
            ops.append(label)
    return ops


def _tracked_dirty_paths(root: Path) -> List[str]:
    raw = worktree.git(root, "status", "--porcelain", "--untracked-files=no", "-z")
    if not raw:
        return []
    entries = raw.split("\0")
    paths: List[str] = []
    i = 0
    while i < len(entries):
        item = entries[i]
        if not item:
            i += 1
            continue
        status = item[:2]
        path = item[3:]
        if path:
            paths.append(path)
        if "R" in status or "C" in status:
            i += 2
        else:
            i += 1
    return sorted(dict.fromkeys(paths))


def classify_worktree_files(target: Path, meta: dict) -> Dict[str, List[str]]:
    """Classify tracked changes, rebuildable artifacts, and blocking untracked/ignored files."""
    tracked = _tracked_dirty_paths(target)
    args = ("ls-files", "--others", "--exclude-standard", "--directory", "--no-empty-directory", "-z")
    untracked = worktree.git(target, *args).split("\0")
    ignored = worktree.git(target, *args, "--ignored").split("\0")
    target = target.resolve()
    others = sorted(set(untracked + ignored) - {""})
    build_outputs = set(worktree.normalize_build_outputs(meta.get("build_outputs")))
    indexed = set(filter(None, worktree.git(target, "ls-files", "--cached", "-z").split("\0")))
    tracked_dirs = {p.as_posix() for name in indexed for p in Path(name).parents}
    rebuildable: List[str] = []
    blocking: List[str] = []

    def classify(entry: str) -> None:
        name = entry.rstrip("/")
        if name in indexed:
            return
        path = target / name
        if path.is_symlink() or target not in path.resolve().parents:
            blocking.append(name)
            return
        directory = entry.endswith("/")
        parts = name.split("/")
        cache_parts = parts if directory else parts[:-1]
        generated = (any(worktree.rebuildable_directory(part) for part in cache_parts)
                     or parts[0] == ".venv"
                     or any(name == output or name.startswith(output + "/") for output in build_outputs)
                     or (not directory and name.endswith(".pyc")))
        if generated and name not in tracked_dirs:
            rebuildable.append(entry)
        elif directory:
            # Git can collapse a new or ignored parent containing only caches.
            children = list(path.iterdir())
            if not children and not generated:
                blocking.append(entry)
            for child in children:
                suffix = "/" if child.is_dir() and not child.is_symlink() else ""
                classify(child.relative_to(target).as_posix() + suffix)
        else:
            blocking.append(entry)

    for entry in others:
        classify(entry)
    return {
        "tracked": tracked,
        "rebuildable": sorted(dict.fromkeys(rebuildable)),
        "blocking": sorted(dict.fromkeys(blocking)),
    }


def _record_active(record: Optional[dict]) -> bool:
    if not isinstance(record, dict) or not record:
        return False
    if jobs.pid_alive(record.get("pid"), record.get("pid_started")):
        return True
    if record.get("status") == "starting":
        from agoralib import cli
        return cli.relay_alive(record)
    return False


def _ensure_no_live_process(state: dict, relay: Optional[dict], dispatch_id: str) -> Optional[str]:
    latest = state.get("relay") or {}
    finalizer = state.get("finalizer") or {}
    records = [r for r in (relay, latest if latest.get("dispatch_id") == dispatch_id else None,
                           finalizer if finalizer.get("dispatch_id") == dispatch_id else None) if isinstance(r, dict)]
    for rec in records:
        if jobs.pid_alive(rec.get("pid"), rec.get("pid_started")):
            return "分派接手或收尾程序仍在執行"
        if rec.get("status") == "starting" and _record_active(rec):
            return "分派程序正在啟動，請稍後再試"
    return None


def _verify_worktree_ownership(root: Path, target: Path, meta: dict, branch_exists: bool) -> None:
    expected_target = root.resolve() / ".agora" / "worktrees" / meta["dispatch_id"]
    if target.resolve() != expected_target:
        raise worktree.WorktreeError("worktree 位置不符")
    worktree.validate_branch_ownership(meta)
    if Path(worktree.git(target, "rev-parse", "--show-toplevel")).resolve() != target.resolve():
        raise worktree.WorktreeError("Git worktree 根目錄不符")
    if branch_exists:
        actual = worktree.git(target, "symbolic-ref", "--quiet", "--short", "HEAD")
        if actual != meta["branch"]:
            raise worktree.WorktreeError(f"worktree branch 不符（預期 {meta['branch']}，實際 {actual}）")
    def _common(path: Path) -> Path:
        return (path / worktree.git(path, "rev-parse", "--git-common-dir")).resolve()
    if _common(target) != _common(root):
        raise worktree.WorktreeError("worktree repository 不符")
    expected_sha = meta.get("commit_sha") or meta.get("base_sha")
    if expected_sha and worktree.git(target, "rev-parse", "HEAD") != expected_sha:
        raise worktree.WorktreeError("worktree HEAD 與已驗證提交不符")


def _sync_dispatch_records(thread: Path, meta: dict) -> None:
    """Persist manifest and mirror updated merge/cleanup records to relay.json and state.json."""
    from agoralib import cli
    worktree.save(meta)
    dispatch_id = meta["dispatch_id"]
    if (thread / "state.json").is_file():
        with cli.thread_lock(thread, wait=cli.WORKER_LOCK_WAIT):
            state = cli.load_state(thread)
            latest = state.get("relay") or {}
            if latest.get("dispatch_id") == dispatch_id:
                for key in ("merge_record", "cleanup_record"):
                    if key in meta:
                        latest[key] = meta[key]
                latest["updated"] = _now_str()
                state["relay"] = latest
                cli.save_state(thread, state)
                cli.archive_relay(thread, latest)
                return
    relay_file = thread / "dispatches" / dispatch_id / "relay.json"
    if relay_file.is_file():
        try:
            archived = completions.read_object(relay_file)
            for key in ("merge_record", "cleanup_record"):
                if key in meta:
                    archived[key] = meta[key]
            archived["updated"] = _now_str()
            completions.atomic_write(relay_file, archived)
        except (OSError, ValueError):
            pass


def _remove_artifact(target: Path, entry: str) -> None:
    norm = entry.rstrip("/")
    full = (target / norm)
    resolved_target = target.resolve()
    if full.is_symlink():
        raise worktree.WorktreeError(f"拒絕刪除符號連結：{norm}")
    resolved = full.resolve()
    if resolved == resolved_target or resolved_target not in resolved.parents:
        raise worktree.WorktreeError(f"拒絕刪除 worktree 外路徑：{norm}")
    if full.is_dir():
        shutil.rmtree(full)
    elif full.exists():
        full.unlink()


def _prune_stale_worktree_metadata(root: Path) -> None:
    _git_run(root, "worktree", "prune")


def _cleanup_error(exc: Exception) -> str:
    detail = str(exc)
    if isinstance(exc, PermissionError) or re.search(
            r"permission denied|access (?:is )?denied|operation not permitted|拒絕存取", detail, re.IGNORECASE):
        return f"權限不足。若為沙箱建立的檔案，需要以較高權限刪除。{detail}"
    return detail


def describe_cleanup(cleanup_rec: Optional[dict]) -> str:
    """Human-readable summary of a cleanup_record for status, notes, and hook reports."""
    if not isinstance(cleanup_rec, dict) or not cleanup_rec.get("status"):
        return ""
    status = cleanup_rec["status"]
    if status == "cleaned":
        if cleanup_rec.get("branch_deleted"):
            return "已清理 worktree 與分支"
        return "已清理 worktree，分支保留"
    if status == "kept":
        return "保留 worktree 與分支"
    if status == "pending_retry":
        return "待重試"
    if status == "blocked":
        return "阻擋（未清理）"
    return str(status)


def cleanup_dispatch(
    root: Path,
    thread: Path,
    meta: dict,
    relay: Optional[dict] = None,
    state: Optional[dict] = None,
    *,
    delete_branch: bool = False,
) -> dict:
    """Safely clean a merged dispatch's worktree; keep branch by default unless `delete_branch=True`."""
    root = root.resolve()
    if state is None:
        state = completions.read_object(thread / "state.json") if (thread / "state.json").is_file() else {}
    dispatch_id = meta.get("dispatch_id") or ""
    branch = meta.get("branch") or ""
    source_branch = meta.get("source_branch") or ""
    now = _now_str()

    def _blocked(reason: str, tracked: Optional[List[str]] = None, blocking: Optional[List[str]] = None) -> dict:
        br_exists = ref_exists(root, f"refs/heads/{branch}") if branch else False
        rec = {
            "status": "blocked",
            "at": now,
            "worktree_removed": not Path(meta["worktree"]).exists() if meta.get("worktree") else True,
            "branch_deleted": not br_exists,
            "branch_kept": br_exists,
            "branch": branch,
            "commit_sha": meta.get("commit_sha"),
            "merge_sha": (meta.get("merge_record") or {}).get("merge_sha"),
            "reason": reason,
            "tracked_changes": list(tracked or []),
            "blocking_files": list(blocking or []),
        }
        if isinstance(meta.get("merge_record"), dict):
            meta["merge_record"]["branch_kept"] = br_exists
        meta["cleanup_record"] = rec
        _sync_dispatch_records(thread, meta)
        return rec

    def _pending_retry(error: str, wt_removed: bool, br_deleted: bool) -> dict:
        rec = {
            "status": "pending_retry",
            "at": now,
            "worktree_removed": wt_removed,
            "branch_deleted": br_deleted,
            "branch_kept": not br_deleted,
            "branch": branch,
            "commit_sha": meta.get("commit_sha"),
            "merge_sha": (meta.get("merge_record") or {}).get("merge_sha"),
            "error": error,
            "retryable": True,
        }
        if isinstance(meta.get("merge_record"), dict):
            meta["merge_record"]["branch_kept"] = not br_deleted
        meta["cleanup_record"] = rec
        _sync_dispatch_records(thread, meta)
        return rec

    # 1. Ownership & live-process checks
    try:
        worktree.validate_branch_ownership(meta)
    except worktree.WorktreeError as exc:
        return _blocked(str(exc))

    target = Path(meta["worktree"])
    expected_target = root / ".agora" / "worktrees" / dispatch_id
    if target.resolve() != expected_target.resolve():
        return _blocked("worktree 位置不符")

    active_reason = _ensure_no_live_process(state, relay, dispatch_id)
    if active_reason:
        return _blocked(active_reason)

    # 2. Merged ancestry check
    if not source_branch or not ref_exists(root, f"refs/heads/{source_branch}"):
        return _blocked(f"找不到來源分支 {source_branch}")

    branch_ref = f"refs/heads/{branch}"
    branch_exists = ref_exists(root, branch_ref)
    if branch_exists:
        branch_sha = worktree.git(root, "rev-parse", branch_ref)
        if meta.get("commit_sha") and branch_sha != meta["commit_sha"]:
            return _blocked("分支目前 SHA 與已驗證提交的 commit_sha 不符，拒絕清理")
        if not is_ancestor(root, branch_sha, f"refs/heads/{source_branch}"):
            return _blocked(f"分支 {branch} 尚未合併到 {source_branch}，拒絕清理")
    else:
        commit_sha = meta.get("commit_sha")
        if not commit_sha or not is_ancestor(root, commit_sha, f"refs/heads/{source_branch}"):
            return _blocked(f"找不到分支 {branch} 且無法確認成果已合併到 {source_branch}")

    # 3. Already cleaned?
    wt_exists = target.exists() or target.is_symlink()
    if not wt_exists and (not branch_exists or not delete_branch):
        branch_deleted = not branch_exists
        branch_kept = branch_exists
        rec = {
            "status": "cleaned",
            "at": (meta.get("cleanup_record") or {}).get("at") or now,
            "worktree_removed": True,
            "branch_deleted": branch_deleted,
            "branch_kept": branch_kept,
            "branch": branch,
            "commit_sha": meta.get("commit_sha"),
            "merge_sha": (meta.get("merge_record") or {}).get("merge_sha"),
            "merged": True,
        }
        if not (meta.get("merge_record") or {}).get("merged"):
            meta["merge_record"] = {
                "merged": True,
                "already_merged": True,
                "source_branch": source_branch,
                "branch": branch,
                "commit_sha": meta.get("commit_sha"),
                "detected_at": now,
                "branch_kept": branch_kept,
            }
        else:
            meta["merge_record"]["branch_kept"] = branch_kept
        meta["cleanup_record"] = rec
        _sync_dispatch_records(thread, meta)
        return rec

    # 4. Worktree validation, conservative artifact check, and worktree removal first
    removed_artifacts: List[str] = []
    if wt_exists:
        if target.is_symlink():
            return _blocked("worktree 為符號連結，拒絕自動清理")
        try:
            _verify_worktree_ownership(root, target, meta, branch_exists)
        except worktree.WorktreeError as exc:
            return _blocked(str(exc))
        try:
            files = classify_worktree_files(target, meta)
        except (worktree.WorktreeError, OSError) as exc:
            return _blocked(f"無法檢查 worktree 檔案狀態：{_cleanup_error(exc)}")
        if files["tracked"] or files["blocking"]:
            parts = []
            if files["tracked"]:
                parts.append("未提交的已追蹤變更：" + ", ".join(files["tracked"]))
            if files["blocking"]:
                parts.append("未知的未追蹤或被忽略檔案：" + ", ".join(files["blocking"]))
            return _blocked("；".join(parts), tracked=files["tracked"], blocking=files["blocking"])
        for entry in files["rebuildable"]:
            try:
                _remove_artifact(target, entry)
                removed_artifacts.append(entry)
            except (OSError, worktree.WorktreeError) as exc:
                return _pending_retry(f"移除可重建產物 {entry} 失敗（待重試）：{_cleanup_error(exc)}",
                                      False, not branch_exists)
        # Re-check before removing worktree (no --force).
        try:
            remaining = classify_worktree_files(target, meta)
        except (worktree.WorktreeError, OSError) as exc:
            return _pending_retry(f"移除產物後重檢 worktree 失敗（待重試）：{_cleanup_error(exc)}",
                                  False, not branch_exists)
        if remaining["tracked"] or remaining["blocking"] or remaining["rebuildable"]:
            leftover = remaining["tracked"] + remaining["blocking"] + remaining["rebuildable"]
            return _pending_retry(f"移除可重建產物後仍有殘留檔案（待重試）：{', '.join(leftover)}", False, not branch_exists)
        try:
            worktree.git(root, "worktree", "remove", str(target))
        except (PermissionError, OSError, worktree.WorktreeError) as exc:
            return _pending_retry(f"移除 worktree 失敗（待重試）：{_cleanup_error(exc)}",
                                  not target.exists(), not branch_exists)
    else:
        _prune_stale_worktree_metadata(root)

    # 5. Delete branch using safe `git branch -d` only when requested (after worktree is removed)
    branch_deleted = not branch_exists
    if delete_branch and branch_exists:
        try:
            worktree.git(root, "branch", "-d", branch)
            branch_deleted = True
        except (PermissionError, OSError, worktree.WorktreeError) as exc:
            return _pending_retry(f"刪除分支 {branch} 失敗（待重試）：{exc}", True, False)
    branch_kept = not branch_deleted

    # 6. Persist final cleaned record while preserving all control records
    if not (meta.get("merge_record") or {}).get("merged"):
        meta["merge_record"] = {
            "merged": True,
            "already_merged": True,
            "source_branch": source_branch,
            "branch": branch,
            "commit_sha": meta.get("commit_sha"),
            "detected_at": now,
            "branch_kept": branch_kept,
        }
    else:
        meta["merge_record"]["branch_kept"] = branch_kept
    rec = {
        "status": "cleaned",
        "at": now,
        "worktree_removed": True,
        "branch_deleted": branch_deleted,
        "branch_kept": branch_kept,
        "branch": branch,
        "commit_sha": meta.get("commit_sha"),
        "merge_sha": (meta.get("merge_record") or {}).get("merge_sha"),
        "merged": True,
        "removed_artifacts": removed_artifacts,
    }
    meta["cleanup_record"] = rec
    _sync_dispatch_records(thread, meta)
    return rec


def _check_summary(meta: dict) -> str:
    cr = meta.get("check_result") or {}
    if not cr:
        return "無紀錄"
    code = cr.get("exit_code")
    status = f"exit={code}"
    if code != 0 and meta.get("preexisting_approval"):
        status += "（已核准既有失敗）"
    tail = (cr.get("output_tail") or "").strip()
    summary_lines = [
        line.strip()
        for line in tail.splitlines()
        if line.strip().startswith(("Ran ", "OK", "FAILED", "語法檢查", "驗證結果"))
    ]
    if summary_lines:
        return f"{status}；{' / '.join(summary_lines[-3:])}"
    return status


def build_merge_message(topic: str, worker: Optional[str], thread_name: str, meta: dict) -> str:
    ai_label = parties.NAMES.get(worker or "", worker or "未知")
    title = (topic or thread_name).strip()
    header = f"合併：{title}（{ai_label}）"
    body_lines = [
        f"討論串：{thread_name}",
        f"原分支：{meta['branch']}",
        f"驗證結果摘要：{_check_summary(meta)}",
    ]
    trailers = [
        f"Agora-Dispatch: {meta['dispatch_id']}",
        f"Agora-Branch: {meta['branch']}",
    ]
    return header + "\n\n" + "\n".join(body_lines) + "\n\n" + "\n".join(trailers) + "\n"


def _collect_environment_summary(python: str, command, cwd: Path, env: dict) -> dict:
    return parties.redact({
        "agora_version": __version__,
        "python": python,
        "command": command,
        "cwd": str(cwd),
        "stdout_encoding": getattr(sys.stdout, "encoding", None) or "<未知>",
        "locale_encoding": locale.getpreferredencoding(False) or "<未知>",
        "env": {
            key: (env[key] if key in env else "<未設定>")
            for key in ENV_SUMMARY_KEYS
        },
    })


def _extract_failed_tests(output: str, check_command, exit_code: Optional[int]) -> Tuple[List[str], bool, Optional[str]]:
    if not output:
        return [], False, None
    code = 1 if exit_code is None else exit_code
    frameworks: List[str] = []
    if check_command:
        try:
            selected = baseline.plan(check_command)
            frameworks.append(selected["framework"])
        except ValueError:
            label = check_command if isinstance(check_command, str) else " ".join(str(x) for x in check_command)
            if "pytest" in label.lower():
                frameworks.append("pytest")
            if "unittest" in label.lower():
                frameworks.append("unittest")
    for fw in ("unittest", "pytest"):
        if fw not in frameworks:
            frameworks.append(fw)
    best: Tuple[List[str], bool, Optional[str]] = ([], False, None)
    for fw in frameworks:
        parsed = baseline.parse(output, fw, code)
        if parsed["complete"]:
            return parsed["ids"], True, fw
        if parsed["ids"] and not best[0]:
            best = (parsed["ids"], False, fw)
    return best


def _format_check_evidence(check_result: dict) -> List[str]:
    lines: List[str] = []
    failed = check_result.get("failed_tests") or []
    if failed:
        total = len(failed)
        shown = failed[:FAILED_TESTS_PREVIEW_LIMIT]
        if total > FAILED_TESTS_PREVIEW_LIMIT:
            lines.append(f"失敗／錯誤測試（共 {total} 項，列出前 {FAILED_TESTS_PREVIEW_LIMIT} 項）：")
        else:
            lines.append(f"失敗／錯誤測試（共 {total} 項）：")
        for test_id in shown:
            lines.append(f"  - {test_id}")
        if total > FAILED_TESTS_PREVIEW_LIMIT:
            log_path = check_result.get("test_log") or check_result.get("log") or ""
            suffix = f"：{log_path}" if log_path else ""
            lines.append(f"  …其餘 {total - FAILED_TESTS_PREVIEW_LIMIT} 項請見完整紀錄{suffix}")
    log_path = check_result.get("log")
    test_log_path = check_result.get("test_log")
    if log_path and test_log_path:
        lines.append(f"完整紀錄：{log_path}｜測試輸出：{test_log_path}")
    elif log_path:
        lines.append(f"完整紀錄：{log_path}")
    elif test_log_path:
        lines.append(f"測試輸出：{test_log_path}")
    timing = check_result.get("timing") or {}
    if timing.get("wait_seconds") is not None or timing.get("run_seconds") is not None:
        parts = []
        if timing.get("wait_seconds") is not None:
            parts.append(f"等待槽位 {timing['wait_seconds']:.2f}s")
        if timing.get("run_seconds") is not None:
            parts.append(f"實際執行 {timing['run_seconds']:.2f}s")
        if timing.get("total_seconds") is not None:
            parts.append(f"總計 {timing['total_seconds']:.2f}s")
        lines.append("驗證耗時：" + "，".join(parts))
    return lines


def _main_check_environment(root: Path, record_path: Optional[Path] = None) -> Tuple[str, dict]:
    env = dict(os.environ)

    def in_dispatch(path: str) -> bool:
        if not path:
            return False
        absolute = Path(path).absolute()
        return config.resolve_control_root(absolute) != absolute

    python = sys.executable
    if in_dispatch(python):
        python = sys._base_executable
    for key in ("PATH", "PYTHONPATH"):
        if key in env:
            env[key] = os.pathsep.join(path for path in env[key].split(os.pathsep) if not in_dispatch(path))
    if in_dispatch(env.get("VIRTUAL_ENV", "")):
        env.pop("VIRTUAL_ENV")
        env.pop("PYTHONNOUSERSITE", None)
    for key in (
        worktree.WORK_ENV,
        worktree.CONTROL_ENV,
        worktree.DISPATCH_ENV,
        worktree.MANIFEST_ENV,
        parties.LOCKED_ENV,
        parties.NESTED_ENV,
        baseline.RECORD_ENV,
    ):
        env.pop(key, None)
    env["AGORA_WORKSPACE"] = str(root)
    env[worktree.INTEGRATION_CHECK_ENV] = "1"
    if record_path is not None:
        env[baseline.RECORD_ENV] = str(record_path)
    return python, env


def run_main_check(root: Path, record_dir: Optional[Path] = None) -> dict:
    """Run `agora check` once in the main workspace as post-merge integration verification."""
    target_dir = Path(record_dir) if record_dir is not None else (root / ".agora" / "integration-checks")
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    stem = f"integration-check-{stamp}-{uuid.uuid4().hex[:8]}"
    record = target_dir / f"{stem}.json"
    log = target_dir / f"{stem}.log"
    test_log = target_dir / f"{stem}.tests.log"

    python, env = _main_check_environment(root, record_path=record)
    command = [python, str(parties.AGORA_ROOT / "agora.py"), "--workspace", str(root), "check"]
    outer_start = time.monotonic()
    proc = subprocess.run(
        command,
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        **quota.NO_WINDOW,
    )
    total_seconds = round(max(0.0, time.monotonic() - outer_start), 3)
    output = proc.stdout + proc.stderr
    evidence = completions.read_object(record)
    raw = evidence.get("output", output)
    log.write_text(output, encoding="utf-8")
    test_log.write_text(raw, encoding="utf-8")

    check_command = evidence.get("check_command")
    test_exit_code = evidence.get("exit_code")
    failed_tests, failed_complete, failed_framework = _extract_failed_tests(
        raw, check_command, test_exit_code if test_exit_code is not None else proc.returncode
    )

    env_summary = evidence.get("environment")
    if not isinstance(env_summary, dict):
        env_summary = _collect_environment_summary(python, check_command or command, root, env)
    else:
        env_summary = parties.redact(env_summary)

    timing = {
        "wait_seconds": evidence.get("wait_seconds"),
        "run_seconds": evidence.get("run_seconds"),
        "total_seconds": total_seconds,
    }
    return {
        "exit_code": proc.returncode,
        "command": parties.redact(command),
        "check_command": parties.redact(check_command) if check_command is not None else None,
        "time": _now_str(),
        "log": str(log),
        "test_log": str(test_log),
        "record": str(record) if record.is_file() else None,
        "output_tail": output[-3000:],
        "complete": evidence.get("complete", False),
        "syntax_errors": evidence.get("syntax_errors"),
        "test_exit_code": test_exit_code,
        "failed_tests": failed_tests,
        "failed_tests_complete": failed_complete,
        "failed_tests_framework": failed_framework,
        "environment": env_summary,
        "timing": timing,
    }


def _load_target_dispatch(ws, thread: Path, state: dict, dispatch_id: Optional[str]) -> Tuple[str, dict, dict, dict]:
    latest = state.get("relay") or {}
    key = dispatch_id or latest.get("dispatch_id")
    if not key or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", key):
        raise MergeError("沒有有效的隔離分派 ID，請確認討論串或 --dispatch-id")
    directory = thread / "dispatches" / key
    meta = completions.read_object(directory / "manifest.json")
    if (
        meta.get("dispatch_id") != key
        or meta.get("control_root") != str(ws.control_root.resolve())
        or meta.get("thread") != str(thread.resolve())
        or meta.get("manifest") != str(directory / "manifest.json")
        or meta.get("commit_message") != str(directory / "commit-message.md")
    ):
        raise MergeError("隔離分派紀錄與工作區／討論串不符，請檢查 manifest")
    relay = latest if latest.get("dispatch_id") == key else completions.read_object(directory / "relay.json")
    if relay:
        try:
            meta = worktree.load(relay)
        except worktree.WorktreeError as exc:
            raise MergeError(str(exc)) from exc
    return key, meta, relay, latest


def _unmerged_files(root: Path) -> List[str]:
    proc = _git_run(root, "diff", "--name-only", "--diff-filter=U", "-z")
    if proc.returncode != 0 or not proc.stdout:
        return []
    return sorted(p for p in proc.stdout.split("\0") if p)


def _merged_tree_preview(root: Path, commit_sha: str) -> Tuple[Optional[str], Optional[str], bool]:
    """Compute merged tree via `git merge-tree --write-tree HEAD <commit_sha>`.

    Returns `(tree_sha, unsupported_reason, has_conflict)`.
    """
    try:
        proc = _git_run(root, "merge-tree", "--write-tree", "HEAD", commit_sha)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"無法執行 git merge-tree（{exc}）", False
    stdout = proc.stdout or ""
    stderr = proc.stderr or ""
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    first = lines[0] if lines else ""
    is_oid = bool(re.fullmatch(r"[0-9a-fA-F]{40,64}", first))
    if proc.returncode == 0 and is_oid:
        return first, None, False
    combined = (stdout + "\n" + stderr).strip()
    if proc.returncode == 1 and (is_oid or "CONFLICT" in combined):
        return None, None, True
    detail = [line.strip() for line in (stderr or stdout).splitlines() if line.strip()]
    reason = detail[0][:200] if detail else f"exit={proc.returncode}"
    return None, f"git merge-tree --write-tree 無法使用（{reason}）", False


def _read_tree_blob(root: Path, tree_sha: str, rel_path: str) -> Optional[str]:
    """Read a blob file from a git tree object; return None if missing or not a blob."""
    proc = _git_run(root, "cat-file", "blob", f"{tree_sha}:{rel_path}")
    return proc.stdout if proc.returncode == 0 else None


def merge_dispatch(
    ws,
    thread: Path,
    dispatch_id: Optional[str] = None,
    *,
    keep: bool = False,
    delete_branch: bool = False,
    run_check: Optional[Callable[[Path], dict]] = None,
) -> dict:
    """Validate preconditions, merge with `--no-ff`, run integration check, and clean up worktree."""
    from agoralib import cli

    if keep and delete_branch:
        raise MergeError("--keep 與 --delete-branch 不能同時使用")
    if os.getenv(worktree.WORK_ENV) or ws.root.resolve() != ws.control_root.resolve():
        raise MergeError("agora merge 必須在主工作區執行，不能在分派 worktree 內執行")
    root = ws.control_root.resolve()

    with shares.dispatch_lock(root), cli.thread_lock(thread, wait=cli.WORKER_LOCK_WAIT):
        state = cli.load_state(thread)
        key, meta, relay, latest = _load_target_dispatch(ws, thread, state, dispatch_id)

        # 1. Verify relay finished and no live process
        active_reason = _ensure_no_live_process(state, relay, key)
        if active_reason:
            raise MergeError(f"{active_reason}，請等程序結束或先 recall 後再合併")
        if relay and relay.get("status") in cli.RELAY_ACTIVE:
            raise MergeError(f"分派尚未結束（狀態：{relay.get('status')}），拒絕合併")
        if latest.get("dispatch_id") == key and latest.get("status") in cli.RELAY_ACTIVE:
            raise MergeError(f"分派尚未結束（狀態：{latest.get('status')}），拒絕合併")

        # 2. Verify commit_status=committed, branch ownership, and branch SHA == commit_sha
        if meta.get("commit_status") != "committed" or not meta.get("commit_sha"):
            raise MergeError(
                f"此分派尚未完成驗證與提交（commit_status={meta.get('commit_status') or '未提交'}），不可合併"
            )
        try:
            worktree.validate_branch_ownership(meta)
        except worktree.WorktreeError as exc:
            raise MergeError(str(exc)) from exc

        branch = meta["branch"]
        commit_sha = meta["commit_sha"]
        source_branch = meta.get("source_branch") or ""

        # 3. Verify main workspace branch == source_branch
        try:
            current_branch = worktree.git(root, "symbolic-ref", "--quiet", "--short", "HEAD")
        except worktree.WorktreeError as exc:
            raise MergeError(f"主工作區目前為 detached HEAD，請先切回 {source_branch} 再合併") from exc
        if not current_branch or current_branch != source_branch:
            raise MergeError(f"主工作區目前分支為 {current_branch or '(detached)'}，與分派來源分支 {source_branch} 不符")

        # 4. Verify no in-progress merge / rebase / cherry-pick
        ops = in_progress_git_ops(root)
        if ops:
            raise MergeError(f"主工作區有進行中的 Git 操作（{'／'.join(ops)}），請先完成或中止後再合併")

        # 5. Verify main workspace has no uncommitted tracked changes
        dirty = _tracked_dirty_paths(root)
        if dirty:
            raise MergeError("主工作區有未提交的已追蹤變更，請先提交或處理後再合併：" + ", ".join(dirty))

        # 6. Verify branch ref SHA matches committed commit_sha (unless already merged & deleted)
        branch_ref = f"refs/heads/{branch}"
        branch_exists = ref_exists(root, branch_ref)
        if branch_exists:
            branch_sha = worktree.git(root, "rev-parse", branch_ref)
            if branch_sha != commit_sha:
                raise MergeError(
                    f"分支 {branch} 目前 SHA（{branch_sha[:12]}）與已驗證提交的 commit_sha（{commit_sha[:12]}）不符，拒絕合併"
                )
        else:
            if not is_ancestor(root, commit_sha, "HEAD"):
                raise MergeError(f"找不到分派分支 {branch}，無法合併")

        already_merged = is_ancestor(root, commit_sha, "HEAD")
        prior_merge = meta.get("merge_record") or {}
        merge_sha: Optional[str] = prior_merge.get("merge_sha")
        topic = state.get("topic") or thread.name
        worker = meta.get("worker") or (relay or {}).get("worker") or meta.get("initial_worker")

        # 7. Perform `git merge --no-ff` only if not already merged
        preflight_warnings: List[str] = []
        if not already_merged:
            main_ws = cli.Workspace(root)
            incoming = {}
            if not main_ws.config().get("check"):
                changed = worktree.git(root, "diff", "--name-only", "--no-renames", "-z",
                                       meta["base_sha"], commit_sha, "--", *cli.PYTEST_CONFIG_FILES)
                for path in filter(None, changed.split("\0")):
                    before = _git_run(root, "show", f"{meta['base_sha']}:{path}")
                    after = _git_run(root, "show", f"{commit_sha}:{path}")
                    # A dispatch that changes an unrelated pyproject section
                    # must not mask a pytest section already added on main.
                    marker = cli.PYTEST_CONFIG_FILES[path]
                    if marker is None or ((marker in before.stdout) != (marker in after.stdout)):
                        incoming[path] = after.stdout if after.returncode == 0 else None
            tree_sha, tree_error, _tree_conflict = _merged_tree_preview(root, commit_sha)
            tree_reader = (lambda rel_path, _t=tree_sha: _read_tree_blob(root, _t, rel_path)) if tree_sha else None
            python, env = _main_check_environment(root)
            try:
                preflight_warnings = cli.preflight_integration_check(
                    main_ws,
                    python,
                    env,
                    incoming,
                    Path(meta["worktree"]),
                    merged_tree_reader=tree_reader,
                    merged_tree_error=tree_error,
                ) or []
            except ValueError as exc:
                raise MergeError(str(exc)) from exc
            msg = build_merge_message(topic, worker, thread.name, meta)
            with tempfile.TemporaryDirectory(prefix="agora-merge-") as tmp:
                msg_file = Path(tmp) / "merge-message.txt"
                msg_file.write_text(msg, encoding="utf-8")
                proc = _git_run(root, "merge", "--no-ff", "-F", str(msg_file), branch)
            if proc.returncode != 0:
                conflicts = _unmerged_files(root)
                gdir = _git_dir(root)
                aborted = False
                if (gdir / "MERGE_HEAD").exists():
                    abort_proc = _git_run(root, "merge", "--abort")
                    aborted = abort_proc.returncode == 0
                detail = (proc.stderr or proc.stdout).strip()[:500]
                if conflicts:
                    raise MergeError(
                        f"合併發生衝突，已執行 git merge --abort（不清理 worktree／分支）：{', '.join(conflicts)}"
                        + (f"\n{detail}" if detail else "")
                    )
                raise MergeError(
                    f"git merge --no-ff 失敗"
                    + ("（已中止合併）" if aborted else "")
                    + f"（不清理 worktree／分支）：{detail}"
                )
            merge_sha = worktree.git(root, "rev-parse", "HEAD")
            meta["merge_record"] = {
                "merged": True,
                "already_merged": False,
                "at": _now_str(),
                "source_branch": source_branch,
                "branch": branch,
                "commit_sha": commit_sha,
                "merge_sha": merge_sha,
                "worker": worker,
                "check_result": None,
                "check_passed": None,
                "preflight_warnings": preflight_warnings,
            }
            worktree.save(meta)
        else:
            if not prior_merge.get("merged"):
                meta["merge_record"] = {
                    "merged": True,
                    "already_merged": True,
                    "at": _now_str(),
                    "source_branch": source_branch,
                    "branch": branch,
                    "commit_sha": commit_sha,
                    "merge_sha": merge_sha,
                    "worker": worker,
                    "check_result": prior_merge.get("check_result"),
                    "check_passed": prior_merge.get("check_passed"),
                }
                worktree.save(meta)

    # 8. Post-merge integration check in main workspace (outside locks so checkqueue/recall work normally)
    need_check = (not already_merged) or (prior_merge.get("check_passed") is not True)
    check_result = prior_merge.get("check_result")
    check_passed = prior_merge.get("check_passed") is True
    if need_check:
        integration_sha = merge_sha or worktree.git(root, "rev-parse", "HEAD")
        dispatch_dir = Path(meta["manifest"]).parent
        try:
            check_result = run_check(root) if run_check is not None else run_main_check(root, record_dir=dispatch_dir)
        except (OSError, subprocess.SubprocessError) as exc:
            check_result = {"exit_code": 1, "output_tail": f"無法執行整合檢查：{exc}"}
        check_passed = check_result.get("exit_code") == 0
        meta["merge_record"]["check_result"] = check_result
        meta["merge_record"]["check_passed"] = check_passed
        _sync_dispatch_records(thread, meta)
        note_lines = [
            f"{'✅' if check_passed else '⚠️'} agora merge 整合驗證"
            f"（分派 {key}｜branch：{branch}｜merge：{merge_sha or '已合併'}｜exit={check_result.get('exit_code')}）"
        ]
        if not check_passed:
            note_lines.insert(0, f"合併已完成（SHA：{integration_sha}），只有整合檢查失敗。")
            note_lines.append("不回滾合併，保留 worktree 與分支，請修正後再處理。")
        note_lines.extend(_format_check_evidence(check_result))
        tail = (check_result.get("output_tail") or "").strip()
        if tail:
            note_lines.append("\n".join(tail.splitlines()[-10:]))
        note_text = "\n".join(note_lines)
        cli.add_system_note(thread, note_text)
        cli.append_progress(thread, "agora merge 整合驗證", note_text)

        if not check_passed:
            meta["merge_record"]["branch_kept"] = ref_exists(root, f"refs/heads/{branch}")
            _sync_dispatch_records(thread, meta)
            return {
                "ok": False,
                "dispatch_id": key,
                "branch": branch,
                "source_branch": source_branch,
                "commit_sha": commit_sha,
                "merge_sha": merge_sha,
                "integration_sha": integration_sha,
                "already_merged": already_merged,
                "check_result": check_result,
                "check_passed": False,
                "check_ran": True,
                "preflight_warnings": preflight_warnings,
                "cleanup": {"status": "skipped_check_failed", "reason": "整合驗證失敗，不回滾合併且不清理"},
            }
    else:
        _sync_dispatch_records(thread, meta)

    # 9. Cleanup (default removes worktree and keeps branch unless --keep or --delete-branch)
    if keep:
        branch_exists_now = ref_exists(root, f"refs/heads/{branch}")
        meta["merge_record"]["branch_kept"] = branch_exists_now
        cleanup_rec = {
            "status": "kept",
            "at": _now_str(),
            "worktree_removed": not Path(meta["worktree"]).exists(),
            "branch_deleted": not branch_exists_now,
            "branch_kept": branch_exists_now,
            "branch": branch,
            "commit_sha": commit_sha,
            "merge_sha": merge_sha,
            "reason": "--keep",
        }
        meta["cleanup_record"] = cleanup_rec
        _sync_dispatch_records(thread, meta)
    else:
        cleanup_rec = cleanup_dispatch(root, thread, meta, relay=relay, delete_branch=delete_branch)

    summary_note = (
        f"🔀 agora merge 完成：分派 {key}（branch：{branch} → {source_branch}，"
        + ("已合併，未建立空節點" if already_merged else f"merge SHA：{(merge_sha or '')[:12]}")
        + f"；清理狀態：{describe_cleanup(cleanup_rec) or cleanup_rec.get('status')}）"
    )
    if preflight_warnings:
        summary_note += "\n" + "\n".join(preflight_warnings)
    cli.add_system_note(thread, summary_note)
    cli.append_progress(thread, "agora merge", summary_note)

    return {
        "ok": True,
        "dispatch_id": key,
        "branch": branch,
        "source_branch": source_branch,
        "commit_sha": commit_sha,
        "merge_sha": merge_sha,
        "already_merged": already_merged,
        "check_result": check_result,
        "check_passed": check_passed,
        "check_ran": need_check,
        "preflight_warnings": preflight_warnings,
        "cleanup": cleanup_rec,
    }


def _thread_dispatches(thread: Path, state: dict) -> List[Tuple[str, dict, Optional[dict]]]:
    """Return all recorded isolated dispatches in a thread: [(dispatch_id, meta, relay)]."""
    found: Dict[str, Tuple[dict, Optional[dict]]] = {}
    latest = state.get("relay") or {}
    dispatches_dir = thread / "dispatches"
    if dispatches_dir.is_dir():
        for manifest_path in sorted(dispatches_dir.glob("*/manifest.json")):
            try:
                meta = completions.read_object(manifest_path)
            except (OSError, ValueError):
                continue
            did = meta.get("dispatch_id")
            if not did:
                continue
            relay = latest if latest.get("dispatch_id") == did else None
            if relay is None:
                relay_path = manifest_path.parent / "relay.json"
                if relay_path.is_file():
                    try:
                        relay = completions.read_object(relay_path)
                    except (OSError, ValueError):
                        relay = None
            found[did] = (meta, relay)
    if latest.get("worktree") and latest.get("dispatch_id") and latest["dispatch_id"] not in found:
        found[latest["dispatch_id"]] = (latest, latest)
    return [(did, pair[0], pair[1]) for did, pair in found.items()]


def _dispatch_is_merged_and_cleaned(meta: dict) -> bool:
    """True when a dispatch has been confirmed merged and its worktree is gone (branch may be kept)."""
    cleanup = meta.get("cleanup_record") or {}
    merge_rec = meta.get("merge_record") or {}
    if cleanup.get("status") != "cleaned":
        return False
    if not (merge_rec.get("merged") or cleanup.get("merged")):
        return False
    wt = meta.get("worktree")
    if wt and (Path(wt).exists() or Path(wt).is_symlink()):
        return False
    return True


def thread_dispatch_keep_reason(thread: Path, state: dict) -> Optional[str]:
    """Return keep reason for `prune.why_keep`, or None if no uncleaned/active isolated dispatch blocks."""
    finalizer = state.get("finalizer") or {}
    if jobs.alive(finalizer):
        return "分派程序尚未退出"
    dispatches = _thread_dispatches(thread, state)
    if not dispatches:
        relay = state.get("relay") or {}
        if relay.get("worktree"):
            return "保留隔離分派成果與控制紀錄；merge／清理須另行指派"
        return None
    for did, meta, relay in dispatches:
        if _ensure_no_live_process(state, relay, did):
            return "分派程序尚未退出"
        if not _dispatch_is_merged_and_cleaned(meta):
            return "保留隔離分派成果與控制紀錄；merge／清理須另行指派"
    return None


def scan_merged_dispatches(ws, threads: List[Path], *, delete_branch: bool = False) -> Tuple[List[dict], List[dict]]:
    """Inspect all historical dispatches across threads for `agora prune --merged`."""
    root = ws.control_root.resolve()
    candidates: List[dict] = []
    kept: List[dict] = []
    for thread in threads:
        try:
            state = completions.read_object(thread / "state.json")
        except (OSError, ValueError):
            continue
        topic = state.get("topic", "")
        for did, meta, relay in _thread_dispatches(thread, state):
            if not meta.get("worktree") or not meta.get("branch"):
                continue
            if meta.get("control_root") and Path(meta["control_root"]).resolve() != root:
                continue
            target = Path(meta["worktree"])
            branch = meta["branch"]
            source_branch = meta.get("source_branch") or ""
            wt_exists = target.exists() or target.is_symlink()
            br_exists = ref_exists(root, f"refs/heads/{branch}")
            if not wt_exists and (not br_exists or not delete_branch):
                continue  # Already cleaned; nothing to prune.
            info = {
                "thread": thread.name,
                "topic": topic,
                "dispatch_id": did,
                "branch": branch,
                "source_branch": source_branch,
                "worktree": str(target),
                "worktree_exists": wt_exists,
                "branch_exists": br_exists,
                "commit_sha": meta.get("commit_sha"),
            }
            active_reason = _ensure_no_live_process(state, relay, did)
            if active_reason:
                kept.append({**info, "reason": active_reason})
                continue
            try:
                worktree.validate_branch_ownership(meta)
            except worktree.WorktreeError as exc:
                kept.append({**info, "reason": str(exc)})
                continue
            if target.resolve() != (root / ".agora" / "worktrees" / did).resolve():
                kept.append({**info, "reason": "worktree 位置不符"})
                continue
            if not source_branch or not ref_exists(root, f"refs/heads/{source_branch}"):
                kept.append({**info, "reason": f"找不到來源分支 {source_branch}"})
                continue
            if br_exists:
                branch_sha = worktree.git(root, "rev-parse", f"refs/heads/{branch}")
                if meta.get("commit_sha") and branch_sha != meta["commit_sha"]:
                    kept.append({**info, "reason": "分支目前 SHA 與已提交 commit_sha 不符"})
                    continue
                if not is_ancestor(root, branch_sha, f"refs/heads/{source_branch}"):
                    kept.append({**info, "reason": f"尚未合併到 {source_branch}"})
                    continue
            else:
                commit_sha = meta.get("commit_sha")
                if not commit_sha or not is_ancestor(root, commit_sha, f"refs/heads/{source_branch}"):
                    kept.append({**info, "reason": f"無法確認已合併到 {source_branch}"})
                    continue
            if wt_exists:
                if target.is_symlink():
                    kept.append({**info, "reason": "worktree 為符號連結"})
                    continue
                try:
                    _verify_worktree_ownership(root, target, meta, br_exists)
                    files = classify_worktree_files(target, meta)
                except (worktree.WorktreeError, OSError) as exc:
                    kept.append({**info, "reason": _cleanup_error(exc)})
                    continue
                if files["tracked"] or files["blocking"]:
                    parts = []
                    if files["tracked"]:
                        parts.append("未提交的已追蹤變更：" + ", ".join(files["tracked"]))
                    if files["blocking"]:
                        parts.append("未知的未追蹤或被忽略檔案：" + ", ".join(files["blocking"]))
                    kept.append({
                        **info,
                        "reason": "；".join(parts),
                        "tracked_changes": files["tracked"],
                        "blocking_files": files["blocking"],
                    })
                    continue
                info["rebuildable_artifacts"] = files["rebuildable"]
            candidates.append(info)
    return candidates, kept


def prune_merged_dispatches(ws, threads: List[Path], *, yes: bool = False, delete_branch: bool = False) -> dict:
    """List or clean merged-but-uncleaned dispatch resources (never deletes threads or sessions)."""
    candidates, kept = scan_merged_dispatches(ws, threads, delete_branch=delete_branch)
    result = {
        "mode": "merged",
        "delete_branch": delete_branch,
        "candidates": candidates,
        "kept": kept,
        "cleaned": [],
        "pending_retry": [],
        "blocked": [],
        "errors": [],
        "dry_run": not yes,
    }
    if not yes:
        return result
    root = ws.control_root.resolve()
    for info in candidates:
        thread = ws.threads / info["thread"]
        manifest_path = thread / "dispatches" / info["dispatch_id"] / "manifest.json"
        try:
            meta = completions.read_object(manifest_path)
        except (OSError, ValueError) as exc:
            result["errors"].append(f"{info['thread']} / {info['dispatch_id']}：無法讀取 manifest：{exc}")
            continue
        state = completions.read_object(thread / "state.json") if (thread / "state.json").is_file() else {}
        relay_path = manifest_path.parent / "relay.json"
        relay = (state.get("relay") if (state.get("relay") or {}).get("dispatch_id") == info["dispatch_id"]
                 else (completions.read_object(relay_path) if relay_path.is_file() else None))
        outcome = cleanup_dispatch(root, thread, meta, relay=relay, state=state, delete_branch=delete_branch)
        entry = {**info, "cleanup": outcome}
        if outcome["status"] == "cleaned":
            result["cleaned"].append(entry)
        elif outcome["status"] == "pending_retry":
            result["pending_retry"].append(entry)
            result["errors"].append(f"{info['thread']} / {info['dispatch_id']}：{outcome.get('error')}")
        else:
            result["blocked"].append(entry)
            result["errors"].append(f"{info['thread']} / {info['dispatch_id']}：{outcome.get('reason')}")
    return result


def cmd_merge(ws, args) -> None:
    """CLI handler for `agora merge <thread> [--dispatch-id <id>] [--keep | --delete-branch]`."""
    from agoralib import cli

    cli.guard_nested()
    thread = ws.resolve_thread(args.thread)
    delete_branch = bool(getattr(args, "delete_branch", False))
    try:
        result = merge_dispatch(
            ws,
            thread,
            dispatch_id=getattr(args, "dispatch_id", None),
            keep=bool(getattr(args, "keep", False)),
            delete_branch=delete_branch,
        )
    except (MergeError, worktree.WorktreeError, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        sys.exit(f"merge 拒絕或失敗：{exc}")

    if getattr(args, "json", False):
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if not result.get("ok"):
            sys.exit(1)
        return

    if not result.get("ok"):
        for warning in result.get("preflight_warnings") or []:
            print(f"⚠️ {warning}")
        cr = result.get("check_result") or {}
        print(
            f"⚠️ 合併已完成（SHA：{result['integration_sha']}），只有整合檢查失敗"
            f"（exit={cr.get('exit_code')}）。"
        )
        print("已保留 worktree 與分支。")
        for line in _format_check_evidence(cr):
            print(line)
        tail = (cr.get("output_tail") or "").strip()
        if tail:
            print(tail)
        sys.exit(1)

    for warning in result.get("preflight_warnings") or []:
        print(f"⚠️ {warning}")
    if result["already_merged"]:
        print(f"分支 {result['branch']} 已是 {result['source_branch']} 的祖先，略過建立合併節點。")
    else:
        print(f"已以 --no-ff 合併：{result['branch']} → {result['source_branch']}（merge SHA：{result['merge_sha']}）")
    if result.get("check_result") is not None and (result.get("check_ran") or not result["already_merged"]):
        cr = result["check_result"]
        timing = cr.get("timing") or {}
        timing_str = ""
        if timing.get("wait_seconds") is not None or timing.get("run_seconds") is not None:
            parts = []
            if timing.get("wait_seconds") is not None:
                parts.append(f"等待槽位 {timing['wait_seconds']:.2f}s")
            if timing.get("run_seconds") is not None:
                parts.append(f"實際執行 {timing['run_seconds']:.2f}s")
            timing_str = f"（{'，'.join(parts)}）"
        print(f"整合驗證通過（exit={cr.get('exit_code')}）{timing_str}。")
        if cr.get("log"):
            print(f"完整紀錄：{cr['log']}")

    cleanup = result.get("cleanup") or {}
    c_status = cleanup.get("status")
    if c_status == "cleaned":
        if cleanup.get("branch_deleted"):
            print(f"已清理 worktree 與分支 {result['branch']}（控制紀錄保留）。")
        else:
            print(f"已清理 worktree，分支 {result['branch']} 保留（控制紀錄保留）。")
    elif c_status == "kept":
        print(f"依 --keep 保留 worktree 與分支 {result['branch']}。")
    elif c_status == "pending_retry":
        print(f"⚠️ 清理未完成（待重試）：{cleanup.get('error')}")
        flag = " --delete-branch" if delete_branch else ""
        print(f"已完成的合併不回滾；解除檔案鎖定後可重新執行 agora merge {thread.name}{flag} 或 agora prune --merged{flag} --yes 重試。")
    elif c_status == "blocked":
        target_label = "worktree／分支" if delete_branch else "worktree"
        print(f"⚠️ 未清理 {target_label}：{cleanup.get('reason')}")
        for p in cleanup.get("tracked_changes") or []:
            print(f"  [已追蹤變更] {p}")
        for p in cleanup.get("blocking_files") or []:
            print(f"  [未追蹤／忽略] {p}")


def cmd_prune_merged(ws, args) -> None:
    """CLI handler for `agora prune --merged` (cleans only merged dispatch resources, never threads)."""
    from agoralib import cli

    if getattr(args, "yes", False):
        cli.guard_nested()
    if getattr(args, "thread", None):
        threads = [ws.resolve_thread(t) for t in args.thread]
    else:
        threads = sorted(p for p in ws.threads.glob("*") if (p / "state.json").exists()) if ws.threads.exists() else []
    delete_branch = bool(getattr(args, "delete_branch", False))
    result = prune_merged_dispatches(ws, threads, yes=bool(getattr(args, "yes", False)), delete_branch=delete_branch)
    if getattr(args, "json", False):
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    candidates = result["candidates"]
    kept = result["kept"]
    if not candidates:
        print("沒有可清理的已合併分派資源（不會刪除討論串與控制紀錄）。")
    elif args.yes:
        if delete_branch:
            print(f"已清理 {len(result['cleaned'])} 個已合併分派資源（含刪除分支；討論串與控制紀錄已保留）：")
        else:
            print(f"已清理 {len(result['cleaned'])} 個已合併分派的 worktree（分支、討論串與控制紀錄已保留）：")
        for info in result["cleaned"]:
            print(f"  • {info['thread']} / {info['dispatch_id']}｜branch：{info['branch']}｜{describe_cleanup(info['cleanup'])}")
        for info in result["pending_retry"]:
            print(f"  ⏳ 待重試：{info['thread']} / {info['dispatch_id']}｜{info['cleanup'].get('error')}")
        for info in result["blocked"]:
            print(f"  ⚠️ 阻擋：{info['thread']} / {info['dispatch_id']}｜{info['cleanup'].get('reason')}")
    else:
        scope_desc = "移除 worktree 與分支" if delete_branch else "僅移除 worktree，分支保留"
        print(f"以下 {len(candidates)} 個已合併分派資源可以清理（{scope_desc}，不刪除討論串）：")
        for info in candidates:
            print(f"  • {info['thread']} / {info['dispatch_id']}｜branch：{info['branch']}｜source：{info['source_branch']}")
        print("\n確認後加上 --yes 清理以上分派資源。")
    for e in result["errors"]:
        print(f"⚠️ {e}")
    if kept and (not getattr(args, "yes", False) or getattr(args, "verbose", False)):
        print("\n保留（不清理）：")
        for info in kept:
            print(f"  • {info['thread']} / {info['dispatch_id']}｜branch：{info['branch']}｜{info['reason']}")
    elif kept:
        print(f"（另有 {len(kept)} 個分派資源保留，加 --verbose 查看原因）")
