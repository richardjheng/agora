"""Fail-closed, on-demand comparison of individual test failures with dispatch base.

Only ordinary unittest/pytest commands are supported. Shell pipelines, custom
runners, collection failures and incomplete reports are deliberately unapprovable.
The initial check is immutable evidence; a diagnostic retry can only make a
failure less eligible for approval.
"""

import fnmatch
import hashlib
import json
import re
import shlex
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

from agoralib import checkqueue, completions, config, quota, worktree
from agoralib.worktree import RECORD_ENV

LABELS = {"new": "新失敗", "preexisting": "既有失敗", "flaky": "不穩定", "unknown": "無法比對"}


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def valid_test_id(test_id, framework):
    """Enforce strict ID whitelist before parsing/rerunning individual test IDs."""
    if not isinstance(test_id, str) or not test_id or test_id.startswith("-") or any(c.isspace() for c in test_id):
        return False
    if framework == "unittest":
        parts = test_id.split(".")
        return len(parts) >= 3 and all(p.isidentifier() for p in parts)
    if framework == "pytest":
        if "::" not in test_id or test_id.startswith(("/", "\\", "-")):
            return False
        path_part = test_id.split("::", 1)[0]
        return ":" not in path_part and ".." not in re.split(r"[/\\]", path_part)
    return False


def parse(output, framework, exit_code):
    """Require a complete, single report with consistent failure counts."""
    text = re.sub(r"\x1b\[[0-9;]*m", "", output)
    result = {"complete": False, "ids": [], "count": 0}
    if framework == "unittest":
        rows = re.findall(r"^(FAIL|ERROR): (\w+) \(([^\r\n]+)\)\s*$", text, re.M)
        result["ids"] = sorted(set(test for _, _, test in rows))
        runs = re.findall(r"^Ran (\d+) tests? in .+$", text, re.M)
        ends = re.findall(r"^(OK(?: \([^\n]*\))?|FAILED \([^\n]+\))\s*$", text, re.M)
        if (len(runs) != 1 or len(ends) != 1 or "_FailedTest" in text
                or "unexpected successes" in text
                or any(name != test.rsplit(".", 1)[-1] for _, name, test in rows)):
            return result
        result["count"] = int(runs[0])
        if any(not valid_test_id(test, "unittest") for test in result["ids"]):
            return result  # fixtures, subtests, custom descriptions
        counts = dict((k, int(v)) for k, v in re.findall(r"[,(]\s*(failures|errors)=(\d+)", ends[0]))
        expected = sum(counts.values())
        result["complete"] = (len(rows) == len(result["ids"]) == expected <= result["count"]
                              and ((exit_code == 0 and ends[0].startswith("OK"))
                                   or (exit_code == 1 and expected > 0)))
    elif framework == "pytest":
        rows = re.findall(r"^(FAILED|ERROR) (\S+)(?: - .*|\s*)$", text, re.M)
        result["ids"] = sorted(set(test for _, test in rows))
        summaries = re.findall(r"^=*[ \t]*((?:\d+ \w+(?:, )?)+) in [\d.]+s(?: \([^\n]*\))?[ \t]*=*[ \t]*$", text, re.M)
        if len(summaries) != 1 or re.search(r"ERROR collecting|Interrupted:|INTERNALERROR|no tests ran", text):
            return result
        counts = dict((k, int(v)) for v, k in re.findall(r"(\d+) (\w+)", summaries[0]))
        result["count"] = sum(counts.get(k, 0) for k in ("passed", "failed", "error", "errors", "skipped", "xfailed", "xpassed"))
        expected = counts.get("failed", 0) + counts.get("error", 0) + counts.get("errors", 0)
        safe_ids = all(valid_test_id(test, "pytest") for test in result["ids"])
        result["complete"] = (safe_ids and len(rows) == len(result["ids"]) == expected
                              and ((exit_code == 0 and counts.get("passed", 0) > 0 and not expected)
                                   or (exit_code == 1 and expected > 0)))
    return result


