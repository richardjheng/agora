"""隔離分派局部測試（`agora test` / `agoralib.targeted`）測試。"""

import argparse
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agoralib import checkqueue, cli, config, jobs, parties, targeted, worktree  # noqa: E402


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    ).stdout.strip()


def _make_repo(tmp: str) -> Path:
    root = Path(tmp).resolve()
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "t")
    _git(root, "config", "user.email", "t@t")
    (root / "pkg").mkdir()
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pkg" / "alpha.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "pkg" / "lonely.py").write_text("X = 1\n", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_alpha.py").write_text(
        "import unittest\n"
        "from pkg.alpha import VALUE\n\n"
        "class TestAlpha(unittest.TestCase):\n"
        "    def test_value(self):\n"
        "        self.assertEqual(VALUE, 1)\n",
        encoding="utf-8",
    )
    (root / "tests" / "test_beta.py").write_text(
        "import unittest\n\n"
        "class TestBeta(unittest.TestCase):\n"
        "    def test_ok(self):\n"
        "        self.assertTrue(True)\n",
        encoding="utf-8",
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    return root


def _make_isolated_dispatch(root: Path, dispatch_id: str = "disp-test-1"):
    thread = root / ".agora" / "threads" / "t1"
    thread.mkdir(parents=True, exist_ok=True)
    meta = worktree.prepare(root, dispatch_id, ["**"], thread, worker="antigravity", title="targeted")
    meta["commands"] = {"build": None, "check": [sys.executable, "-m", "unittest", "discover", "-s", "tests"]}
    worktree.save(meta)
    return thread, meta


class TestParseTargetsFile(unittest.TestCase):
    def test_ignores_comments_and_blank_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test-targets.txt"
            self.assertEqual(targeted.parse_targets_file(path), [])
            path.write_text(
                "\n"
                "# 這是註解\n"
                "   # 縮排註解\n"
                "  tests/test_alpha.py  \n"
                "\n"
                "tests/test_beta.py::TestBeta::test_ok\n"
                "   \n",
                encoding="utf-8",
            )
            self.assertEqual(
                targeted.parse_targets_file(path),
                ["tests/test_alpha.py", "tests/test_beta.py::TestBeta::test_ok"],
            )


class TestValidateTargets(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        (self.root / "tests").mkdir()
        (self.root / "tests" / "test_ok.py").write_text("x = 1\n", encoding="utf-8")
        (self.root / "tests" / "note.txt").write_text("hi\n", encoding="utf-8")
        (self.root / "outside.py").write_text("x = 1\n", encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def test_accepts_valid_paths_and_pytest_node_ids(self):
        self.assertEqual(
            targeted.validate_targets(["tests/test_ok.py"], self.root, "unittest"),
            ["tests/test_ok.py"],
        )
        self.assertEqual(
            targeted.validate_targets(
                ["tests/test_ok.py", "tests/test_ok.py::TestA::test_case[1]"],
                self.root,
                "pytest",
            ),
            ["tests/test_ok.py", "tests/test_ok.py::TestA::test_case[1]"],
        )

    def test_rejects_invalid_targets_as_batch_with_reasons(self):
        bad_cases = [
            "-k",
            "--maxfail=1",
            "tests/test_ok.py -v",
            "tests/../outside.py",
            "../outside.py",
            "/etc/passwd.py",
            "C:/tests/test_ok.py",
            "tests/test_*.py",
            "tests/test_?.py",
            "tests/test_[ab].py",
            "outside.py",
            "tests/note.txt",
            "tests/test_missing.py",
        ]
        # 即使混有合法目標，整批都必須拒絕並逐項列出原因
        with self.assertRaises(targeted.TargetValidationError) as ctx:
            targeted.validate_targets(["tests/test_ok.py", *bad_cases], self.root, "pytest")
        msg = str(ctx.exception)
        self.assertEqual(len(ctx.exception.errors), len(bad_cases))
        for item in bad_cases:
            self.assertIn(item, msg)

    def test_rejects_pytest_node_id_with_bad_chars_or_under_unittest(self):
        with self.assertRaises(targeted.TargetValidationError):
            targeted.validate_targets(["tests/test_ok.py::test_a"], self.root, "unittest")
        for bad in (
            "tests/test_ok.py::",
            "tests/test_ok.py::test_a;rm",
            "tests/test_ok.py::test_*",
            "tests/test_ok.py::test_a[x-y]",
            "tests/test_ok.py::TestA::::test_b",
        ):
            with self.assertRaises(targeted.TargetValidationError, msg=bad):
                targeted.validate_targets([bad], self.root, "pytest")

    def test_rejects_symlink_escaping_test_root(self):
        outside = self.root / "outside.py"
        link = self.root / "tests" / "test_escape.py"
        created_real_symlink = False
        try:
            os.symlink(outside, link)
            created_real_symlink = True
        except OSError:
            # Windows 未開啟開發人員模式時無法建立實體 symlink，改以 resolve mock 驗證
            link.write_text("x = 1\n", encoding="utf-8")

        if created_real_symlink:
            with self.assertRaises(targeted.TargetValidationError) as ctx:
                targeted.validate_targets(["tests/test_escape.py"], self.root, "unittest")
            self.assertIn("超出測試根目錄", str(ctx.exception))
        else:
            real_resolve = Path.resolve

            def fake_resolve(path_self, *args, **kwargs):
                if path_self.name == "test_escape.py":
                    return outside
                return real_resolve(path_self, *args, **kwargs)

            with patch.object(Path, "resolve", fake_resolve):
                with self.assertRaises(targeted.TargetValidationError) as ctx:
                    targeted.validate_targets(["tests/test_escape.py"], self.root, "unittest")
                self.assertIn("超出測試根目錄", str(ctx.exception))

    def test_rejects_over_max_targets(self):
        targets = []
        for i in range(targeted.MAX_TARGETS + 1):
            p = self.root / "tests" / f"test_n{i}.py"
            p.write_text("x = 1\n", encoding="utf-8")
            targets.append(f"tests/test_n{i}.py")
        with self.assertRaises(targeted.TargetValidationError) as ctx:
            targeted.validate_targets(targets, self.root, "unittest")
        self.assertIn("超過上限", str(ctx.exception))


class TestAutoPickTargets(unittest.TestCase):
    def test_picks_changed_test_and_mapped_source_test_and_untracked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_repo(tmp)
            base_sha = _git(root, "rev-parse", "HEAD")
            # 1. 改動 pkg/alpha.py → 對應到 tests/test_alpha.py
            (root / "pkg" / "alpha.py").write_text("VALUE = 2\n", encoding="utf-8")
            self.assertEqual(targeted.auto_pick_targets(root, base_sha), ["tests/test_alpha.py"])
            # 2. 改動測試檔本身 + 新增未追蹤測試檔
            (root / "tests" / "test_beta.py").write_text("# changed\n", encoding="utf-8")
            (root / "tests" / "test_gamma.py").write_text("# new\n", encoding="utf-8")
            self.assertEqual(
                targeted.auto_pick_targets(root, base_sha),
                ["tests/test_alpha.py", "tests/test_beta.py", "tests/test_gamma.py"],
            )

    def test_returns_empty_when_no_matching_test_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_repo(tmp)
            base_sha = _git(root, "rev-parse", "HEAD")
            (root / "pkg" / "lonely.py").write_text("X = 2\n", encoding="utf-8")
            self.assertEqual(targeted.auto_pick_targets(root, base_sha), [])


class TestBuildCommandsAndExecution(unittest.TestCase):
    def test_pytest_and_unittest_argv_building(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "tests").mkdir()
            (root / "tests" / "test_a.py").write_text("", encoding="utf-8")
            (root / "tests" / "test_b.py").write_text("", encoding="utf-8")

            # pytest
            pytest_cmds = targeted.build_commands(
                "/py", "pytest", ["tests/test_a.py", "tests/test_b.py::test_x"], root
            )
            self.assertEqual(
                pytest_cmds,
                [["/py", "-m", "pytest", "-p", "no:cacheprovider", "tests/test_a.py", "tests/test_b.py::test_x"]],
            )

            # unittest without tests/__init__.py (must omit -t <root> so discover works)
            unit_cmds_no_init = targeted.build_commands(
                "/py", "unittest", ["tests/test_a.py", "tests/test_b.py"], root
            )
            self.assertEqual(
                unit_cmds_no_init,
                [
                    ["/py", "-m", "unittest", "discover", "-s", "tests", "-p", "test_a.py"],
                    ["/py", "-m", "unittest", "discover", "-s", "tests", "-p", "test_b.py"],
                ],
            )

            # unittest with tests/__init__.py (includes -t <root>)
            (root / "tests" / "__init__.py").write_text("", encoding="utf-8")
            unit_cmds_with_init = targeted.build_commands(
                "/py", "unittest", ["tests/test_a.py"], root
            )
            self.assertEqual(
                unit_cmds_with_init,
                [["/py", "-m", "unittest", "discover", "-s", "tests", "-p", "test_a.py", "-t", str(root)]],
            )

    def test_unittest_runs_with_and_without_init_py(self):
        for with_init in (False, True):
            with self.subTest(with_init=with_init), tempfile.TemporaryDirectory() as tmp:
                root = _make_repo(tmp)
                if with_init:
                    (root / "tests" / "__init__.py").write_text("", encoding="utf-8")
                cmds = targeted.build_commands(sys.executable, "unittest", ["tests/test_alpha.py"], root)
                res = targeted.execute_commands(cmds, root, "unittest", timeout=30)
                self.assertEqual(res["exit_code"], 0, res["output"])
                self.assertIn("Ran 1 test", res["output"])

    def test_timeout_kills_process_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "tests").mkdir()
            (root / "tests" / "test_slow.py").write_text(
                "import time, unittest\n\n"
                "class TestSlow(unittest.TestCase):\n"
                "    def test_hang(self):\n"
                "        time.sleep(30)\n",
                encoding="utf-8",
            )
            cmds = targeted.build_commands(sys.executable, "unittest", ["tests/test_slow.py"], root)
            start = time.monotonic()
            res = targeted.execute_commands(cmds, root, "unittest", timeout=0.5)
            elapsed = time.monotonic() - start
            self.assertTrue(res["timed_out"])
            self.assertNotEqual(res["exit_code"], 0)
            self.assertIn("逾時", res["output"])
            self.assertLess(elapsed, 15.0)

    def test_zero_tests_collected_is_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "tests").mkdir()
            (root / "tests" / "test_empty.py").write_text("X = 1\n", encoding="utf-8")
            cmds = targeted.build_commands(sys.executable, "unittest", ["tests/test_empty.py"], root)
            res = targeted.execute_commands(cmds, root, "unittest", timeout=30)
            self.assertNotEqual(res["exit_code"], 0)
            self.assertIn("零個測試", res["output"])

    def test_subprocess_does_not_inherit_agora_controller_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "tests").mkdir()
            (root / "tests" / "test_env.py").write_text(
                "import os, unittest\n\n"
                "class TestEnv(unittest.TestCase):\n"
                "    def test_clean_env(self):\n"
                "        for k in ('AGORA_CHECK_RECORD', 'AGORA_LOCKED_COMMANDS', 'AGORA_DISPATCH_MANIFEST', 'AGORA_DISPATCH_ID'):\n"
                "            self.assertNotIn(k, os.environ)\n",
                encoding="utf-8",
            )
            cmds = targeted.build_commands(sys.executable, "unittest", ["tests/test_env.py"], root)
            dirty_env = {
                **os.environ,
                worktree.RECORD_ENV: "record.json",
                parties.LOCKED_ENV: "{}",
                worktree.MANIFEST_ENV: "manifest.json",
                worktree.DISPATCH_ENV: "d1",
            }
            res = targeted.execute_commands(cmds, root, "unittest", env=dirty_env, timeout=30)
            self.assertEqual(res["exit_code"], 0, res["output"])


