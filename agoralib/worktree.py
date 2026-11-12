"""Dispatch-owned Git resources. Never merge, overwrite, or remove task results.

The manifest lives in the controller, independently of a thread's latest relay.
Checks fail closed; failed checks leave the branch and worktree for review.
"""

import fnmatch
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import venv
from pathlib import Path

from agoralib import quota, snapshot

CONTROL_ENV = "AGORA_CONTROL_ROOT"
WORK_ENV = "AGORA_WORKTREE"
DISPATCH_ENV = "AGORA_DISPATCH_ID"
MANIFEST_ENV = "AGORA_DISPATCH_MANIFEST"
FIELDS = ("dispatch_id", "source_branch", "base_sha", "branch", "worktree", "control_root",
          "manifest", "commit_message", "check_result", "commit_sha", "commit_status", "environment_status")


class WorktreeError(RuntimeError):
    pass


def git_environment(env, roots):
    """Trust only the task's two repositories in child processes, never globally."""
    env = dict(env)
    count = int(env.get("GIT_CONFIG_COUNT", "0"))
    existing = {env.get(f"GIT_CONFIG_VALUE_{i}") for i in range(count)
                if env.get(f"GIT_CONFIG_KEY_{i}") == "safe.directory"}
    for root in roots:
        path = Path(root).resolve().as_posix()
        if path not in existing:
            env[f"GIT_CONFIG_KEY_{count}"] = "safe.directory"
            env[f"GIT_CONFIG_VALUE_{count}"] = path
            existing.add(path)
            count += 1
    env["GIT_CONFIG_COUNT"] = str(count)
    return env


def git(root, *args):
    env = dict(os.environ)
    task_roots = [env.get(CONTROL_ENV), env.get(WORK_ENV)]
    if all(task_roots) and Path(root).resolve() in [Path(p).resolve() for p in task_roots]:
        env = git_environment(env, task_roots)
    proc = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=300, env=env, **quota.NO_WINDOW)
    if proc.returncode:
        raise WorktreeError(f"git {' '.join(args[:2])}: {(proc.stderr or proc.stdout).strip()[:500]}")
    return proc.stdout if "-z" in args else proc.stdout.strip()


def scope_patterns(values):
    if not values:
        raise WorktreeError("分派必須用 --allow <相對路徑或 glob> 明列允許修改範圍（可重複）。")
    result = []
    for value in values:
        value = value.replace("\\", "/")
        if not value or value.startswith(("/", ":")) or ":" in value or ".." in value.split("/"):
            raise WorktreeError(f"無效的允許範圍：{value}")
        result.append(value.rstrip("/"))
    return result


def save(meta):
    path = Path(meta["manifest"])
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def prepare(root, dispatch_id, allowed, thread):
    root = root.resolve()
    allowed = scope_patterns(allowed)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", dispatch_id):
        raise WorktreeError("無效的 dispatch ID")
    if Path(git(root, "rev-parse", "--show-toplevel")).resolve() != root:
        raise WorktreeError("分派工作區必須是 Git repository 根目錄。")
    try:
        source = git(root, "symbolic-ref", "--quiet", "--short", "HEAD")
    except WorktreeError as exc:
        raise WorktreeError("detached HEAD：無法分派。") from exc
    if not source:
        raise WorktreeError("detached HEAD：無法分派。")
    base = git(root, "rev-parse", "HEAD")
    branch = f"agora/{dispatch_id}"
    target = root / ".agora" / "worktrees" / dispatch_id
    control = thread / "dispatches" / dispatch_id
    refs = git(root, "for-each-ref", "--format=%(refname)", f"refs/heads/{branch}").splitlines()
    if f"refs/heads/{branch}" in refs or target.exists() or target.is_symlink() or control.exists():
        raise WorktreeError(f"分派資源已存在，不覆蓋：{branch} / {target} / {control}")
    dirty = bool(git(root, "status", "--porcelain"))
    meta = dict(dispatch_id=dispatch_id, source_branch=source, base_sha=base, branch=branch,
                worktree=str(target), control_root=str(root), thread=str(thread.resolve()),
                allowed_paths=allowed, source_dirty=dirty, manifest=str(control / "manifest.json"),
                commit_message=str(control / "commit-message.md"), check_result=None,
                commit_sha=None, commit_status="pending", environment_status="pending")
    control.mkdir(parents=True, exist_ok=False)
    save(meta)
    try:
        git(root, "worktree", "add", "-b", branch, str(target), base)
    except BaseException as exc:
        meta.update(commit_status="setup_failed", error=str(exc))
        save(meta)
        raise
    return meta


