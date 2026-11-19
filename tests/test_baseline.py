"""Real temporary repositories and test processes; never AI, pip or network."""

import argparse
import importlib.util
import io
import json
import os
import sys
import unittest
import venv
from pathlib import Path
from unittest.mock import patch

from agoralib import baseline, checkqueue, cli, completions, worktree

# Optional existing dependency directory for offline integration testing.
PYTEST_SITE = os.environ.get("AGORA_TEST_PYTEST_SITE", "")
HAS_PYTEST = bool(importlib.util.find_spec("pytest") or (PYTEST_SITE and (Path(PYTEST_SITE) / "pytest").is_dir()))
try:
    from . import test_worktree as fixtures
except ImportError:
    import test_worktree as fixtures


class ParseTests(unittest.TestCase):
    def test_unittest_and_pytest_complete_reports(self):
        cases = [("unittest", "FAIL: test_a (test_demo.T.test_a)\nRan 2 tests in 0.001s\nFAILED (failures=1)\n", ["test_demo.T.test_a"]),
                 ("unittest", "ERROR: test_a (test_demo.T.test_a)\nRan 1 test in 0.001s\nFAILED (errors=1)\n", ["test_demo.T.test_a"]),
                 ("pytest", "FAILED tests/test_demo.py::T::test_a - AssertionError\n1 failed, 2 passed in 0.02s\n", ["tests/test_demo.py::T::test_a"]),
                 ("pytest", "ERROR tests/test_demo.py::test_a - ValueError\n=== 1 error in 0.02s ===\n", ["tests/test_demo.py::test_a"])]
        for framework, output, ids in cases:
            with self.subTest(framework=framework, output=output):
                result = baseline.parse(output, framework, 1)
                self.assertTrue(result["complete"])
                self.assertEqual(result["ids"], ids)

    def test_collection_truncation_duplicates_and_unknown_fail_closed(self):
        cases = [("unittest", "ERROR: test_a (unittest.loader._FailedTest.test_a)\nRan 1 test in 0.1s\nFAILED (errors=1)\n"),
                 ("unittest", "FAIL: test_a (pkg.T.test_a)\n"),
                 ("unittest", "Ran 1 test in 0.1s\nFAILED (failures=2)\n"),
                 ("unittest", "FAIL: test_a (pkg.T.test_a)\nRan 2 tests in 0.1s\nFAILED (failures=1, unexpected successes=1)\n"),
                 ("unittest", "ERROR: tearDownClass (pkg.mod.T)\nRan 1 test in 0.1s\nFAILED (errors=1)\n"),
                 ("pytest", "ERROR collecting test_a.py\nERROR test_a.py\n1 error in 0.1s\n"),
                 ("pytest", "FAILED test_a.py::test_a - error\nFAILED test_a.py::test_a - error\n2 failed in 0.1s\n"),
                 ("pytest", "FAILED test_a.py::test_a - error\n"),
                 ("other", "1 failed in 0.1s\n")]
        for framework, text in cases:
            with self.subTest(text=text):
                self.assertFalse(baseline.parse(text, framework, 1)["complete"])

    def test_warnings_and_expected_failures_are_not_extra_failed_tests(self):
        result = baseline.parse("FAILED test_a.py::test_a - error\n1 failed, 2 warnings in 0.1s\n", "pytest", 1)
        self.assertTrue(result["complete"])
        self.assertEqual(result["count"], 1)
        result = baseline.parse("FAIL: test_a (pkg.T.test_a)\nRan 3 tests in 0.1s\nFAILED (failures=1, expected failures=2)\n", "unittest", 1)
        self.assertTrue(result["complete"])
        self.assertEqual(result["ids"], ["pkg.T.test_a"])

    def test_plan_refuses_shell_and_unreproducible_options(self):
        for command in ("python -m pytest && echo ok", [["python", "-m", "pytest"]], ["python", "-m"],
                        ["/other/env/bin/pytest", "-q"],
                        ["python", "-m", "pytest", "-x"], ["python", "custom.py"],
                        ["python", "-m", "unittest", "discover", "-s", "../tests"]):
            with self.subTest(command=command), self.assertRaises(ValueError):
                baseline.plan(command)
        self.assertEqual(baseline.plan('python -m unittest discover -s tests')["start"], "tests")
        self.assertEqual(baseline.plan(["pytest", "-q"])["framework"], "pytest")