def plan(spec):
    """Translate a simple frozen command; never execute arbitrary shell fragments."""
    if isinstance(spec, str):
        if any(c in spec for c in "&|;<>`$\n\r"):
            raise ValueError("複合 shell 指令無法比對")
        spec = [s.strip('\"\'') for s in shlex.split(spec, posix=False)]
    if not isinstance(spec, list) or not spec or not all(isinstance(s, str) for s in spec):
        raise ValueError("未知或複合驗證指令")
    program = re.split(r"[/\\]", spec[0])[-1].lower()
    if len(spec) >= 3 and re.fullmatch(r"python[\d.]*(?:\.exe)?|py(?:\.exe)?", program) and spec[1:2] == ["-m"]:
        framework, args = spec[2], spec[3:]
    elif program in ("pytest", "pytest.exe"):
        if spec[0] != program:
            raise ValueError("pytest 執行檔須由分派環境解析，外部路徑無法比對")
        framework, args = "pytest", spec[1:]
    else:
        raise ValueError("未知測試框架")
    if framework not in ("unittest", "pytest"):
        raise ValueError("未知測試框架")
    start, top, flags = ".", ".", []
    i = 0
    while i < len(args):
        arg = args[i]
        if framework == "unittest" and arg in ("-s", "--start-directory", "-t", "--top-level-directory", "-p", "--pattern"):
            i += 1
            if i >= len(args):
                raise ValueError("不完整 unittest 指令")
            if arg in ("-s", "--start-directory"):
                start = args[i]
            elif arg in ("-t", "--top-level-directory"):
                top = args[i]
        elif arg in ("-q", "-v", "--quiet", "--verbose"):
            pass
        elif framework == "unittest" and arg in ("-b", "--buffer"):
            flags.append(arg)
        elif framework == "pytest" and arg in ("--disable-warnings", "--strict-markers", "--strict-config"):
            flags.append(arg)
        elif framework == "pytest" and re.fullmatch(r"--tb=(auto|long|short|line|native|no)", arg):
            flags.append(arg)
        elif arg.startswith("-"):
            raise ValueError(f"無法安全重跑的測試選項：{arg}")
        # Positional discovery paths/selectors are replaced with the failed IDs.
        i += 1
    for value in (start, top):
        if Path(value).is_absolute() or ".." in Path(value).parts:
            raise ValueError("測試探索路徑必須在工作區內")
    return {"framework": framework, "start": start, "top": top, "flags": flags}


def plan_custom(spec, root=None):
    """Validate and translate a declared check_framework into a rerun plan."""
    validated = config.validate_check_framework(spec, root=root)
    return {
        "framework": validated["output"],
        "custom": True,
        "script": validated["script"],
        "rerun": validated["rerun"],
        "ids_index": validated["ids_index"],
        "env": validated["env"],
        "target_format": validated["target_format"],
        "start": ".",
        "top": ".",
        "flags": [],
    }


def dependency_fingerprint(target):
    names = worktree.git(target, "ls-files", "-z").split("\0")
    names += worktree.git(target, "ls-files", "--others", "--exclude-standard", "--exclude=.venv/", "-z").split("\0")
    patterns = ("requirements*.txt", "pyproject.toml", "setup.py", "setup.cfg", "*.lock", "*-lock.json", "Pipfile")
    result = {}
    for name in sorted(set(names)):
        if not name or not any(fnmatch.fnmatchcase(Path(name).name, p) for p in patterns):
            continue
        path = target / name
        if path.is_symlink() or target.resolve() not in path.resolve().parents:
            raise ValueError("依賴檔指向工作區外")
        result[name] = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
    return digest(result)