class TestIsolatedWorkflowAndSlots(unittest.TestCase):
    def test_refuses_outside_isolated_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = cli.Workspace(Path(tmp))
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop(worktree.MANIFEST_ENV, None)
                with self.assertRaises(SystemExit) as ctx:
                    targeted.cmd_test(ws, argparse.Namespace())
            self.assertIn("只能在隔離分派中使用", str(ctx.exception.code))
            self.assertIn("agora check", str(ctx.exception.code))

    def test_auto_pick_empty_exits_nonzero_with_targets_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_repo(tmp)
            _, meta = _make_isolated_dispatch(root)
            wt = Path(meta["worktree"])
            (wt / "pkg" / "lonely.py").write_text("X = 99\n", encoding="utf-8")
            env = worktree.environment(meta)
            with patch.dict(os.environ, env):
                ws = cli.Workspace(wt)
                with self.assertRaises(SystemExit) as ctx:
                    targeted.cmd_test(ws, argparse.Namespace())
            msg = str(ctx.exception.code)
            self.assertIn(str(targeted.targets_file_path(meta)), msg)

    def test_end_to_end_isolated_test_writes_log_and_never_touches_check_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_repo(tmp)
            _, meta = _make_isolated_dispatch(root)
            wt = Path(meta["worktree"])
            targets_file = targeted.targets_file_path(meta)
            targets_file.write_text("# comment\ntests/test_alpha.py\n", encoding="utf-8")
            env = worktree.environment(meta)
            env[parties.LOCKED_ENV] = json.dumps(meta["commands"], ensure_ascii=False)
            out = io.StringIO()
            with patch.dict(os.environ, env), \
                    patch.object(cli, "checked_task_environment", lambda ws, refresh=False: _noop_ctx()), \
                    patch.object(worktree, "python_path", return_value=Path(sys.executable)), \
                    patch("sys.stdout", out):
                ws = cli.Workspace(wt)
                code = targeted.run_targeted_tests(ws, timeout=30)
            self.assertEqual(code, 0, out.getvalue())
            lines = [line for line in out.getvalue().splitlines() if line.strip()]
            self.assertIn("目標檔", lines[0])
            self.assertIn("tests/test_alpha.py", lines[0])
            self.assertIn("局部測試，不作為提交依據", lines[-1])
            self.assertIn("exit=0", lines[-1])
            logs = list(Path(meta["manifest"]).parent.glob("test-*.log"))
            self.assertEqual(len(logs), 1)
            self.assertIn("Ran 1 test", logs[0].read_text(encoding="utf-8"))
            # 不寫 meta.check_result
            fresh_meta = json.loads(Path(meta["manifest"]).read_text(encoding="utf-8"))
            self.assertIsNone(fresh_meta.get("check_result"))

    def test_test_slots_and_check_slots_do_not_block_each_other(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            entered = threading.Event()
            release = threading.Event()

            def hold_check_slot():
                with checkqueue.acquire_slot(root, limit=1, label="full-check", output=lambda _: None):
                    entered.set()
                    release.wait(timeout=10)

            t = threading.Thread(target=hold_check_slot)
            t.start()
            try:
                self.assertTrue(entered.wait(timeout=5))
                # check-slots 已滿（1/1），但 test-slots 互不佔用，應立即取得
                start = time.monotonic()
                with checkqueue.acquire_slot(
                    root,
                    limit=targeted.TEST_CONCURRENCY,
                    label="targeted",
                    dirname=checkqueue.TEST_SLOTS_DIRNAME,
                    output=lambda _: None,
                ) as slot:
                    self.assertEqual(slot["slot_path"].parent.name, checkqueue.TEST_SLOTS_DIRNAME)
                self.assertLess(time.monotonic() - start, 3.0)
            finally:
                release.set()
                t.join(timeout=5)

    def test_same_dispatch_cannot_run_targeted_test_concurrently(self):
        with tempfile.TemporaryDirectory() as tmp:
            dispatch_dir = Path(tmp)
            with targeted.dispatch_test_lock(dispatch_dir):
                with self.assertRaises(targeted.TargetedError) as ctx:
                    with targeted.dispatch_test_lock(dispatch_dir):
                        pass
            self.assertIn("同時只能執行一次", str(ctx.exception))


class TestPermissionsAndPrompts(unittest.TestCase):
    def test_agent_commands_antigravity_permissions_and_outdated_status(self):
        self.assertIn("test", cli.AGENT_COMMANDS)
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as home:
            ws = cli.Workspace(Path(tmp))
            with patch.object(Path, "home", return_value=Path(home)), patch("sys.stdout", io.StringIO()):
                cli.cmd_install(ws, None)
                settings_path = Path(home) / ".gemini" / "antigravity-cli" / "settings.json"
                data = json.loads(settings_path.read_text(encoding="utf-8"))
                allow = data["permissions"]["allow"]
                plan = cli.install_plan()
                expected_rule = f"command({plan['cmd']} test)"
                self.assertIn(expected_rule, allow)
                self.assertEqual(cli.install_status(ws)["state"], "installed")

                # 模擬舊安裝（不含 test）：install_status 應判為 outdated
                data["permissions"]["allow"] = [r for r in allow if not r.endswith(" test)")]
                settings_path.write_text(json.dumps(data), encoding="utf-8")
                status = cli.install_status(ws)
                self.assertEqual(status["state"], "outdated")
                self.assertIn("Antigravity 指令權限", status["outdated"])

    def test_claude_allowed_tools_includes_test(self):
        seen = {}

        def fake_run(cmd, *args, **kwargs):
            seen["cmd"] = cmd
            return [{"type": "result", "result": "ok", "session_id": "s"}], ""

        with tempfile.TemporaryDirectory() as tmp, patch.object(parties, "_run", fake_run):
            parties.call_claude("做事", None, {}, True, Path(tmp), 60, Path(tmp))
        self.assertIn(f"Bash({parties.agora_cmd()} test*)", seen["cmd"])

    def test_codex_and_antigravity_receive_test_slots_in_add_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_repo(tmp)
            thread, meta = _make_isolated_dispatch(root)
            test_slots = checkqueue.slots_dir(root, dirname=checkqueue.TEST_SLOTS_DIRNAME)
            test_slots.mkdir(parents=True, exist_ok=True)
            seen_codex = {}

            def fake_run_codex(cmd, *args, **kwargs):
                seen_codex["cmd"] = cmd
                return [{"type": "thread.started", "thread_id": "th1"},
                        {"type": "item.completed", "item": {"type": "agent_message", "text": "ok"}}], ""

            with patch.object(parties, "_run", fake_run_codex), \
                    patch.object(parties.quota, "update_codex_snapshot_from_session"):
                parties.call_codex(
                    "hi", None, {"add_dirs": [str(thread), str(test_slots)]},
                    True, Path(meta["worktree"]), 30, thread,
                )
            add_dirs = [seen_codex["cmd"][i + 1] for i, x in enumerate(seen_codex["cmd"]) if x == "--add-dir"]
            self.assertIn(str(test_slots), add_dirs)

    def test_resolve_runner_errors_on_unknown_framework_or_missing_pytest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            with self.assertRaises(targeted.TargetedError) as ctx:
                targeted.resolve_runner(root, ["npm", "test"])
            self.assertIn("無法推導測試框架", str(ctx.exception))

            with patch.object(cli, "has_module", return_value=False):
                with self.assertRaises(targeted.TargetedError) as ctx:
                    targeted.resolve_runner(root, [sys.executable, "-m", "pytest", "-q"])
                self.assertIn("缺少測試套件", str(ctx.exception))

            with patch.object(cli, "has_module", return_value=True):
                info = targeted.resolve_runner(root, [sys.executable, "-m", "pytest", "-q"])
                self.assertEqual(info["framework"], "pytest")
                self.assertEqual(info["python"], os.path.abspath(sys.executable))

    def test_cmd_work_prompt_and_add_dirs_for_isolated_vs_non_isolated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _make_repo(tmp)
            ws = cli.Workspace(root)
            thread, meta = _make_isolated_dispatch(root, "disp-prompt-1")
            state = {
                "topic": "t",
                "parties": {"claude": {"session_id": None, "seen": 0, "model": None},
                            "codex": {"session_id": None, "seen": 0, "model": None}},
                "messages": [],
                "relay": {
                    "worker": "codex", "from": "claude", "handoff": "工作單", "rounds": 0,
                    "status": "starting", "dispatch_created": True,
                    **{k: meta.get(k) for k in worktree.FIELDS},
                    "allowed_paths": meta["allowed_paths"],
                },
            }
            cli.save_state(thread, state)
            (thread / "transcript.md").write_text("", encoding="utf-8")
            (thread / "progress.md").write_text("# 進度\n", encoding="utf-8")
            (thread / "control.json").write_text('{"stop_requested": false}', encoding="utf-8")

            seen = {}

            def fake_codex(prompt, session_id, opts, work, workspace, timeout, scratch, live=None):
                seen["prompt"] = prompt
                seen["add_dirs"] = list(opts.get("add_dirs") or [])
                return "完成一步\n【已暫停】", "s1", "1s"

            with patch.dict(cli.CALLERS, {"codex": fake_codex}), \
                    patch.object(cli, "party_usage", return_value={"remaining_pct": 80, "resets_at": time.time() + 3600, "updated_at": time.time()}), \
                    patch.object(cli, "git_status", return_value=""), \
                    patch.object(cli.quota, "refresh"), \
                    patch.object(worktree, "setup_environment"), \
                    patch.object(cli.shares, "dispatch_state", return_value={"dispatches": {"disp-prompt-1": {}}}), \
                    patch.object(cli.shares, "recover_dispatch", return_value={"dispatches": {"disp-prompt-1": {}}}), \
                    patch("sys.stdout", io.StringIO()):
                cli.cmd_work(ws, argparse.Namespace(thread="t1", dispatch_id="disp-prompt-1", run_id=None))

            self.assertIn("agora test", seen["prompt"].replace(cli.agora_cmd(), "agora"))
            self.assertIn(str(targeted.targets_file_path(meta)), seen["prompt"])
            expected_test_slots = str(checkqueue.slots_dir(root, dirname=checkqueue.TEST_SLOTS_DIRNAME))
            self.assertIn(expected_test_slots, seen["add_dirs"])

            # 非隔離分派不提 agora test
            state_non_iso = {
                "topic": "t",
                "parties": {"claude": {"session_id": None, "seen": 0, "model": None},
                            "codex": {"session_id": None, "seen": 0, "model": None}},
                "messages": [],
                "relay": {"worker": "codex", "from": "claude", "handoff": "工作單", "rounds": 0, "status": "starting"},
            }
            cli.save_state(thread, state_non_iso)
            seen_non_iso = {}

            def fake_codex_non_iso(prompt, session_id, opts, work, workspace, timeout, scratch, live=None):
                seen_non_iso["prompt"] = prompt
                return "完成一步\n【已暫停】", "s1", "1s"

            with patch.dict(cli.CALLERS, {"codex": fake_codex_non_iso}), \
                    patch.object(cli, "party_usage", return_value=None), \
                    patch.object(cli, "git_status", return_value=""), \
                    patch.object(cli.quota, "refresh"), \
                    patch("sys.stdout", io.StringIO()):
                cli.cmd_work(ws, argparse.Namespace(thread="t1"))

            self.assertNotIn("agora test", seen_non_iso["prompt"].replace(cli.agora_cmd(), "agora"))


class _noop_ctx:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


if __name__ == "__main__":
    unittest.main()
