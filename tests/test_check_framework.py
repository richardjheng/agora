"""Tests for custom test entrypoint declaration (`check_framework`, #16)."""

import argparse
import io
import json
import os
import sys
import tempfile
import time
import unittest
import venv
from pathlib import Path
from unittest.mock import patch

from agoralib import baseline, cli, completions, config, merge, parties, targeted, worktree

try:
    from . import test_worktree as fixtures
except ImportError:
    import test_worktree as fixtures


class _NoopCtx:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class TestCheckFrameworkValidation(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        (self.root / "ops").mkdir()
        (self.root / "ops" / "run_tests.py").write_text("print('ok')\n", encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def test_valid_minimal_and_full_specs(self):
        self.assertIsNone(config.validate_check_framework(None, root=self.root))
        minimal = config.validate_check_framework(
            {"output": "unittest", "rerun": ["ops/run_tests.py", "{ids}"]},
            root=self.root,
        )
        self.assertEqual(minimal["output"], "unittest")
        self.assertEqual(minimal["script"], "ops/run_tests.py")
        self.assertEqual(minimal["ids_index"], 1)
        self.assertEqual(minimal["env"], {})
        self.assertIsNone(minimal["target_format"])

        full = config.validate_check_framework(
            {
                "output": "pytest",
                "rerun": ["ops/run_tests.py", "--fast", "{ids}", "--no-header"],
                "env": {"TAIXU_TEST_DATA_DIFF": "1", "MY_FLAG_2": "yes"},
                "target_format": "path",
            },
            root=self.root,
        )
        self.assertEqual(full["output"], "pytest")
        self.assertEqual(full["ids_index"], 2)
        self.assertEqual(full["env"], {"TAIXU_TEST_DATA_DIFF": "1", "MY_FLAG_2": "yes"})
        self.assertEqual(full["target_format"], "path")

    def test_rejects_invalid_output_rerun_paths_env_and_target_format(self):
        valid_base = {"output": "unittest", "rerun": ["ops/run_tests.py", "{ids}"]}
        cases = [
            ("not_dict", "unittest", "JSON 物件"),
            ("empty_dict", {}, "output"),
            ("bad_output", {**valid_base, "output": "nose"}, "output"),
            ("missing_rerun", {"output": "unittest"}, "rerun"),
            ("rerun_string", {"output": "unittest", "rerun": "ops/run_tests.py {ids}"}, "rerun"),
            ("rerun_no_ids", {"output": "unittest", "rerun": ["ops/run_tests.py"]}, "{ids}"),
            ("rerun_two_ids", {"output": "unittest", "rerun": ["ops/run_tests.py", "{ids}", "{ids}"]}, "{ids}"),
            ("rerun_extra_braces", {"output": "unittest", "rerun": ["ops/run_tests.py", "--k={foo}", "{ids}"]}, "'{'"),
            ("rerun_leading_dash", {"output": "unittest", "rerun": ["-m", "{ids}"]}, "'-'"),
            ("rerun_python_exe", {"output": "unittest", "rerun": ["python", "ops/run_tests.py", "{ids}"]}, ".py"),
            ("rerun_abs_posix", {"output": "unittest", "rerun": ["/ops/run_tests.py", "{ids}"]}, "絕對路徑"),
            ("rerun_abs_win", {"output": "unittest", "rerun": ["C:/ops/run_tests.py", "{ids}"]}, "絕對路徑"),
            ("rerun_dotdot", {"output": "unittest", "rerun": ["ops/../ops/run_tests.py", "{ids}"]}, "'..'"),
            ("rerun_missing", {"output": "unittest", "rerun": ["ops/missing.py", "{ids}"]}, "不存在"),
            ("env_not_dict", {**valid_base, "env": ["A=1"]}, "env"),
            ("env_non_str_val", {**valid_base, "env": {"FOO": 1}}, "字串"),
            ("env_lowercase_key", {**valid_base, "env": {"foo": "1"}}, "鍵名不合法"),
            ("env_leading_digit", {**valid_base, "env": {"1FOO": "1"}}, "鍵名不合法"),
            ("env_path", {**valid_base, "env": {"PATH": "/tmp"}}, "受保護"),
            ("env_pythonpath", {**valid_base, "env": {"PYTHONPATH": "/tmp"}}, "受保護"),
            ("env_pythonhome", {**valid_base, "env": {"PYTHONHOME": "/tmp"}}, "受保護"),
            ("env_virtual_env", {**valid_base, "env": {"VIRTUAL_ENV": "/tmp"}}, "受保護"),
            ("env_agora_var", {**valid_base, "env": {"AGORA_WORKSPACE": "/tmp"}}, "受保護"),
            ("bad_target_format", {**valid_base, "target_format": "dotted"}, "target_format"),
            ("empty_target_format", {**valid_base, "target_format": ""}, "target_format"),
        ]
        for label, spec, expected in cases:
            with self.subTest(case=label):
                with self.assertRaises(ValueError) as caught:
                    config.validate_check_framework(spec, root=self.root)
                self.assertIn(expected, str(caught.exception))

    def test_assign_rejects_invalid_check_framework_before_creating_worktree(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            worktree.git(root, "init", "-b", "main")
            worktree.git(root, "config", "user.email", "t@localhost")
            worktree.git(root, "config", "user.name", "T")
            (root / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
            worktree.git(root, "add", ".")
            worktree.git(root, "commit", "-m", "init")
            thread = root / ".agora" / "threads" / "t1"
            thread.mkdir(parents=True)
            cli.save_state(thread, {"topic": "cf-invalid", "parties": {}, "messages": []})
            config.save(root, {
                "parties": {"codex": {"enabled": True}, "antigravity": {"enabled": False}},
                "check_framework": {"output": "unittest", "rerun": ["ops/missing.py", "{ids}"]},
            })
            ws = cli.Workspace(root)
            args = argparse.Namespace(
                thread="t1", sender="claude", to=None, message="work", file=None,
                session=None, from_summary=False, yes=False, tier=None, model=None, effort=None,
                subagents=None, trigger=None, allow=["app.py"], dispatch_id="badcf1",
                reason=None, user_override=False, resume=None, name=None,
            )
            with patch.dict(os.environ, {cli.NESTED_ENV: ""}), \
                    patch.object(cli.sandbox, "network_restricted", return_value=False), \
                    patch.object(cli.sandbox, "is_codex_sandbox", return_value=False):
                with self.assertRaises(SystemExit) as caught:
                    cli.cmd_assign(ws, args)
            self.assertIn("check_framework", str(caught.exception.code))
            self.assertIn("不存在", str(caught.exception.code))
            self.assertFalse((root / ".agora" / "worktrees" / "badcf1").exists())
            self.assertEqual(worktree.git(root, "branch", "--list", "codex/*"), "")


class TestCheckFrameworkFreezeAndRebase(unittest.TestCase):
    def test_guarded_and_locked_keys_include_check_framework(self):
        self.assertIn("check_framework", cli.GUARDED_KEYS)
        self.assertIn("check_framework", cli.LOCKED_KEYS)

    def test_guard_config_restores_tampered_check_framework(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, thread = Path(tmp), Path(tmp) / "thread"
            thread.mkdir()
            original_cf = {"output": "unittest", "rerun": ["ops/run_tests.py", "{ids}"]}
            config.save(root, {"check": ["python", "-V"], "check_framework": original_cf})
            original = cli.read_config_text(root)
            config.save(root, {"check": ["python", "-V"], "check_framework": {"output": "pytest", "rerun": ["evil.py", "{ids}"]}})
            self.assertTrue(cli.guard_config(root, original, thread, "codex"))
            self.assertEqual(config.load(root)["check_framework"], original_cf)
            self.assertIn("check_framework", (thread / "transcript.md").read_text(encoding="utf-8"))

    def test_rebase_command_preserves_check_framework_dict_and_strings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            meta = {"control_root": str(root), "worktree": str(root / ".agora" / "worktrees" / "d1")}
            cf = {
                "output": "unittest",
                "rerun": ["ops/run_tests.py", "{ids}"],
                "env": {"PY_HINT": "python", "ROOT_HINT": str(root)},
                "target_format": "module",
            }
            rebased = worktree.rebase_command(
                {"build": None, "check": [sys.executable, str(root / "ops/run_tests.py")], "check_framework": cf},
                meta,
            )
            self.assertEqual(rebased["check_framework"], cf)
            self.assertEqual(rebased["check"][0], str(worktree.python_path(Path(meta["worktree"]))))
            # Direct dict pass also never crashes or rewrites check_framework fields
            self.assertEqual(worktree.rebase_command(cf, meta), cf)


class TestCheckFrameworkBaselineComparison(unittest.TestCase):
    setUp = fixtures.WorktreeTests.setUp
    task = fixtures.WorktreeTests.task
    message = fixtures.WorktreeTests.message
    modify = fixtures.WorktreeTests.modify

    def _prepare_custom_repo(self, *, test_source=None, runner_script=None, env_cfg=None, check_framework=None):
        (self.root / "ops").mkdir(exist_ok=True)
        (self.root / "tests").mkdir(exist_ok=True)
        default_runner = (
            "import os, sys, unittest\n"
            "assert os.environ.get('CUSTOM_FLAG') == 'yes', 'missing CUSTOM_FLAG'\n"
            "loader = unittest.TestLoader()\n"
            "if len(sys.argv) > 1:\n"
            "    sys.path.insert(0, 'tests')\n"
            "    suite = loader.loadTestsFromNames(sys.argv[1:])\n"
            "else:\n"
            "    suite = loader.discover('tests')\n"
            "runner = unittest.TextTestRunner(verbosity=2)\n"
            "res = runner.run(suite)\n"
            "sys.exit(0 if res.wasSuccessful() else 1)\n"
        )
        (self.root / "ops" / "run_tests.py").write_text(runner_script or default_runner, encoding="utf-8")
        default_tests = (
            "import unittest, app\n"
            "class T(unittest.TestCase):\n"
            "    def test_old(self):\n"
            "        self.assertEqual(app.VALUE, -1)\n"
            "    def test_new(self):\n"
            "        self.assertLess(app.VALUE, 3)\n"
        )
        (self.root / "tests" / "test_demo.py").write_text(test_source or default_tests, encoding="utf-8")
        worktree.git(self.root, "add", "ops", "tests")
        worktree.git(self.root, "commit", "-m", "custom runner fixture")

        meta = self.task(allowed=["app.py", "tests/*", "ops/*"])
        target = Path(meta["worktree"])
        venv.EnvBuilder(with_pip=False).create(target / ".venv")
        check_cmd = [
            "python", "-c",
            "import os,runpy,sys;os.environ['CUSTOM_FLAG']='yes';"
            "sys.argv=['ops/run_tests.py'];runpy.run_path('ops/run_tests.py',run_name='__main__')",
        ]
        cf = check_framework if check_framework is not None else {
            "output": "unittest",
            "rerun": ["ops/run_tests.py", "{ids}"],
            "env": env_cfg if env_cfg is not None else {"CUSTOM_FLAG": "yes"},
            "target_format": "module",
        }
        meta.update(
            environment_status="ready",
            commands={"build": None, "check": check_cmd, "check_framework": cf},
        )
        worktree.save(meta)
        self.modify(meta)
        self.message(meta)
        return meta

    def _run_check(self, meta):
        with patch.object(cli, "AGORA_ROOT_DIR", Path(cli.__file__).resolve().parents[1]):
            return cli.run_task_check(meta)

    def test_custom_runner_end_to_end_preexisting_new_env_and_cache_key(self):
        meta = self._prepare_custom_repo()
        with self.assertRaises(worktree.WorktreeError) as caught:
            worktree.finalize(meta, self._run_check)
        err = str(caught.exception)
        self.assertEqual(meta["baseline_comparison"]["classifications"], {"test_demo.T.test_old": "preexisting"})
        self.assertIn("既有失敗：test_demo.T.test_old", err)
        self.assertIn("--accept-preexisting", err)
        first_key = meta["baseline_comparison"]["cache_key"]

        # Changing check_framework changes cache_key
        meta["commands"]["check_framework"]["env"]["EXTRA_VAR"] = "2"
        worktree.save(meta)
        with self.assertRaises(worktree.WorktreeError):
            worktree.finalize(meta, self._run_check)
        self.assertFalse(meta["baseline_comparison"]["cache_hit"])
        self.assertNotEqual(meta["baseline_comparison"]["cache_key"], first_key)

        # Approving preexisting failure commits cleanly
        worktree.finalize(meta, self._run_check, accept_preexisting=True, reason="既有失敗已核對", sender="claude")
        self.assertEqual(meta["commit_status"], "committed")
        self.assertTrue(meta["baseline_comparison"]["cache_hit"])

    def test_custom_runner_mixed_new_and_preexisting(self):
        meta = self._prepare_custom_repo()
        self.modify(meta, text="VALUE = 3\n")
        with self.assertRaises(worktree.WorktreeError) as caught:
            worktree.finalize(meta, self._run_check, accept_preexisting=True, reason="no")
        self.assertEqual(
            meta["baseline_comparison"]["classifications"],
            {"test_demo.T.test_new": "new", "test_demo.T.test_old": "preexisting"},
        )
        self.assertIn("新失敗：test_demo.T.test_new", str(caught.exception))
        self.assertIn("既有失敗：test_demo.T.test_old", str(caught.exception))

    def test_custom_runner_base_importing_dispatch_source_marked_unknown(self):
        # Simulate a base rerun that erroneously imports a module from the dispatch worktree.
        leaky_runner = (
            "import importlib.util, os, pathlib, sys, unittest\n"
            "dispatch_root = os.environ.get('AGORA_WORKTREE')\n"
            "cwd = pathlib.Path.cwd().resolve()\n"
            "if dispatch_root and pathlib.Path(dispatch_root).resolve() != cwd:\n"
            "    spec = importlib.util.spec_from_file_location('leaked_dispatch_app', pathlib.Path(dispatch_root) / 'app.py')\n"
            "    mod = importlib.util.module_from_spec(spec)\n"
            "    sys.modules['leaked_dispatch_app'] = mod\n"
            "    spec.loader.exec_module(mod)\n"
            "loader = unittest.TestLoader()\n"
            "if len(sys.argv) > 1:\n"
            "    sys.path.insert(0, 'tests')\n"
            "    suite = loader.loadTestsFromNames(sys.argv[1:])\n"
            "else:\n"
            "    suite = loader.discover('tests')\n"
            "res = unittest.TextTestRunner(verbosity=2).run(suite)\n"
            "sys.exit(0 if res.wasSuccessful() else 1)\n"
        )
        meta = self._prepare_custom_repo(runner_script=leaky_runner, env_cfg={})
        with self.assertRaises(worktree.WorktreeError) as caught:
            worktree.finalize(meta, self._run_check, accept_preexisting=True, reason="should fail")
        self.assertEqual(meta["baseline_comparison"]["classifications"], {"test_demo.T.test_old": "unknown"})
        self.assertIn("無法比對失敗項目（不代表沒有新失敗）", str(caught.exception))
        self.assertIn("誤載入分派原始碼", str(caught.exception))
        self.assertNotIn("新失敗：無", str(caught.exception))

    def test_invalid_check_framework_during_compare_marks_unknown_with_reason(self):
        meta = self._prepare_custom_repo(check_framework={"output": "unittest", "rerun": ["ops/missing.py", "{ids}"]})
        with self.assertRaises(worktree.WorktreeError) as caught:
            worktree.finalize(meta, self._run_check)
        self.assertEqual(meta["baseline_comparison"]["classifications"], {"<unparsed>": "unknown"})
        self.assertIn("不存在", meta["baseline_comparison"]["error"])
        self.assertIn("無法比對失敗項目（不代表沒有新失敗）", str(caught.exception))
        self.assertNotIn("新失敗：無", str(caught.exception))

    def test_rerun_rejects_unsafe_test_id_without_executing_subprocess(self):
        meta = self._prepare_custom_repo()
        selected = baseline.plan_custom(meta["commands"]["check_framework"], root=Path(meta["worktree"]))
        with tempfile.TemporaryDirectory() as log_dir, patch.object(baseline.subprocess, "run") as mock_run:
            res = baseline.rerun(meta, Path(meta["worktree"]), selected, ["--evil-flag"], Path(log_dir), "base")
        mock_run.assert_not_called()
        self.assertEqual(res["--evil-flag"]["state"], "unknown")
        self.assertIn("不合格的測試 ID", res["--evil-flag"]["reason"])


class TestBaselineDescribe(unittest.TestCase):
    def test_undeclared_unknown_framework_hides_empty_categories_and_hints_config(self):
        record = {
            "dispatch_id": "d1",
            "thread": "/tmp/t1",
            "baseline_comparison": {
                "classifications": {"<unparsed>": "unknown"},
                "error": "未知測試框架",
            },
        }
        text = baseline.describe(record)
        self.assertNotIn("新失敗：無", text)
        self.assertNotIn("既有失敗：無", text)
        self.assertNotIn("不穩定：無", text)
        self.assertIn("無法比對失敗項目（不代表沒有新失敗）：未知測試框架", text)
        self.assertIn("自訂驗證指令請在 .agora/config.json 宣告 check_framework", text)

        # Also works when formatted via completions.describe
        comp_text = completions.describe({
            "id": "x" * 12,
            "thread": "t1",
            "topic": "t",
            "type": "relay",
            "dispatch_id": "d1",
            "status": "failed",
            "baseline_comparison": record["baseline_comparison"],
        })
        self.assertNotIn("新失敗：無", comp_text)
        self.assertIn("不代表沒有新失敗", comp_text)
        self.assertIn("check_framework", comp_text)

    def test_partial_unknown_prints_only_nonempty_categories(self):
        record = {
            "dispatch_id": "d1",
            "thread": "/tmp/t1",
            "baseline_comparison": {
                "classifications": {"m.T.test_new": "new", "m.T.test_bad": "unknown"},
                "base": {"m.T.test_bad": {"state": "unknown", "reason": "重跑輸出不完整或異常退出"}},
                "dispatch": {"m.T.test_bad": {"state": "failed"}},
            },
        }
        text = baseline.describe(record)
        self.assertIn("新失敗：m.T.test_new", text)
        self.assertIn("無法比對：m.T.test_bad", text)
        self.assertNotIn("既有失敗：無", text)
        self.assertNotIn("不穩定：無", text)
        self.assertIn("無法比對失敗項目（不代表沒有新失敗）：重跑輸出不完整或異常退出", text)
        self.assertNotIn("宣告 check_framework", text)

    def test_normal_comparison_output_is_unchanged(self):
        record = {
            "dispatch_id": "d1",
            "thread": "/tmp/t1",
            "baseline_comparison": {
                "classifications": {"m.T.test_old": "preexisting"},
            },
        }
        text = baseline.describe(record)
        self.assertIn("新失敗：無", text)
        self.assertIn("既有失敗：m.T.test_old", text)
        self.assertIn("不穩定：無", text)
        self.assertIn("無法比對：無", text)
        self.assertIn("--accept-preexisting", text)
        self.assertNotIn("不代表沒有新失敗", text)


class TestTargetedCustomFramework(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        (self.root / "ops").mkdir()
        (self.root / "ops" / "run_tests.py").write_text(
            "import os, sys, unittest\n"
            "assert os.environ.get('TAIXU_FLAG') == '1'\n"
            "sys.path.insert(0, 'tests')\n"
            "loader = unittest.TestLoader()\n"
            "suite = loader.loadTestsFromNames(sys.argv[1:])\n"
            "res = unittest.TextTestRunner(verbosity=2).run(suite)\n"
            "sys.exit(0 if res.wasSuccessful() else 1)\n",
            encoding="utf-8",
        )
        (self.root / "tests" / "sub").mkdir(parents=True)
        (self.root / "tests" / "test_a.py").write_text(
            "import unittest\nclass A(unittest.TestCase):\n    def test_ok(self): self.assertTrue(True)\n",
            encoding="utf-8",
        )
        (self.root / "tests" / "sub" / "test_b.py").write_text(
            "import unittest\nclass B(unittest.TestCase):\n    def test_ok(self): self.assertTrue(True)\n",
            encoding="utf-8",
        )

    def tearDown(self):
        self._tmp.cleanup()

    def test_build_commands_for_path_and_module_formats(self):
        cf_path = {
            "output": "pytest",
            "rerun": ["ops/run_tests.py", "--fast", "{ids}"],
            "target_format": "path",
        }
        cmds_path = targeted.build_commands(
            "/py", "pytest", ["tests/test_a.py", "tests/sub/test_b.py::test_ok"], self.root,
            check_framework=cf_path,
        )
        self.assertEqual(
            cmds_path,
            [["/py", "ops/run_tests.py", "--fast", "tests/test_a.py", "tests/sub/test_b.py::test_ok"]],
        )

        cf_mod = {
            "output": "unittest",
            "rerun": ["ops/run_tests.py", "{ids}", "--strict"],
            "target_format": "module",
        }
        cmds_mod = targeted.build_commands(
            "/py", "unittest", ["tests/test_a.py", "tests/sub/test_b.py"], self.root,
            check_framework=cf_mod,
        )
        self.assertEqual(
            cmds_mod,
            [["/py", "ops/run_tests.py", "test_a", "sub.test_b", "--strict"]],
        )

    def test_module_format_rejects_double_colon_targets(self):
        with self.assertRaises(targeted.TargetValidationError) as ctx:
            targeted.validate_targets(
                ["tests/test_a.py::A::test_ok"], self.root, "pytest", target_format="module"
            )
        self.assertIn("target_format=module", str(ctx.exception))

        with self.assertRaises(targeted.TargetedError):
            targeted.format_target("tests/test_a.py::test_ok", "module")

    def test_missing_target_format_exits_nonzero_with_prompt(self):
        cf_no_fmt = {"output": "unittest", "rerun": ["ops/run_tests.py", "{ids}"]}
        with self.assertRaises(targeted.TargetedError) as ctx:
            targeted.resolve_runner(self.root, ["python", "-c", "pass"], check_framework=cf_no_fmt)
        self.assertIn("自訂測試入口未宣告 target_format", str(ctx.exception))
        self.assertIn("check_framework", str(ctx.exception))

        # Undeclared check_framework + unknown check hints check_framework
        with self.assertRaises(targeted.TargetedError) as ctx2:
            targeted.resolve_runner(self.root, ["python", "-c", "pass"])
        self.assertIn("check_framework", str(ctx2.exception))

    def test_end_to_end_run_targeted_tests_with_custom_framework_and_env(self):
        worktree.git(self.root, "init", "-b", "main")
        worktree.git(self.root, "config", "user.email", "t@localhost")
        worktree.git(self.root, "config", "user.name", "T")
        worktree.git(self.root, "add", "-A")
        worktree.git(self.root, "commit", "-m", "init")

        thread = self.root / ".agora" / "threads" / "t1"
        thread.mkdir(parents=True)
        meta = worktree.prepare(self.root, "disp-cf-1", ["**"], thread, worker="codex", title="cf")
        cf = {
            "output": "unittest",
            "rerun": ["ops/run_tests.py", "{ids}"],
            "env": {"TAIXU_FLAG": "1"},
            "target_format": "module",
        }
        meta["commands"] = {"build": None, "check": ["python", "-c", "pass"], "check_framework": cf}
        worktree.save(meta)
        wt = Path(meta["worktree"])
        targets_file = targeted.targets_file_path(meta)
        targets_file.write_text("tests/test_a.py\ntests/sub/test_b.py\n", encoding="utf-8")

        env = worktree.environment(meta)
        env[parties.LOCKED_ENV] = json.dumps(meta["commands"], ensure_ascii=False)
        out = io.StringIO()
        with patch.dict(os.environ, env), \
                patch.object(cli, "checked_task_environment", lambda ws, refresh=False: _NoopCtx()), \
                patch.object(worktree, "python_path", return_value=Path(sys.executable)), \
                patch("sys.stdout", out):
            code = targeted.run_targeted_tests(cli.Workspace(wt), timeout=30)
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn("Ran 2 tests", out.getvalue())

        # Without target_format, run_targeted_tests exits non-zero with message
        meta["commands"]["check_framework"] = {"output": "unittest", "rerun": ["ops/run_tests.py", "{ids}"]}
        env[parties.LOCKED_ENV] = json.dumps(meta["commands"], ensure_ascii=False)
        with patch.dict(os.environ, env):
            with self.assertRaises(SystemExit) as caught:
                targeted.run_targeted_tests(cli.Workspace(wt), timeout=30)
        self.assertIn("自訂測試入口未宣告 target_format", str(caught.exception.code))

    def test_cmd_work_omits_agora_test_when_check_framework_lacks_target_format(self):
        worktree.git(self.root, "init", "-b", "main")
        worktree.git(self.root, "config", "user.email", "t@localhost")
        worktree.git(self.root, "config", "user.name", "T")
        worktree.git(self.root, "add", "-A")
        worktree.git(self.root, "commit", "-m", "init")

        ws = cli.Workspace(self.root)
        thread = self.root / ".agora" / "threads" / "t1"
        thread.mkdir(parents=True)
        for idx, (cf, expect_test) in enumerate((
            ({"output": "unittest", "rerun": ["ops/run_tests.py", "{ids}"]}, False),
            ({"output": "unittest", "rerun": ["ops/run_tests.py", "{ids}"], "target_format": "module"}, True),
        )):
            did = f"disp-work-{idx}"
            meta = worktree.prepare(self.root, did, ["**"], thread, worker="codex", title="cf")
            meta["commands"] = {"build": None, "check": ["python", "-c", "pass"], "check_framework": cf}
            worktree.save(meta)
            state = {
                "topic": "cf",
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
                return "完成\n【已暫停】", "s1", "1s"

            with patch.dict(cli.CALLERS, {"codex": fake_codex}), \
                    patch.object(cli.sandbox, "network_restricted", return_value=False), \
                    patch.object(cli.sandbox, "is_codex_sandbox", return_value=False), \
                    patch.object(cli, "party_usage", return_value={"remaining_pct": 80, "resets_at": time.time() + 3600, "updated_at": time.time()}), \
                    patch.object(cli, "git_status", return_value=""), \
                    patch.object(cli.quota, "refresh"), \
                    patch.object(worktree, "setup_environment"), \
                    patch.object(cli.shares, "dispatch_state", return_value={"dispatches": {did: {}}}), \
                    patch.object(cli.shares, "recover_dispatch", return_value={"dispatches": {did: {}}}), \
                    patch("sys.stdout", io.StringIO()):
                cli.cmd_work(ws, argparse.Namespace(thread="t1", dispatch_id=did, run_id=None))

            normalized = seen["prompt"].replace(cli.agora_cmd(), "agora")
            if expect_test:
                self.assertIn("agora test", normalized)
                self.assertIn(str(targeted.targets_file_path(meta)), seen["prompt"])
            else:
                self.assertNotIn("agora test", normalized)
                self.assertNotIn("test-targets.txt", normalized)


class TestMergeExtractWithCheckFramework(unittest.TestCase):
    def test_extract_failed_tests_follows_check_framework_output(self):
        unittest_out = (
            "FAIL: test_a (test_demo.T.test_a)\n"
            "Ran 2 tests in 0.01s\n"
            "FAILED (failures=1)\n"
        )
        pytest_out = (
            "FAILED tests/test_demo.py::test_a - AssertionError\n"
            "=== 1 failed, 1 passed in 0.05s ===\n"
        )
        custom_cmd = ["python", "-c", "runpy.run_path('ops/run_tests.py')"]

        ids, complete, fw = merge._extract_failed_tests(
            unittest_out, custom_cmd, 1,
            check_framework={"output": "unittest", "rerun": ["ops/run_tests.py", "{ids}"]},
        )
        self.assertEqual((ids, complete, fw), (["test_demo.T.test_a"], True, "unittest"))

        # Declaring output="pytest" on unittest output must strictly follow output (not fall back to unittest)
        ids_mismatch, complete_mismatch, fw_mismatch = merge._extract_failed_tests(
            unittest_out, custom_cmd, 1,
            check_framework={"output": "pytest", "rerun": ["ops/run_tests.py", "{ids}"]},
        )
        self.assertEqual((ids_mismatch, complete_mismatch, fw_mismatch), ([], False, None))

        ids_p, complete_p, fw_p = merge._extract_failed_tests(
            pytest_out, custom_cmd, 1,
            check_framework={"output": "pytest", "rerun": ["ops/run_tests.py", "{ids}"]},
        )
        self.assertEqual((ids_p, complete_p, fw_p), (["tests/test_demo.py::test_a"], True, "pytest"))


if __name__ == "__main__":
    unittest.main()