def load(relay):
    try:
        meta = json.loads(Path(relay["manifest"]).read_text(encoding="utf-8"))
    except (KeyError, OSError, ValueError) as exc:
        raise WorktreeError(f"無法讀取分派 manifest：{exc}") from exc
    for key in ("dispatch_id", "branch", "base_sha", "worktree", "control_root", "manifest", "commit_message", "allowed_paths"):
        if meta.get(key) != relay.get(key):
            raise WorktreeError(f"分派 metadata 不符：{key}")
    return meta


def validate(meta):
    root = Path(meta["control_root"]).resolve()
    target = Path(meta["worktree"]).resolve()
    if target != root / ".agora" / "worktrees" / meta["dispatch_id"]:
        raise WorktreeError("worktree 位置不符")
    if meta["branch"] != f"agora/{meta['dispatch_id']}":
        raise WorktreeError("dispatch 與 branch 不符")
    if Path(git(target, "rev-parse", "--show-toplevel")).resolve() != target:
        raise WorktreeError("Git worktree 根目錄不符")
    if git(target, "symbolic-ref", "--quiet", "--short", "HEAD") != meta["branch"]:
        raise WorktreeError("worktree branch 不符")
    if git(target, "rev-parse", "--git-common-dir") != git(root, "rev-parse", "--git-common-dir"):
        # --git-common-dir may be relative in the main checkout.
        def common(p):
            return (p / git(p, "rev-parse", "--git-common-dir")).resolve()
        if common(target) != common(root):
            raise WorktreeError("worktree repository 不符")
    expected = meta.get("commit_sha") or meta["base_sha"]
    if git(target, "rev-parse", "HEAD") != expected:
        raise WorktreeError("HEAD 已改變，拒絕提交未核對的歷史")
    return target


def python_path(target):
    return target / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def environment(meta):
    target = Path(meta["worktree"])
    env = dict(os.environ)
    for key in ("PYTHONPATH", "PYTHONHOME", "AGORA_JOB"):
        env.pop(key, None)
    env.update(VIRTUAL_ENV=str(target / ".venv"), PYTHONNOUSERSITE="1",
               PATH=str(python_path(target).parent) + os.pathsep + env.get("PATH", ""),
               AGORA_WORKSPACE=str(target), **{CONTROL_ENV: meta["control_root"], WORK_ENV: str(target),
                                              DISPATCH_ENV: meta["dispatch_id"], MANIFEST_ENV: meta["manifest"]})
    return git_environment(env, (meta["control_root"], target))


def setup_environment(meta):
    target = validate(meta)
    if (target / ".venv").exists():
        raise WorktreeError(".venv 已存在，拒絕覆用未驗證的環境")
    try:
        venv.EnvBuilder(with_pip=True).create(target / ".venv")
        commands = []
        for name in ("requirements.txt", "requirements-dev.txt"):
            if (target / name).is_file():
                commands.append([str(python_path(target)), "-m", "pip", "install", "-r", name])
        if any((target / name).is_file() for name in ("pyproject.toml", "setup.py", "setup.cfg")):
            commands.append([str(python_path(target)), "-m", "pip", "install", "-e", "."])
        for command in commands:
            proc = subprocess.run(command, cwd=target, env=environment(meta), capture_output=True,
                                  text=True, encoding="utf-8", errors="replace", timeout=600, **quota.NO_WINDOW)
            if proc.returncode:
                raise WorktreeError(f"依賴安裝失敗：{(proc.stdout + proc.stderr)[-2000:]}")
        verify_environment(meta)
        meta["environment_status"] = "ready"
        save(meta)
    except BaseException as exc:
        meta.update(environment_status="failed", error=str(exc))
        save(meta)
        raise