class BaselineTests(unittest.TestCase):
    setUp = fixtures.WorktreeTests.setUp
    task = fixtures.WorktreeTests.task
    message = fixtures.WorktreeTests.message
    modify = fixtures.WorktreeTests.modify

    def prepare(self, source=None, framework="unittest"):
        source = source or ("import unittest\nimport app\nclass T(unittest.TestCase):\n"
                            "    def test_old(self): self.assertEqual(app.VALUE, -1)\n"
                            "    def test_new(self): self.assertLess(app.VALUE, 3)\n")
        (self.root / "tests").mkdir()
        (self.root / "tests" / "test_demo.py").write_text(source, encoding="utf-8")
        worktree.git(self.root, "add", "tests")
        worktree.git(self.root, "commit", "-m", "test fixture")
        meta = self.task(allowed=["app.py", "tests/*", "requirements*.txt"])
        target = Path(meta["worktree"])
        venv.EnvBuilder(with_pip=False).create(target / ".venv")
        if framework == "pytest":
            # Reuse already installed test dependencies without pip/network. This
            # fixture's venv still supplies the executable for every real run.
            sites = ([PYTEST_SITE] if PYTEST_SITE else
                     [p for p in sys.path if p and Path(p).name in ("site-packages", "dist-packages")])
            site_dir = (target / ".venv" / "Lib" / "site-packages" if os.name == "nt" else
                        next((target / ".venv" / "lib").glob("python*/site-packages")))
            (site_dir / "fixture-dependencies.pth").write_text("\n".join(sites) + "\n", encoding="utf-8")
            os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
            os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
        meta.update(environment_status="ready", commands={"check": ["python", "-m", framework,
                    *(["discover", "-s", "tests"] if framework == "unittest" else ["-q"])]})
        worktree.save(meta)
        self.modify(meta)
        self.message(meta)
        return meta

    def check(self, meta):
        # The child must execute this checkout, not an installed older Agora.
        with patch.object(cli, "AGORA_ROOT_DIR", Path(cli.__file__).resolve().parents[1]):
            return cli.run_task_check(meta)

    def fail(self, meta=None, **kwargs):
        # unittest.TestCase.fail is retained for assertions invoking it internally.
        if not isinstance(meta, dict):
            return super().fail(meta)
        with self.assertRaises(worktree.WorktreeError) as caught:
            worktree.finalize(meta, self.check, **kwargs)
        self.assertIsNone(meta.get("commit_sha"))
        return str(caught.exception)

    def test_preexisting_requires_host_approval_and_audits_real_commit(self):
        meta = self.prepare()
        with patch.object(checkqueue, "acquire_slot", wraps=checkqueue.acquire_slot) as queued:
            error = self.fail(meta)
        self.assertIn("既有失敗", error)
        self.assertIn("--accept-preexisting", error)
        self.assertEqual(queued.call_args.args[0], self.root)
        self.assertEqual(meta["commit_status"], "check_failed_preexisting")
        first = dict(meta["check_result"])
        first_log = Path(first["log"]).read_bytes()
        worktree.finalize(meta, self.check, accept_preexisting=True, reason="已核對起點，另案修復", sender="claude")
        self.assertEqual(meta["commit_status"], "committed")
        self.assertEqual(meta["check_result"]["exit_code"], 1)
        self.assertEqual(Path(first["log"]).read_bytes(), first_log)
        self.assertTrue(meta["baseline_comparison"]["cache_hit"])
        self.assertTrue(baseline.approval_valid(meta, meta["checked_tree"]))
        message = worktree.git(Path(meta["worktree"]), "show", "-s", "--format=%B", "HEAD")
        self.assertIn("Agora-Preexisting-Failures: test_demo.T.test_old", message)
        self.assertIn("Agora-Approval: by=claude reason=已核對起點，另案修復", message)
        saved = json.loads(Path(meta["manifest"]).read_text(encoding="utf-8"))
        self.assertTrue(saved["preexisting_approvals"][0]["base"])
        # Recovery of a crash between update-ref and manifest save also verifies approval.
        sha = meta.pop("commit_sha")
        worktree.recover_commit(meta)
        self.assertEqual(meta["commit_sha"], sha)

    def test_mixed_new_and_old_cannot_be_approved(self):
        meta = self.prepare()
        self.modify(meta, text="VALUE = 3\n")
        error = self.fail(meta, accept_preexisting=True, reason="cannot waive new failures")
        self.assertEqual(meta["baseline_comparison"]["classifications"],
                         {"test_demo.T.test_new": "new", "test_demo.T.test_old": "preexisting"})
        self.assertIn("新失敗：test_demo.T.test_new", error)
        self.assertIn("既有失敗：test_demo.T.test_old", error)
        self.assertNotIn("主持方可核准", error)

    def test_new_only_cannot_be_approved(self):
        meta = self.prepare("import unittest, app\nclass T(unittest.TestCase):\n    def test_new(self): self.assertEqual(app.VALUE, 1)\n")
        self.fail(meta, accept_preexisting=True, reason="no")
        self.assertEqual(set(meta["baseline_comparison"]["classifications"].values()), {"new"})

    def test_flaky_retry_never_washes_initial_failure(self):
        source = ("import unittest, pathlib\nclass T(unittest.TestCase):\n"
                  "    def test_once(self):\n"
                  "        marker = pathlib.Path('.agora-once')\n"
                  "        seen = marker.exists()\n"
                  "        marker.write_text('seen')\n"
                  "        self.assertTrue(seen)\n")
        meta = self.prepare(source)
        # Keep the fixture's runtime marker out of the task tree.
        worktree.git(Path(meta["worktree"]), "config", "core.excludesFile", str(self.root / "runtime-ignore"))
        (self.root / "runtime-ignore").write_text(".agora-once\n", encoding="utf-8")
        self.fail(meta, accept_preexisting=True, reason="no")
        self.assertEqual(set(meta["baseline_comparison"]["classifications"].values()), {"flaky"})
        self.assertEqual(meta["check_result"]["exit_code"], 1)
        self.assertIn("FAIL:", Path(meta["check_result"]["log"]).read_text(encoding="utf-8"))

    def test_collection_and_syntax_errors_are_unknown(self):
        meta = self.prepare("import missing_agora_fixture_module\n")
        self.fail(meta, accept_preexisting=True, reason="no")
        self.assertEqual(set(meta["baseline_comparison"]["classifications"].values()), {"unknown"})
        self.assertFalse(meta["baseline_comparison"]["base"])
        self.modify(meta, name="tests/test_demo.py", text="def broken(\n")
        self.fail(meta, accept_preexisting=True, reason="no")
        self.assertIn("語法錯誤", meta["baseline_comparison"]["error"])

    def test_changed_tree_requires_fresh_review_and_approval(self):
        meta = self.prepare()
        self.fail(meta)
        old = meta["checked_tree"]
        self.modify(meta, text="VALUE = 2\n# a later edit\n")
        error = self.fail(meta, accept_preexisting=True, reason="old review")
        self.assertIn("舊核准失效", error)
        self.assertNotEqual(old, meta["checked_tree"])
        self.assertNotIn("preexisting_approval", meta)
        worktree.finalize(meta, self.check, accept_preexisting=True, reason="reviewed new content")
        self.assertIsNotNone(meta["commit_sha"])

    def test_late_change_invalidates_approval_before_ref_update(self):
        meta = self.prepare()
        real = worktree.git

        def git(root, *args):
            result = real(root, *args)
            if args[0] == "commit-tree":
                self.modify(meta, text="VALUE = 4\n")
            return result

        with patch.object(worktree, "git", side_effect=git):
            error = self.fail(meta, accept_preexisting=True, reason="reviewed")
        self.assertIn("核准失效", error)
        self.assertNotIn("preexisting_approval", meta)
        self.assertEqual(real(Path(meta["worktree"]), "rev-parse", "HEAD"), meta["base_sha"])

    def test_cache_dependencies_command_and_failure_ids(self):
        meta = self.prepare()
        self.fail(meta)
        original = meta["baseline_comparison"]["cache_key"]
        self.fail(meta)
        self.assertTrue(meta["baseline_comparison"]["cache_hit"])
        self.modify(meta, name="requirements-dev.txt", text="# changed dependency contract\n")
        self.fail(meta)
        self.assertFalse(meta["baseline_comparison"]["cache_hit"])
        self.assertNotEqual(original, meta["baseline_comparison"]["cache_key"])
        original = meta["baseline_comparison"]["cache_key"]
        meta["commands"]["check"].append("-v")
        self.fail(meta)
        self.assertNotEqual(original, meta["baseline_comparison"]["cache_key"])
        original = meta["baseline_comparison"]["cache_key"]
        self.modify(meta, text="VALUE = 3\n")
        self.fail(meta)
        self.assertNotEqual(original, meta["baseline_comparison"]["cache_key"])

    def test_cache_requires_unchanged_evidence_log(self):
        meta = self.prepare()
        self.fail(meta)
        record = next(iter(meta["baseline_comparison"]["base"].values()))
        Path(record["log"]).write_text("incomplete diagnostic", encoding="utf-8")
        self.fail(meta)
        self.assertFalse(meta["baseline_comparison"]["cache_hit"])
        self.assertTrue(baseline.eligible(meta["baseline_comparison"]))

    def test_reason_required_before_check_and_nested_host_blocked(self):
        meta = self.prepare()
        with patch.object(cli, "run_task_check") as run:
            for reason in (None, "", "   "):
                self.assertIn("--reason", self.fail(meta, accept_preexisting=True, reason=reason))
            run.assert_not_called()
        with patch.dict(os.environ, {cli.NESTED_ENV: "1"}), self.assertRaises(SystemExit):
            cli.cmd_finalize(cli.Workspace(self.root), argparse.Namespace(thread="t1", accept_preexisting=True, reason="no"))
        with patch.dict(os.environ, {cli.NESTED_ENV: ""}), self.assertRaisesRegex(SystemExit, "--reason"):
            cli.cmd_finalize(cli.Workspace(self.root), argparse.Namespace(thread="t1", accept_preexisting=True, reason=" "))

    def test_changed_command_requires_host_to_review_new_result(self):
        meta = self.prepare()
        self.fail(meta)
        meta["commands"]["check"].append("-v")
        error = self.fail(meta, accept_preexisting=True, reason="review of earlier command")
        self.assertIn("凍結指令或失敗 ID 集合改變", error)
        self.assertNotIn("preexisting_approval", meta)

    def test_stop_cancels_baseline_and_prevents_approval(self):
        meta = self.prepare()
        self.fail(meta, can_commit=lambda: False, accept_preexisting=True, reason="no")
        self.assertEqual(set(meta["baseline_comparison"]["classifications"].values()), {"unknown"})
        self.assertIn("停止", meta["baseline_comparison"]["error"])

    def test_approval_binding_rejects_tree_commands_ids_and_evidence_changes(self):
        comparison = {"checked_tree": "tree", "commands": {"check": ["python", "-m", "unittest"]},
                      "classifications": {"m.T.test_a": "preexisting"}}
        approval = {"checked_tree": "tree", "commands": comparison["commands"], "reason": "reviewed",
                    "ids": ["m.T.test_a"], "comparison_hash": baseline.digest(comparison)}
        meta = {"commands": comparison["commands"], "baseline_comparison": comparison, "preexisting_approval": approval}
        self.assertTrue(baseline.approval_valid(meta, "tree"))
        self.assertFalse(baseline.approval_valid(meta, "changed-tree"))
        for field, value in (("commands", {}), ("ids", ["other.T.test_b"]), ("comparison_hash", "bad")):
            altered = {**meta, "preexisting_approval": {**approval, field: value}}
            self.assertFalse(baseline.approval_valid(altered, "tree"))

    def test_cli_host_approval_status_and_completion(self):
        meta = self.prepare()
        cli.update_relay(self.thread, **{k: v for k, v in meta.items() if k != "thread"},
                         status="stopped", worker="codex", pid=99999999, pid_started=123.0)
        with patch.dict(os.environ, {cli.NESTED_ENV: ""}), patch.object(cli, "AGORA_ROOT_DIR", Path(cli.__file__).resolve().parents[1]):
            with self.assertRaises(SystemExit):
                cli.cmd_finalize(cli.Workspace(self.root), argparse.Namespace(thread="t1", dispatch_id=None))
            reports = [item for item in completions.snapshot(self.root)[0] if item.get("baseline_comparison")]
            self.assertEqual(len(reports), 1)
            item = reports[0]
            self.assertIn("既有失敗", completions.describe(item))
            self.assertIn("--accept-preexisting", completions.describe(item))
            with patch("sys.stdout", new_callable=io.StringIO) as output:
                cli.cmd_status(cli.Workspace(self.root), argparse.Namespace(thread="t1", wait=None, json=True))
                self.assertIn("baseline_comparison", json.loads(output.getvalue()))
            cli.main(["--workspace", str(self.root), "finalize", "t1", "--accept-preexisting", "--reason", "reviewed", "--from", "claude"])
        saved = json.loads(Path(meta["manifest"]).read_text(encoding="utf-8"))
        self.assertEqual(saved["preexisting_approval"]["by"], "claude")
        self.assertEqual(saved["commit_status"], "committed")

    @unittest.skipUnless(HAS_PYTEST, "pytest not installed; real pytest integration requires existing pytest")
    def test_pytest_real_repository_preexisting(self):
        source = "import app\ndef test_old(): assert app.VALUE == -1\ndef test_new(): assert app.VALUE < 3\n"
        meta = self.prepare(source, framework="pytest")
        self.fail(meta)
        self.assertEqual(set(meta["baseline_comparison"]["classifications"].values()), {"preexisting"})
        worktree.finalize(meta, self.check, accept_preexisting=True, reason="pytest base verified")
        self.assertIsNotNone(meta["commit_sha"])

    @unittest.skipUnless(HAS_PYTEST, "pytest not installed; real pytest integration requires existing pytest")
    def test_pytest_real_repository_mixed(self):
        source = "import app\ndef test_old(): assert app.VALUE == -1\ndef test_new(): assert app.VALUE < 3\n"
        meta = self.prepare(source, framework="pytest")
        self.modify(meta, text="VALUE = 3\n")
        self.fail(meta, accept_preexisting=True, reason="cannot approve mixed failures")
        self.assertEqual(meta["baseline_comparison"]["classifications"],
                         {"tests/test_demo.py::test_new": "new", "tests/test_demo.py::test_old": "preexisting"})


if __name__ == "__main__":
    unittest.main()
