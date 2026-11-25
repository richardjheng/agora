"""Dispatch environment regressions with pip subprocess calls mocked."""

import argparse
import hashlib
import io
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

from agoralib import cli, worktree

try:
    from . import test_worktree as fixtures
except ImportError:
    import test_worktree as fixtures


class EnvironmentTests(unittest.TestCase):
    task = fixtures.WorktreeTests.task

    def setUp(self):
        fixtures.WorktreeTests.setUp(self)
        self.meta = self.task()
        self.target = Path(self.meta["worktree"])
        self.log = Path(self.meta["manifest"]).with_name("environment.log")

    def mock_pip(self, *results):
        real_run = subprocess.run
        pip_run = Mock(side_effect=results or None,
                       return_value=subprocess.CompletedProcess([], 0, "", ""))

        def run(command, **kwargs):
            if command[1:4] == ["-m", "pip", "install"]:
                return pip_run(command, **kwargs)
            return real_run(command, **kwargs)

        runner = patch.object(worktree.subprocess, "run", side_effect=run)
        runner.start()
        self.addCleanup(runner.stop)
        return pip_run

    def write(self, filename, content):
        (self.target / filename).write_text(content, encoding="utf-8")

    def setup(self):
        with patch.object(worktree.venv.EnvBuilder, "create", side_effect=lambda path: path.mkdir()), \
                patch.object(worktree, "verify_environment") as verify, patch("sys.stdout", io.StringIO()):
            worktree.setup_environment(self.meta)
        verify.assert_called_once_with(self.meta)

    @unittest.skipIf(worktree.tomllib is None, "requires the standard TOML parser")
    def test_only_declared_development_and_test_extras_are_installed(self):
        self.write("pyproject.toml", "[project.optional-dependencies]\ntests = []\ndocs = []\ndev = []\n")
        self.write("requirements.txt", "# base\n")
        self.write("requirements-dev.txt", "# dev\n")
        pip = self.mock_pip()
        self.setup()
        self.assertEqual([args[0][4:] for args, _ in pip.call_args_list],
                         [["-r", "requirements.txt"], ["-r", "requirements-dev.txt"], ["-e", ".[dev,tests]"]])
        self.assertEqual(set(worktree.load(self.meta)["dependency_hashes"]),
                         {"pyproject.toml", "requirements.txt", "requirements-dev.txt"})
        for _, options in pip.call_args_list:
            self.assertEqual(options["encoding"], "utf-8")
            self.assertEqual(options["env"]["PYTHONUTF8"], "1")
            self.assertEqual(options["env"]["PYTHONIOENCODING"], "utf-8")

    def test_missing_tomllib_installs_basic_project_and_logs_fallback(self):
        self.write("pyproject.toml", "[project.optional-dependencies]\ntest = []\n")
        pip = self.mock_pip()
        with patch.object(worktree, "tomllib", None):
            self.setup()
        self.assertEqual(pip.call_args.args[0][4:], ["-e", "."])
        self.assertIn("略過 dev／test／tests extras", self.log.read_text(encoding="utf-8"))

    def test_transient_errors_retry_with_increasing_delay_and_log_then_succeed(self):
        self.write("requirements.txt", "# fixture\n")
        errors = ["[WinError 32] 檔案被占用", "HTTP 503 Service Unavailable",
                  "Content-Type: Unknown\nERROR: from versions: none"]
        pip = self.mock_pip(*(subprocess.CompletedProcess([], 1, "", error) for error in errors),
                            subprocess.CompletedProcess([], 0, "", ""))
        with patch.dict(os.environ, {"AGORA_PIP_RETRY_DELAY": "0.5"}), patch.object(worktree.time, "sleep") as sleep:
            self.setup()
        self.assertEqual(pip.call_count, 4)
        self.assertEqual(sleep.call_args_list, [call(0.5), call(1), call(1.5)])
        self.assertEqual(self.log.read_text(encoding="utf-8").count("暫時性錯誤"), 3)
        self.assertEqual(worktree.load(self.meta)["environment_status"], "ready")

    def test_permanent_errors_and_versions_none_alone_fail_immediately(self):
        self.write("requirements.txt", "# fixture\n")
        for error in ("ERROR: invalid requirement", "ERROR: no matching distribution found (from versions: none)"):
            with self.subTest(error=error):
                with patch.object(worktree.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", error)) as run, \
                        patch.object(worktree.time, "sleep") as sleep:
                    with self.assertRaisesRegex(worktree.WorktreeError, "依賴安裝失敗"):
                        worktree._install_dependencies(self.meta, self.target, {"requirements.txt": "hash"})
                run.assert_called_once()
                sleep.assert_not_called()
        self.assertFalse(self.log.exists())

    def test_check_reinstalls_new_and_changed_declarations_before_verification_and_tests(self):
        self.setup()
        pip = self.mock_pip()
        ws = cli.Workspace(self.target)
        events = []
        pip.side_effect = lambda *args, **kwargs: events.append("pip") or subprocess.CompletedProcess([], 0, "", "")

        def check():
            with self.assertRaises(SystemExit) as exit_code:
                cli.cmd_check(ws, argparse.Namespace(task=None))
            self.assertEqual(exit_code.exception.code, 0)

        with patch.dict(os.environ, worktree.environment(self.meta)), patch("sys.stdout", io.StringIO()), \
                patch.object(worktree, "verify_environment", side_effect=lambda meta: events.append("verify")), \
                patch.object(cli, "command_config", return_value={"check": ["fake-test"]}), \
                patch.object(cli, "run_configured_command",
                             side_effect=lambda *args: events.append("test") or subprocess.CompletedProcess([], 0, "", "")):
            self.write("requirements-test.txt", "# added\n")
            check()
            self.assertEqual(events, ["pip", "verify", "test"])
            events.clear()
            check()
            self.assertEqual(events, ["verify", "test"])
            events.clear()
            self.write("requirements-test.txt", "# edited\n")
            check()
            self.assertEqual(events, ["pip", "verify", "test"])

        contents = (self.target / "requirements-test.txt").read_bytes()
        self.assertEqual(worktree.load(self.meta)["dependency_hashes"],
                         {"requirements-test.txt": hashlib.sha256(contents).hexdigest()})
        self.assertEqual(pip.call_count, 2)

    def test_project_process_cannot_see_verification_record(self):
        record = self.root / "outer-check.json"
        record.write_bytes(b'{"complete": false}')
        key = cli.failure_baseline.RECORD_ENV
        command = [sys.executable, "-c", f"import os; assert {key!r} not in os.environ"]
        with patch.dict(os.environ, {key: str(record)}):
            proc = cli.run_configured_command(command, self.target)
            self.assertEqual(os.environ[key], str(record))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(record.read_bytes(), b'{"complete": false}')

    def test_frozen_check_keeps_control_metadata_in_the_parent(self):
        self.setup()
        worktree.venv.EnvBuilder(with_pip=False).create(self.target / ".venv")
        record = self.root / "check.json"
        hidden = (cli.failure_baseline.RECORD_ENV, cli.LOCKED_ENV, worktree.MANIFEST_ENV,
                  worktree.CONTROL_ENV, worktree.DISPATCH_ENV, cli.jobs.JOB_ENV)
        env = worktree.environment(self.meta)
        command = [sys.executable, "-c",
                   f"import os; assert not set({hidden!r}) & os.environ.keys(); "
                   f"assert os.environ['VIRTUAL_ENV'] == {env['VIRTUAL_ENV']!r}; "
                   f"assert os.environ['PATH'].split(os.pathsep)[0] == {env['PATH'].split(os.pathsep)[0]!r}"]
        env.update({cli.failure_baseline.RECORD_ENV: str(record),
                    cli.LOCKED_ENV: json.dumps({"check": command}),
                    cli.jobs.JOB_ENV: str(self.thread)})
        with patch.dict(os.environ, env, clear=True), \
                patch("sys.stdout", io.StringIO()) as out, self.assertRaises(SystemExit) as exit_code:
            cli.cmd_check(cli.Workspace(self.target), argparse.Namespace(task=None))
        self.assertEqual(exit_code.exception.code, 0, out.getvalue())
        evidence = json.loads(record.read_text(encoding="utf-8"))
        self.assertEqual(evidence["check_command"], worktree.rebase_command(command, self.meta))
        self.assertEqual(evidence["exit_code"], 0)
        self.assertTrue(evidence["complete"])

    def test_lock_conflict_refuses_setup_and_preserves_metadata(self):
        with worktree.environment_lock(self.meta), patch.object(worktree.venv.EnvBuilder, "create") as create:
            with self.assertRaisesRegex(worktree.WorktreeError, "另一個 Agora 程序"):
                worktree.setup_environment(self.meta)
            create.assert_not_called()
        self.assertEqual(worktree.load(self.meta)["environment_status"], "pending")
        self.setup()

    def test_failed_recovery_reports_busy_directory_without_reusing_it(self):
        self.setup()
        self.meta["environment_status"] = "failed"
        worktree.save(self.meta)
        with patch.object(worktree.shutil, "rmtree", side_effect=PermissionError("sharing violation")), \
                patch.object(worktree.venv.EnvBuilder, "create") as create:
            with self.assertRaisesRegex(worktree.WorktreeError, "無法刪除分派 .venv"):
                worktree.setup_environment(self.meta)
        create.assert_not_called()
        self.assertEqual(worktree.load(self.meta)["environment_status"], "failed")

    def test_failed_recovery_refuses_a_venv_link_to_another_directory(self):
        outside = self.root / "keep-venv"
        outside.mkdir()
        try:
            (self.target / ".venv").symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"directory symlinks unavailable: {exc}")
        self.meta["environment_status"] = "failed"
        worktree.save(self.meta)
        with patch.object(worktree.shutil, "rmtree") as remove:
            with self.assertRaisesRegex(worktree.WorktreeError, "實體目錄"):
                worktree.setup_environment(self.meta)
        remove.assert_not_called()
        self.assertTrue(outside.is_dir())