def environment_fingerprint(target):
    """Also invalidate cached evidence when installed dependency versions change."""
    env = target / ".venv"
    cfg = (env / "pyvenv.cfg").read_text(encoding="utf-8")
    result = {"python": [line for line in cfg.splitlines() if line.startswith(("home =", "version =", "include-system-site-packages ="))]}
    sites = [env / "Lib" / "site-packages", *env.glob("lib/python*/site-packages")]
    for site in sites:
        for path in sorted(site.glob("*.dist-info/METADATA")):
            result[path.parent.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return digest(result)


def cached_results_valid(cached, key, ids):
    results = cached.get("base", {})
    if cached.get("key") != key or set(results) != set(ids):
        return False
    for item in results.values():
        if not isinstance(item, dict) or item.get("state") not in ("passed", "failed", "unknown"):
            return False
        try:
            if hashlib.sha256(Path(item["log"]).read_bytes()).hexdigest() != item.get("log_sha256"):
                return False
        except (OSError, KeyError):
            return False
    return True


# An editable install in the dispatch venv must never silently import dispatch
# sources while testing the base. Root/src and discovery paths take precedence;
# any remaining imports from the other worktree invalidate the diagnostic.
RUNNER = '''import pathlib, runpy, sys
root, task, start, top, framework = sys.argv[1:6]
args = sys.argv[6:]
root, task = pathlib.Path(root).resolve(), pathlib.Path(task).resolve()
sys.path[:] = [p for p in sys.path if not (p and task in pathlib.Path(p).resolve().parents and task / '.venv' not in pathlib.Path(p).resolve().parents)]
sys.path[:0] = [str(root / top), str(root / start), str(root / 'src'), str(root)]
sys.argv = [framework] + args
code = 0
try:
    runpy.run_module(framework, run_name='__main__', alter_sys=True)
except SystemExit as exc:
    code = exc.code or 0
if root != task:
    for mod in list(sys.modules.values()):
        origin = getattr(mod, '__file__', None)
        if origin:
            parents = pathlib.Path(origin).resolve().parents
            if task in parents and task / '.venv' not in parents:
                print('AGORA: imported dispatch source during baseline', file=sys.stderr)
                code = 3
sys.exit(code)
'''


CUSTOM_RUNNER = '''import pathlib, runpy, sys
root, task, script = sys.argv[1:4]
args = sys.argv[4:]
root, task = pathlib.Path(root).resolve(), pathlib.Path(task).resolve()
script_path = (root / script).resolve()
if root not in script_path.parents or not script_path.is_file():
    print('AGORA: custom test script outside workspace or missing', file=sys.stderr)
    sys.exit(2)
def _in_task_source(p):
    try:
        rp = pathlib.Path(p).resolve()
    except OSError:
        return False
    return (rp == task or task in rp.parents) and rp != task / '.venv' and task / '.venv' not in rp.parents
sys.path[:] = [p for p in sys.path if not (p and _in_task_source(p))]
sys.path[:0] = [str(root / 'src'), str(root)]
sys.argv = [script] + args
code = 0
try:
    runpy.run_path(str(script_path), run_name='__main__')
except SystemExit as exc:
    if exc.code is None:
        code = 0
    elif isinstance(exc.code, int):
        code = exc.code
    else:
        print(exc.code, file=sys.stderr)
        code = 1
if root != task:
    for mod in list(sys.modules.values()):
        origin = getattr(mod, '__file__', None)
        if isinstance(origin, str) and origin and _in_task_source(origin):
            print('AGORA: imported dispatch source during baseline', file=sys.stderr)
            code = 3
sys.exit(code)
'''


def rerun(meta, target, selected, ids, directory, label, can_continue=lambda: True):
    env = worktree.environment(meta)
    env.pop(RECORD_ENV, None)
    if selected.get("env"):
        env.update(selected["env"])
    env.update(PYTHONDONTWRITEBYTECODE="1", AGORA_WORKSPACE=str(target))
    env = worktree.git_environment(env, (target,))
    target_resolved = Path(target).resolve()
    if selected.get("custom"):
        script_path = (target_resolved / selected["script"]).resolve()
        if target_resolved not in script_path.parents or not script_path.is_file():
            raise ValueError(f"工作區缺少自訂測試腳本：{selected['script']}")
    results = {}
    for index, test in enumerate(ids):
        if not can_continue():
            raise checkqueue.CheckStopped("已要求停止，不再執行下一個診斷項目")
        path = directory / f"{label}-{index}.log"
        if not valid_test_id(test, selected["framework"]):
            output = f"AGORA: invalid test id rejected: {test}\n"
            path.write_text(output, encoding="utf-8")
            results[test] = {
                "state": "unknown", "exit_code": None, "log": str(path), "time": now(),
                "log_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "reason": f"不合格的測試 ID：{test}",
            }
            continue
        if selected.get("custom"):
            template_args = [test if item == "{ids}" else item for item in selected["rerun"][1:]]
            command = [str(worktree.python_path(Path(meta["worktree"]))), "-c", CUSTOM_RUNNER,
                       str(target), meta["worktree"], selected["script"], *template_args]
        else:
            args = (["-v"] if selected["framework"] == "unittest"
                    else ["-p", "no:cacheprovider", "-q", "-rA", "--color=no"])
            command = [str(worktree.python_path(Path(meta["worktree"]))), "-c", RUNNER,
                       str(target), meta["worktree"], selected["start"], selected["top"], selected["framework"],
                       *selected["flags"], *args, test]
        proc = subprocess.run(command, cwd=target, env=env, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", **quota.NO_WINDOW)
        output = proc.stdout + proc.stderr
        path.write_text(output, encoding="utf-8")
        parsed = parse(output, selected["framework"], proc.returncode)
        state = "unknown"
        if parsed["complete"] and parsed["count"] == 1:
            if proc.returncode == 0 and not parsed["ids"]:
                # A skipped unittest is OK but is not evidence of a pass.
                if not re.search(r"skipped|xfailed|xpassed|deselected|expected failures", output):
                    state = "passed"
            elif parsed["ids"] == [test]:
                state = "failed"
        entry = {"state": state, "exit_code": proc.returncode, "log": str(path), "time": now(),
                 "log_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        if state == "unknown":
            if "AGORA: imported dispatch source during baseline" in output:
                entry["reason"] = "基準重跑誤載入分派原始碼"
            elif not parsed["complete"]:
                entry["reason"] = "重跑輸出不完整或異常退出"
            elif parsed["count"] != 1:
                entry["reason"] = f"重跑執行測試數為 {parsed['count']}（預期 1）"
            else:
                entry["reason"] = "重跑結果無法判定"
        results[test] = entry
    return results


def compare(meta, result, can_continue=lambda: True):
    ids = []
    comparison = {"time": now(), "checked_tree": meta.get("checked_tree"),
                  "commands": json.loads(json.dumps(meta.get("commands"))), "initial": dict(result), "classifications": {},
                  "base": {}, "dispatch": {}, "cache_hit": False}
    try:
        commands = meta.get("commands") or {}
        target = Path(meta["worktree"])
        if commands.get("check_framework") is not None:
            selected = plan_custom(commands.get("check_framework"), root=target)
        else:
            selected = plan(commands.get("check"))
        if (not result.get("complete") or result.get("syntax_errors") != 0
                or result.get("exit_code") != 1 or result.get("test_exit_code") != 1):
            raise ValueError("首次驗證未完整執行或有語法錯誤")
        output = Path(result.get("test_log") or result["log"]).read_text(encoding="utf-8")
        parsed = parse(output, selected["framework"], result["test_exit_code"])
        ids = parsed["ids"]
        if not parsed["complete"] or not ids:
            raise ValueError("收集／匯入失敗、輸出不完整或無法解析失敗項目")
        fingerprint = dependency_fingerprint(target)
        key = digest({"version": 1, "base_sha": meta["base_sha"], "commands": meta["commands"],
                      "ids": ids, "dependencies": fingerprint, "environment": environment_fingerprint(target)})
        comparison.update(ids=ids, dependency_fingerprint=fingerprint, cache_key=key)
        directory = Path(meta["manifest"]).parent / ("baseline-" + uuid.uuid4().hex)
        directory.mkdir()
        root = Path(meta["control_root"])
        cache_home = root / ".agora" / "baselines" / key
        # The original check has exited and released its slot. No nested slot.
        with checkqueue.acquire_slot(root, label=meta["dispatch_id"] + " baseline",
                                     stop_check=lambda: not can_continue()):
            with completions.report_lock(cache_home, wait=300):
                cache_path = cache_home / "result.json"
                cached = completions.read_object(cache_path)
                if cached_results_valid(cached, key, ids):
                    comparison.update(base=cached["base"], cache_hit=True)
                else:
                    base = root / ".agora" / "worktrees" / ("base-" + meta["base_sha"][:12] + "-" + uuid.uuid4().hex[:12])
                    worktree.git(root, "worktree", "add", "--detach", str(base), meta["base_sha"])
                    comparison["base_worktree"] = str(base)
                    comparison["base"] = rerun(meta, base, selected, ids, directory, "base", can_continue)
                    # Tests that rewrite tracked base sources cannot establish a baseline.
                    if worktree.git(base, "diff", "HEAD"):
                        raise ValueError("基準重跑改動了起點來源")
                    if all(item["state"] in ("passed", "failed") for item in comparison["base"].values()):
                        completions.atomic_write(cache_path, {"key": key, "base": comparison["base"]})
            if not can_continue():
                raise ValueError("已要求停止，未執行診斷重跑")
            comparison["dispatch"] = rerun(meta, target, selected, ids, directory, "dispatch", can_continue)
        for test in ids:
            base_state = comparison["base"][test]["state"]
            task_state = comparison["dispatch"][test]["state"]
            comparison["classifications"][test] = (
                "flaky" if task_state == "passed" else
                "unknown" if task_state != "failed" or base_state not in ("passed", "failed") else
                "new" if base_state == "passed" else "preexisting")
    except (OSError, ValueError, KeyError, TypeError, worktree.WorktreeError, checkqueue.CheckStopped, subprocess.SubprocessError) as exc:
        comparison["error"] = str(exc)
        comparison["classifications"] = {test: "unknown" for test in ids or ["<unparsed>"]}
    return comparison


def eligible(comparison):
    values = (comparison or {}).get("classifications", {})
    return bool(values) and set(values.values()) == {"preexisting"} and not comparison.get("error")


def _unknown_reason(comparison, unknown_ids):
    if comparison.get("error"):
        return str(comparison["error"])
    reasons = []
    for bucket in ("base", "dispatch"):
        records = comparison.get(bucket) or {}
        for test_id in unknown_ids:
            item = records.get(test_id)
            if isinstance(item, dict) and item.get("reason") and item["reason"] not in reasons:
                reasons.append(item["reason"])
    return "；".join(reasons) if reasons else "部分測試重跑結果無法判定"


def describe(record):
    comparison = record.get("baseline_comparison")
    if not comparison:
        return ""
    classifications = comparison.get("classifications") or {}
    unknown_ids = [k for k, v in classifications.items() if v == "unknown"]
    has_unknown = bool(comparison.get("error")) or bool(unknown_ids)
    if has_unknown:
        groups = []
        for kind, label in LABELS.items():
            items = [k for k, v in classifications.items() if v == kind]
            if items:
                groups.append(f"{label}：" + ", ".join(items))
        reason = _unknown_reason(comparison, unknown_ids)
        groups.append(f"無法比對失敗項目（不代表沒有新失敗）：{reason}")
        if "未知測試框架" in reason or "未知或複合驗證指令" in reason:
            groups.append("自訂驗證指令請在 .agora/config.json 宣告 check_framework")
        return "\n".join(groups)
    groups = [f"{label}：" + (", ".join(k for k, v in classifications.items() if v == kind) or "無")
              for kind, label in LABELS.items()]
    if eligible(comparison) and not record.get("commit_sha"):
        thread = Path(record.get("thread") or "<thread>").name
        groups.append(f'主持方可核准（不自動核准）：agora finalize "{thread}" --dispatch-id {record["dispatch_id"]} --accept-preexisting --reason "<理由>"')
    return "\n".join(groups)


def approval_valid(meta, tree):
    approval = meta.get("preexisting_approval") or {}
    comparison = meta.get("baseline_comparison") or {}
    return (eligible(comparison) and bool(approval.get("reason"))
            and approval.get("checked_tree") == tree == comparison.get("checked_tree")
            and approval.get("commands") == meta.get("commands") == comparison.get("commands")
            and approval.get("ids") == sorted(comparison["classifications"])
            and approval.get("comparison_hash") == digest(comparison))