def verify_environment(meta):
    target = validate(meta)
    python = python_path(target)
    if not python.is_file():
        raise WorktreeError("缺少分派自己的 .venv，不能降級使用主環境")
    packages = sorted({p.parts[1] if p.parts[0] == "src" else p.parts[0]
                       for p in map(Path, git(target, "ls-files", "*/__init__.py").splitlines())
                       if len(p.parts) == (3 if p.parts[0] == "src" else 2)
                       and p.parts[0] not in ("tests", ".venv", "venv")})
    code = '''import importlib.util, importlib.metadata, json, pathlib, sys
from urllib.parse import unquote, urlparse
root = pathlib.Path(sys.argv[1]).resolve()
assert pathlib.Path(sys.prefix).resolve() == root / '.venv', 'wrong venv'
for dist in importlib.metadata.distributions():
    direct = json.loads(dist.read_text('direct_url.json') or '{}')
    if direct.get('dir_info', {}).get('editable'):
        path = unquote(urlparse(direct.get('url', '')).path)
        if len(path) > 2 and path[0] == '/' and path[2] == ':': path = path[1:]
        assert pathlib.Path(path).resolve() == root, 'editable install outside worktree: ' + path
for name in json.loads(sys.argv[2]):
    spec = importlib.util.find_spec(name)
    assert spec is not None, 'missing module: ' + name
    paths = [spec.origin] if spec.origin else list(spec.submodule_search_locations or [])
    assert paths, 'unresolved module: ' + name
    for path in paths:
        parents = pathlib.Path(path).resolve().parents
        assert root in parents and root / '.venv' not in parents, 'module outside worktree source: ' + str(path)
print('worktree imports verified')
'''
    proc = subprocess.run([str(python), "-c", code, str(target), json.dumps(packages)], cwd=target,
                          env=environment(meta), capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=60, **quota.NO_WINDOW)
    if proc.returncode:
        raise WorktreeError(f"載入來源檢查失敗：{proc.stdout}{proc.stderr}")


def rebase_command(spec, meta):
    """Map frozen controller commands into the task's environment and paths."""
    def map_path(text):
        sources = sorted({meta["control_root"], Path(meta["control_root"]).as_posix()}, key=len, reverse=True)
        return re.sub("|".join(re.escape(s) for s in sources), lambda _: meta["worktree"], text)

    if isinstance(spec, dict):
        return {k: rebase_command(v, meta) for k, v in spec.items()}
    if isinstance(spec, list):
        result = [map_path(x) if isinstance(x, str) else rebase_command(x, meta) for x in spec]
        if result and isinstance(result[0], str):
            first = Path(result[0]).name.lower()
            if re.fullmatch(r"python[\d.]*(\.exe)?|py(\.exe)?", first):
                result[0] = str(python_path(Path(meta["worktree"])))
        return result
    if isinstance(spec, str):
        result = map_path(spec)
        first = re.match(r'''\s*(?:"([^"]+)"|'([^']+)'|(\S+))''', result)
        if first:
            program = next(x for x in first.groups() if x is not None)
            if re.fullmatch(r"python[\d.]*(\.exe)?|py(\.exe)?", Path(program).name.lower()):
                result = '"' + str(python_path(Path(meta["worktree"]))) + '"' + result[first.end():]
        return result
    return spec


def checked_paths(meta):
    target = validate(meta)
    paths = set(filter(None, git(target, "diff", "--name-only", "--no-renames", "-z", meta["base_sha"]).split("\0")))
    paths.update(filter(None, git(target, "ls-files", "--others", "--exclude-standard", "--exclude=.venv/", "-z").split("\0")))
    for name in paths:
        parts = name.split("/")
        forbidden = any(p in (".git", ".agora", ".venv", "venv") for p in parts)
        forbidden |= any(fnmatch.fnmatchcase(p, pattern) for p in parts for pattern in snapshot.SECRET_PATTERNS)
        if forbidden or not any(fnmatch.fnmatchcase(name, pat) for pat in meta["allowed_paths"]):
            raise WorktreeError(f"超出允許修改範圍：{name}")
        resolved = (target / name).resolve()
        if target not in resolved.parents:
            raise WorktreeError(f"變更路徑指向 worktree 外：{name}")
    return sorted(paths)


def task_tree(meta):
    target = validate(meta)
    paths = checked_paths(meta)
    with tempfile.TemporaryDirectory(prefix="agora-task-index-") as tmp:
        env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
        snapshot._git(target, "read-tree", meta["base_sha"], env=env)
        if paths:
            snapshot._git(target, "add", "-A", "--", *[f":(literal){p}" for p in paths], env=env)
        return snapshot._git(target, "write-tree", env=env)


def recover_commit(meta):
    """Recover the small interval between updating a ref and saving its SHA."""
    target = Path(meta["worktree"])
    head = git(target, "rev-parse", "HEAD")
    if head != meta["base_sha"] and not meta.get("commit_sha"):
        body = git(target, "show", "-s", "--format=%B", "HEAD")
        parent = git(target, "rev-parse", "HEAD^")
        if (f"Agora-Dispatch: {meta['dispatch_id']}" in body and parent == meta["base_sha"]
                and (meta.get("check_result") or {}).get("exit_code") == 0
                and meta.get("checked_tree") == git(target, "rev-parse", "HEAD^{tree}")):
            meta.update(commit_sha=head, commit_status="committed")
            save(meta)


def finalize(meta, run_check, can_commit=lambda: True):
    """Called only after the CLI process has exited; commit the exact checked tree.

    A failing/not-run check never commits. A private index and commit-tree bind the
    check to immutable content. A crash after update-ref is recovered by its trailer.
    """
    recover_commit(meta)
    target = validate(meta)
    if meta.get("commit_sha"):
        git(target, "read-tree", meta["commit_sha"])
        return meta
    checked_paths(meta)
    message = Path(meta["commit_message"]).read_text(encoding="utf-8").strip()
    if not message:
        raise WorktreeError("缺少提交說明：請交代任務目的與測試範圍")
    tree = task_tree(meta)
    meta.update(commit_status="checking", checked_tree=tree)
    save(meta)
    result = run_check(meta)
    meta["check_result"] = result
    meta["commit_status"] = "check_failed" if result.get("exit_code") != 0 else "checked"
    save(meta)
    if result.get("exit_code") != 0:
        raise WorktreeError("agora check 失敗或未執行，成果保留但不提交")
    if not can_commit():
        raise WorktreeError("已要求停止，成果保留但不提交")
    checked_paths(meta)
    if task_tree(meta) != tree:
        raise WorktreeError("check 期間程式碼改變，拒絕提交與驗證不符的內容")
    if tree == git(target, "rev-parse", "HEAD^{tree}"):
        meta["commit_status"] = "no_changes"
        save(meta)
        return meta
    check_hash = hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()
    body = (message + f"\n\nAgora-Dispatch: {meta['dispatch_id']}\nAgora-Base: {meta['base_sha']}"
            + f"\nAgora-Check: exit={result['exit_code']}; record-sha256={check_hash}\n"
            + "\n" + result.get("output_tail", ""))
    with tempfile.TemporaryDirectory(prefix="agora-commit-") as tmp:
        msg = Path(tmp) / "message.txt"
        msg.write_text(body, encoding="utf-8")
        commit = git(target, "commit-tree", tree, "-p", meta["base_sha"], "-F", str(msg))
    # Compare-and-swap: never overwrite a branch moved by somebody else.
    if not can_commit():
        raise WorktreeError("已要求停止，成果保留但不提交")
    git(target, "update-ref", f"refs/heads/{meta['branch']}", commit, meta["base_sha"])
    meta.update(commit_sha=commit, commit_status="committed")
    save(meta)
    git(target, "read-tree", commit)
    return meta
