"""Tests for AI-prefixed branch naming, compatibility, `agora merge` (--no-ff), and conservative cleanup."""

import argparse
import errno
import io
import json
import os
import shutil
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from agoralib import cli, completions, config, merge, prune, worktree

try:
    from . import test_worktree as fixtures
except ImportError:
    import test_worktree as fixtures


class MergeTestBase(unittest.TestCase):
    setUp = fixtures.WorktreeTests.setUp
    task = fixtures.WorktreeTests.task
    message = fixtures.WorktreeTests.message
    modify = fixtures.WorktreeTests.modify
    venv = fixtures.WorktreeTests.venv

    def configure_parties(self):
        config.save(self.root, {
            "parties": {
                "claude": {"enabled": True, "share": 0},
                "codex": {"enabled": True, "share": 10},
                "antigravity": {"enabled": True, "share": 5},
            },
            "check": [sys._base_executable, "-c", "pass"],
        })

    def make_committed_dispatch(
        self,
        dispatch_id="abcdef0123456789abcdef0123456789",
        worker="antigravity",
        title="分派分支命名與 --no-ff 合併清理",
        name=None,
        allowed=None,
        edit_file="app.py",
        edit_text="VALUE = 2\n",
        build_outputs=None,
        legacy=False,
    ):
        cli.save_state(self.thread, {"topic": title, "parties": {}, "messages": []})
        if legacy:
            meta = worktree.prepare(self.root, dispatch_id, allowed or ["app.py", "demo/*"], self.thread)
            meta["worker"] = worker
            worktree.save(meta)
        else:
            meta = worktree.prepare(
                self.root, dispatch_id, allowed or ["app.py", "demo/*"], self.thread,
                worker=worker, title=title, name=name, build_outputs=build_outputs,
            )
            meta["worker"] = worker
            worktree.save(meta)
        self.modify(meta, edit_file, edit_text)
        self.message(meta)
        worktree.finalize(meta, lambda m: {"exit_code": 0, "output_tail": "Ran 1 test in 0.01s\nOK"})
        cli.update_relay(
            self.thread,
            **{k: meta.get(k) for k in worktree.FIELDS},
            allowed_paths=meta["allowed_paths"],
            worker=worker,
            status="done",
            pid=99999999,
            pid_started=123.0,
        )
        return json.loads(Path(meta["manifest"]).read_text(encoding="utf-8"))

    def read_meta(self, meta):
        return json.loads(Path(meta["manifest"]).read_text(encoding="utf-8"))


class TestBranchNamingAndOwnership(MergeTestBase):
    def test_slug_sanitization_chinese_symbols_length_and_fallback(self):
        self.assertEqual(worktree.branch_slug("分派分支命名與 --no-ff 合併清理"), "分派分支命名與-no-ff-合併清理")
        self.assertEqual(worktree.branch_slug("!!!@@@###___---   "), "task")
        self.assertEqual(worktree.branch_slug(""), "task")
        self.assertEqual(worktree.branch_slug(None), "task")
        self.assertEqual(worktree.branch_slug("foo_bar baz..qux.lock"), "foo-bar-baz-qux-lock")
        long_title = "a" * 29 + "-extra-suffix-here"
        self.assertEqual(worktree.branch_slug(long_title), "a" * 29)
        self.assertEqual(len(worktree.branch_slug("中" * 40)), 30)

    def test_prefixes_for_all_three_ais_and_worktree_dir_unchanged(self):
        for worker, expected_prefix in (("claude", "claude"), ("codex", "codex"), ("antigravity", "agy")):
            did = f"{expected_prefix}0123456789abcdef0123456789"
            meta = worktree.prepare(
                self.root, did, ["app.py"], self.thread,
                worker=worker, title="中文功能測試",
            )
            self.assertEqual(meta["branch"], f"{expected_prefix}/中文功能測試-{did[:7]}")
            self.assertEqual(meta["branch_naming_version"], 2)
            self.assertEqual(meta["initial_worker"], worker)
            self.assertEqual(Path(meta["worktree"]), self.root / ".agora" / "worktrees" / did)
            worktree.validate(meta)

    def test_assign_name_override_and_all_symbols_fallback(self):
        did = "1234567890abcdef1234567890abcdef"
        meta1 = worktree.prepare(
            self.root, did, ["app.py"], self.thread,
            worker="codex", title="不會用到這個標題", name="自訂名稱!! #1",
        )
        self.assertEqual(meta1["branch"], "codex/自訂名稱-1-1234567")
        did2 = "fedcba0987654321fedcba0987654321"
        meta2 = worktree.prepare(
            self.root, did2, ["app.py"], self.thread,
            worker="claude", title="有標題", name="***///___",
        )
        self.assertEqual(meta2["branch"], "claude/task-fedcba0")

    def test_7_char_collision_extends_id_and_ref_format_validated(self):
        did = "abcdef0123456789abcdef0123456789"
        worktree.git(self.root, "branch", "agy/中文主題-abcdef0")
        worktree.git(self.root, "branch", "agy/中文主題-abcdef01")
        meta = worktree.prepare(
            self.root, did, ["app.py"], self.thread,
            worker="antigravity", title="中文主題",
        )
        self.assertEqual(meta["branch"], "agy/中文主題-abcdef012")
        worktree.validate(meta)

        # If check-ref-format fails, prepare must refuse without creating a branch.
        real_git = worktree.git
        def fail_ref_check(root, *args):
            if args[:2] == ("check-ref-format", "--branch"):
                raise worktree.WorktreeError("invalid ref")
            return real_git(root, *args)

        with patch.object(worktree, "git", side_effect=fail_ref_check):
            with self.assertRaisesRegex(worktree.WorktreeError, "無效的 branch 名稱"):
                worktree.build_branch_name(self.root, "99999998888888", "codex", title="ok")

    def test_ownership_validation_new_legacy_and_forged_branches(self):
        did = "abcdef0123456789abcdef0123456789"
        # Valid v2 & v1
        self.assertTrue(worktree.is_valid_dispatch_branch("agy/中文-abcdef0", did, 2, "antigravity"))
        self.assertTrue(worktree.is_valid_dispatch_branch("agy/中文-abcdef0123", did, 2, "antigravity"))
        self.assertTrue(worktree.is_valid_dispatch_branch(f"agora/{did}", did, 1, None))
        self.assertTrue(worktree.is_valid_dispatch_branch(f"agora/{did}", did, None, None))
        # Hyphenated custom dispatch IDs work properly
        self.assertTrue(worktree.is_valid_dispatch_branch("codex/fix-bug-work-1", "work-1", 2, "codex"))

        # Forged / invalid branches must all be rejected
        forged = [
            ("main", did, 2, "antigravity"),
            ("agy/中文-1111111", did, 2, "antigravity"),        # wrong ID prefix
            ("agy/中文-abcd", did, 2, "antigravity"),           # too short (< 7)
            ("evil/中文-abcdef0", did, 2, "antigravity"),       # invalid prefix
            ("claude/中文-abcdef0", did, 2, "antigravity"),     # wrong initial_worker prefix
            ("agy/-abcdef0", did, 2, "antigravity"),            # empty slug
            ("agy/a/b-abcdef0", did, 2, "antigravity"),         # extra slash
            (f"agora/{did}", did, 2, "antigravity"),            # v1 name on v2 manifest
            ("agora/otherid", did, 1, None),                    # wrong legacy ID
        ]
        for branch, d, ver, init_w in forged:
            with self.subTest(branch=branch):
                self.assertFalse(worktree.is_valid_dispatch_branch(branch, d, ver, init_w))

        # Forged branch written directly into manifest on disk is rejected by validate()
        meta = worktree.prepare(self.root, did, ["app.py"], self.thread, worker="codex", title="test")
        worktree.git(Path(meta["worktree"]), "checkout", "-b", "codex/forged-0000000")
        meta["branch"] = "codex/forged-0000000"
        worktree.save(meta)
        with self.assertRaisesRegex(worktree.WorktreeError, "dispatch 與 branch 不符"):
            worktree.validate(meta)

    def test_resume_switch_worker_keeps_branch_name_and_rejects_name_flag(self):
        self.configure_parties()
        ws = cli.Workspace(self.root)
        cli.save_state(self.thread, {"topic": "切換接手測試", "parties": {}, "messages": []})
        did = "11223344556677889900aabbccddeeff"
        args = argparse.Namespace(
            thread=self.thread.name, sender="claude", to=None, message="實作", file=None,
            session=None, from_summary=False, yes=False, tier=None, model=None, effort=None,
            subagents=None, trigger=None, allow=["app.py"], reason=None, user_override=False,
            dispatch_id=did, resume=None, name="自訂分支",
        )
        with patch.dict(os.environ, {cli.NESTED_ENV: ""}), patch("sys.stdout", io.StringIO()), \
                patch.object(cli, "party_usage", return_value={"remaining_pct": 80, "updated_at": time.time()}), \
                patch.object(cli.quota, "blocked", return_value=None), \
                patch.object(cli.models, "CACHE", self.root / "models.json"), \
                patch.object(cli.jobs, "spawn", return_value=99999999), \
                patch.object(cli.jobs, "pid_started", return_value=123.0):
            cli.cmd_assign(ws, args)
        relay = cli.load_state(self.thread)["relay"]
        self.assertEqual(relay["worker"], "codex")
        self.assertEqual(relay["initial_worker"], "codex")
        self.assertEqual(relay["branch"], "codex/自訂分支-1122334")
        cli.update_relay(self.thread, status="stopped")

        # --resume with --name must refuse
        bad_resume = argparse.Namespace(**{**vars(args), "dispatch_id": None, "resume": did, "name": "new-name"})
        with patch.dict(os.environ, {cli.NESTED_ENV: ""}), self.assertRaisesRegex(SystemExit, "--name"):
            cli.cmd_assign(ws, bad_resume)

        # --resume switching to antigravity keeps original codex/... branch and initial_worker
        good_resume = argparse.Namespace(
            **{**vars(args), "dispatch_id": None, "resume": did, "name": None,
               "to": "antigravity", "reason": "改派", "allow": None, "message": None}
        )
        with patch.dict(os.environ, {cli.NESTED_ENV: ""}), patch("sys.stdout", io.StringIO()), \
                patch.object(cli, "party_usage", return_value={"remaining_pct": 80, "updated_at": time.time()}), \
                patch.object(cli.quota, "blocked", return_value=None), \
                patch.object(cli.models, "CACHE", self.root / "models.json"), \
                patch.object(cli.jobs, "spawn", return_value=88888888), \
                patch.object(cli.jobs, "pid_started", return_value=456.0):
            cli.cmd_assign(ws, good_resume)
        resumed = cli.load_state(self.thread)["relay"]
        self.assertEqual(resumed["worker"], "antigravity")
        self.assertEqual(resumed["initial_worker"], "codex")
        self.assertEqual(resumed["branch"], "codex/自訂分支-1122334")
        worktree.validate(worktree.load(resumed))


class TestAgoraMerge(MergeTestBase):
    def test_merge_precheck_refusals(self):
        ws = cli.Workspace(self.root)
        meta = self.make_committed_dispatch()

        # 1. Live worker process refused
        cli.update_relay(self.thread, pid=os.getpid(), pid_started=cli.jobs.pid_started(os.getpid()))
        with self.assertRaisesRegex(merge.MergeError, "仍在執行"):
            merge.merge_dispatch(ws, self.thread, run_check=lambda r: {"exit_code": 0})
        cli.update_relay(self.thread, pid=99999999, pid_started=123.0)

        # 2. Relay still active status refused
        cli.update_relay(self.thread, status="running")
        with self.assertRaisesRegex(merge.MergeError, "尚未結束"):
            merge.merge_dispatch(ws, self.thread, run_check=lambda r: {"exit_code": 0})
        cli.update_relay(self.thread, status="done")

        # 3. Uncommitted dispatch refused
        saved = self.read_meta(meta)
        saved["commit_status"] = "pending"
        worktree.save(saved)
        with self.assertRaisesRegex(merge.MergeError, "尚未完成驗證與提交"):
            merge.merge_dispatch(ws, self.thread, run_check=lambda r: {"exit_code": 0})
        saved["commit_status"] = "committed"
        worktree.save(saved)

        # 4. Branch SHA diverged from commit_sha refused
        worktree.git(Path(meta["worktree"]), "commit", "--allow-empty", "-m", "unverified extra")
        with self.assertRaisesRegex(merge.MergeError, "commit_sha"):
            merge.merge_dispatch(ws, self.thread, run_check=lambda r: {"exit_code": 0})
        worktree.git(Path(meta["worktree"]), "reset", "--hard", meta["commit_sha"])

        # 5. Wrong current branch in main workspace refused
        worktree.git(self.root, "checkout", "-b", "other-branch")
        with self.assertRaisesRegex(merge.MergeError, "來源分支"):
            merge.merge_dispatch(ws, self.thread, run_check=lambda r: {"exit_code": 0})
        worktree.git(self.root, "checkout", "main")
        worktree.git(self.root, "branch", "-D", "other-branch")

        # 6. Uncommitted tracked change in main workspace refused
        (self.root / "other.py").write_text("DIRTY = True\n", encoding="utf-8")
        with self.assertRaisesRegex(merge.MergeError, "未提交的已追蹤變更"):
            merge.merge_dispatch(ws, self.thread, run_check=lambda r: {"exit_code": 0})
        (self.root / "other.py").write_text("VALUE = 9\n", encoding="utf-8")

        # 7. In-progress merge / rebase / cherry-pick refused
        git_dir = merge._git_dir(self.root)
        for marker, label in (("MERGE_HEAD", "merge"), ("REBASE_HEAD", "rebase"), ("CHERRY_PICK_HEAD", "cherry-pick")):
            (git_dir / marker).write_text("0" * 40 + "\n", encoding="utf-8")
            try:
                with self.assertRaisesRegex(merge.MergeError, label):
                    merge.merge_dispatch(ws, self.thread, run_check=lambda r: {"exit_code": 0})
            finally:
                (git_dir / marker).unlink()

        # 8. Nested worker cannot run cmd_merge; merge not in AGENT_COMMANDS
        self.assertNotIn("merge", cli.AGENT_COMMANDS)
        with patch.dict(os.environ, {cli.NESTED_ENV: "1"}), self.assertRaises(SystemExit):
            cli.cmd_merge(ws, argparse.Namespace(thread=self.thread.name, dispatch_id=None, keep=False, json=False))

    def test_merge_no_ff_message_trailers_check_and_default_cleanup(self):
        ws = cli.Workspace(self.root)
        # Untracked file in main workspace must NOT block merge pre-check
        (self.root / "local-untracked.txt").write_text("ok\n", encoding="utf-8")
        meta = self.make_committed_dispatch()
        target = Path(meta["worktree"])
        # Add rebuildable artifacts that should be auto-cleaned
        (target / ".venv").mkdir()
        (target / ".venv" / "pyvenv.cfg").write_text("home = x\n", encoding="utf-8")
        (target / "demo" / "__pycache__").mkdir()
        (target / "demo" / "__pycache__" / "mod.cpython-311.pyc").write_bytes(b"pyc")
        (target / "temp.pyc").write_bytes(b"pyc")

        checked_roots = []
        def fake_check(root):
            checked_roots.append(root)
            return {"exit_code": 0, "time": "2026-11-19 16:00:00", "output_tail": "Ran 10 tests\nOK"}

        result = merge.merge_dispatch(ws, self.thread, run_check=fake_check)
        self.assertTrue(result["ok"])
        self.assertFalse(result["already_merged"])
        self.assertEqual(checked_roots, [self.root])
        self.assertEqual(result["cleanup"]["status"], "cleaned")

        # Must be a true 2-parent --no-ff merge commit
        parents = worktree.git(self.root, "rev-list", "--parents", "-n", "1", "HEAD").split()
        self.assertEqual(len(parents), 3)
        self.assertEqual(parents[2], meta["commit_sha"])

        # Verify commit message & trailers
        msg = worktree.git(self.root, "show", "-s", "--format=%B", "HEAD")
        self.assertTrue(msg.startswith("合併：分派分支命名與 --no-ff 合併清理（Antigravity）"))
        self.assertIn(meta["branch"], msg)
        self.assertIn("驗證結果摘要", msg)
        trailers = worktree.git(self.root, "show", "-s", "--format=%(trailers)", "HEAD")
        self.assertIn(f"Agora-Dispatch: {meta['dispatch_id']}", trailers)
        self.assertIn(f"Agora-Branch: {meta['branch']}", trailers)

        # Worktree removed, branch kept by default, control records preserved
        self.assertFalse(target.exists())
        self.assertTrue(merge.ref_exists(self.root, f"refs/heads/{meta['branch']}"))
        self.assertTrue(Path(meta["manifest"]).is_file())
        saved = self.read_meta(meta)
        self.assertEqual(saved["merge_record"]["merge_sha"], result["merge_sha"])
        self.assertTrue(saved["merge_record"]["branch_kept"])
        self.assertEqual(saved["cleanup_record"]["status"], "cleaned")
        self.assertFalse(saved["cleanup_record"]["branch_deleted"])
        self.assertTrue(saved["cleanup_record"]["branch_kept"])
        self.assertIn("agora merge 整合驗證", (self.thread / "transcript.md").read_text(encoding="utf-8"))

        # `agora status` and `agora changes` still work after worktree cleanup
        out = io.StringIO()
        with patch("sys.stdout", out):
            cli.cmd_status(ws, argparse.Namespace(thread=self.thread.name, wait=None, json=False))
        self.assertIn("已清理 worktree，分支保留", out.getvalue())
        self.assertIn(meta["branch"], out.getvalue())

        out_changes = io.StringIO()
        with patch("sys.stdout", out_changes):
            cli.cmd_changes(ws, argparse.Namespace(thread=self.thread.name, dispatch_id=None, json=True))
        changes_data = json.loads(out_changes.getvalue())
        self.assertEqual([f["path"] for f in changes_data["files"]], ["app.py"])

        # Re-running merge while branch is kept succeeds without creating a new merge commit
        head_after_merge = worktree.git(self.root, "rev-parse", "HEAD")
        rerun = merge.merge_dispatch(ws, self.thread, run_check=lambda r: self.fail("already checked"))
        self.assertTrue(rerun["ok"])
        self.assertTrue(rerun["already_merged"])
        self.assertEqual(worktree.git(self.root, "rev-parse", "HEAD"), head_after_merge)
        self.assertEqual(rerun["cleanup"]["status"], "cleaned")
        self.assertTrue(rerun["cleanup"]["branch_kept"])
        self.assertTrue(merge.ref_exists(self.root, f"refs/heads/{meta['branch']}"))

        # Re-running merge with --delete-branch deletes the kept branch
        deleted = merge.merge_dispatch(ws, self.thread, delete_branch=True, run_check=lambda r: self.fail("already checked"))
        self.assertTrue(deleted["ok"])
        self.assertTrue(deleted["already_merged"])
        self.assertEqual(deleted["cleanup"]["status"], "cleaned")
        self.assertTrue(deleted["cleanup"]["branch_deleted"])
        self.assertFalse(deleted["cleanup"]["branch_kept"])
        self.assertFalse(merge.ref_exists(self.root, f"refs/heads/{meta['branch']}"))

    def test_merge_delete_branch_and_keep_mutual_exclusion(self):
        ws = cli.Workspace(self.root)
        meta = self.make_committed_dispatch()
        with self.assertRaisesRegex(merge.MergeError, "--keep"):
            merge.merge_dispatch(ws, self.thread, keep=True, delete_branch=True, run_check=lambda r: {"exit_code": 0})
        result = merge.merge_dispatch(ws, self.thread, delete_branch=True, run_check=lambda r: {"exit_code": 0})
        self.assertTrue(result["ok"])
        self.assertEqual(result["cleanup"]["status"], "cleaned")
        self.assertTrue(result["cleanup"]["branch_deleted"])
        self.assertFalse(result["cleanup"]["branch_kept"])
        self.assertFalse(Path(meta["worktree"]).exists())
        self.assertFalse(merge.ref_exists(self.root, f"refs/heads/{meta['branch']}"))
        saved = self.read_meta(meta)
        self.assertFalse(saved["merge_record"]["branch_kept"])

    def test_already_merged_skips_empty_commit_and_cleans_up(self):
        ws = cli.Workspace(self.root)
        meta = self.make_committed_dispatch()
        worktree.git(self.root, "merge", "--no-ff", "-m", "manual merge", meta["branch"])
        head_before = worktree.git(self.root, "rev-parse", "HEAD")

        with patch.object(merge, "run_main_check", return_value={"exit_code": 0}) as check:
            result = merge.merge_dispatch(ws, self.thread)
        check.assert_called_once_with(self.root, record_dir=Path(meta["manifest"]).parent)
        self.assertTrue(result["ok"])
        self.assertTrue(result["already_merged"])
        self.assertEqual(worktree.git(self.root, "rev-parse", "HEAD"), head_before)
        self.assertEqual(result["cleanup"]["status"], "cleaned")
        self.assertTrue(result["cleanup"]["branch_kept"])
        self.assertFalse(Path(meta["worktree"]).exists())
        self.assertTrue(merge.ref_exists(self.root, f"refs/heads/{meta['branch']}"))

    def test_interrupted_merge_without_passed_check_runs_it_again(self):
        ws = cli.Workspace(self.root)
        meta = self.make_committed_dispatch()
        with patch.object(merge, "run_main_check", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                merge.merge_dispatch(ws, self.thread, keep=True)
        head = worktree.git(self.root, "rev-parse", "HEAD")
        with patch.object(merge, "run_main_check", return_value={"exit_code": 0}) as check:
            result = merge.merge_dispatch(ws, self.thread, keep=True)
        check.assert_called_once_with(self.root, record_dir=Path(meta["manifest"]).parent)
        self.assertTrue(result["already_merged"])
        self.assertTrue(result["check_passed"])
        self.assertEqual(result["merge_sha"], head)

    def test_missing_pytest_refuses_merge_for_configured_and_incoming_checks(self):
        ws = cli.Workspace(self.root)
        self.make_committed_dispatch(allowed=["pytest.ini"], edit_file="pytest.ini", edit_text="[pytest]\n")
        head = worktree.git(self.root, "rev-parse", "HEAD")
        for check in (None, [sys._base_executable, "-m", "pytest", "-q"]):
            with self.subTest(check=check):
                config.save(self.root, {"check": check})
                with patch.object(cli, "has_module", return_value=False) as probe, \
                        patch.object(merge, "run_main_check") as integration:
                    with self.assertRaisesRegex(merge.MergeError, r"整合環境缺失.*pytest.*python -m venv"):
                        merge.merge_dispatch(ws, self.thread)
                self.assertEqual(probe.call_args.kwargs["root"], self.root)
                integration.assert_not_called()
                self.assertEqual(worktree.git(self.root, "rev-parse", "HEAD"), head)

    def test_merge_runs_unittest_without_workspace_venv(self):
        ws = cli.Workspace(self.root)
        (self.root / "tests").mkdir()
        test = "import unittest\nclass AppTest(unittest.TestCase):\n def test_app(self):\n  self.assertTrue(True)\n"
        (self.root / "tests/test_app.py").write_text(test, encoding="utf-8")
        self.make_committed_dispatch()
        self.assertIsNone(cli.project_python(self.root))
        result = merge.merge_dispatch(ws, self.thread, keep=True)
        self.assertTrue(result["check_passed"], result["check_result"])
        self.assertIn("unittest", result["check_result"]["output_tail"])
        self.assertIn("Ran 1 test", result["check_result"]["output_tail"])

    def test_merge_refuses_a_configured_dispatch_python(self):
        ws = cli.Workspace(self.root)
        meta = self.make_committed_dispatch()
        self.venv(meta)
        python = worktree.python_path(Path(meta["worktree"]))
        config.save(self.root, {"check": [str(python), "-m", "pytest"]})
        head = worktree.git(self.root, "rev-parse", "HEAD")
        with self.assertRaisesRegex(merge.MergeError, "整合環境缺失.*分派 worktree"):
            merge.merge_dispatch(ws, self.thread)
        self.assertEqual(worktree.git(self.root, "rev-parse", "HEAD"), head)

    def test_conflict_aborts_reports_files_and_does_not_clean(self):
        ws = cli.Workspace(self.root)
        meta = self.make_committed_dispatch(edit_file="app.py", edit_text="VALUE = 200\n")
        # Create conflicting change on main
        (self.root / "app.py").write_text("VALUE = 999\n", encoding="utf-8")
        worktree.git(self.root, "add", "app.py")
        worktree.git(self.root, "commit", "-m", "conflicting main edit")
        head_before = worktree.git(self.root, "rev-parse", "HEAD")

        with self.assertRaises(merge.MergeError) as caught:
            merge.merge_dispatch(ws, self.thread, run_check=lambda r: self.fail("must not run check on conflict"))
        self.assertIn("衝突", str(caught.exception))
        self.assertIn("git merge --abort", str(caught.exception))
        self.assertIn("app.py", str(caught.exception))
        # Main repo must be clean, no MERGE_HEAD, HEAD unchanged, worktree & branch preserved
        self.assertFalse((merge._git_dir(self.root) / "MERGE_HEAD").exists())
        self.assertEqual(worktree.git(self.root, "rev-parse", "HEAD"), head_before)
        self.assertEqual(worktree.git(self.root, "status", "--porcelain", "--untracked-files=no"), "")
        self.assertTrue(Path(meta["worktree"]).is_dir())
        self.assertTrue(merge.ref_exists(self.root, f"refs/heads/{meta['branch']}"))

    def test_integration_check_failure_does_not_rollback_or_clean(self):
        ws = cli.Workspace(self.root)
        meta = self.make_committed_dispatch()
        result = merge.merge_dispatch(
            ws, self.thread,
            run_check=lambda r: {"exit_code": 1, "time": "2026-11-19 16:00:00", "output_tail": "FAIL: test_x"},
        )
        self.assertFalse(result["ok"])
        self.assertFalse(result["check_passed"])
        # Merge commit stays on main (not rolled back!)
        self.assertEqual(worktree.git(self.root, "rev-parse", "HEAD"), result["merge_sha"])
        self.assertEqual((self.root / "app.py").read_text(encoding="utf-8"), "VALUE = 2\n")
        # Worktree & branch are NOT cleaned up
        self.assertTrue(Path(meta["worktree"]).is_dir())
        self.assertTrue(merge.ref_exists(self.root, f"refs/heads/{meta['branch']}"))
        self.assertIn("不回滾", (self.thread / "transcript.md").read_text(encoding="utf-8"))
        out = io.StringIO()
        with patch.object(merge, "merge_dispatch", return_value=result), patch("sys.stdout", out):
            with self.assertRaises(SystemExit):
                merge.cmd_merge(ws, argparse.Namespace(thread=self.thread.name, json=False))
        first = out.getvalue().splitlines()[0]
        self.assertIn("合併已完成", first)
        self.assertIn("只有整合檢查失敗", first)
        self.assertIn(result["merge_sha"], first)

    def test_merge_keep_flag_preserves_worktree_and_branch(self):
        ws = cli.Workspace(self.root)
        meta = self.make_committed_dispatch()
        result = merge.merge_dispatch(ws, self.thread, keep=True, run_check=lambda r: {"exit_code": 0})
        self.assertTrue(result["ok"])
        self.assertEqual(result["cleanup"]["status"], "kept")
        self.assertTrue(Path(meta["worktree"]).is_dir())
        self.assertTrue(merge.ref_exists(self.root, f"refs/heads/{meta['branch']}"))


class TestCleanupAndPruneMerged(MergeTestBase):
    def test_rebuildable_build_outputs_cleaned_but_unknown_files_block(self):
        ws = cli.Workspace(self.root)
        meta = self.make_committed_dispatch(build_outputs=["dist"])
        target = Path(meta["worktree"])
        (target / "dist").mkdir()
        (target / "dist" / "bundle.js").write_text("ok", encoding="utf-8")
        # Unknown untracked file + unknown ignored file
        (target / "notes.txt").write_text("keep me", encoding="utf-8")
        (target / "debug.log").write_text("ignored or untracked", encoding="utf-8")
        (target / "demo" / "__pycache__").mkdir()
        (target / "demo" / "__pycache__" / "ok.pyc").write_bytes(b"pyc")

        result = merge.merge_dispatch(ws, self.thread, run_check=lambda r: {"exit_code": 0})
        self.assertTrue(result["ok"])
        self.assertEqual(result["cleanup"]["status"], "blocked")
        self.assertIn("notes.txt", result["cleanup"]["blocking_files"])
        self.assertIn("debug.log", result["cleanup"]["blocking_files"])
        # Nothing deleted when blocked!
        self.assertTrue(target.is_dir())
        self.assertTrue((target / "dist" / "bundle.js").is_file())
        self.assertTrue(merge.ref_exists(self.root, f"refs/heads/{meta['branch']}"))

        # Remove unknown files, leave recorded build_outputs + .pyc -> retry succeeds
        (target / "notes.txt").unlink()
        (target / "debug.log").unlink()

        retry = merge.merge_dispatch(ws, self.thread, run_check=lambda r: self.fail("already checked"))
        self.assertTrue(retry["already_merged"])
        self.assertEqual(retry["cleanup"]["status"], "cleaned")
        self.assertFalse(target.exists())
        self.assertTrue(merge.ref_exists(self.root, f"refs/heads/{meta['branch']}"))

    def test_uncommitted_tracked_change_in_worktree_blocks_cleanup(self):
        ws = cli.Workspace(self.root)
        meta = self.make_committed_dispatch()
        worktree.git(self.root, "merge", "--no-ff", "-m", "merge", meta["branch"])
        (Path(meta["worktree"]) / "app.py").write_text("UNCOMMITTED EDIT\n", encoding="utf-8")
        outcome = merge.cleanup_dispatch(self.root, self.thread, meta)
        self.assertEqual(outcome["status"], "blocked")
        self.assertEqual(outcome["tracked_changes"], ["app.py"])
        self.assertTrue(Path(meta["worktree"]).is_dir())
        self.assertTrue(merge.ref_exists(self.root, f"refs/heads/{meta['branch']}"))

    def test_prune_blocks_tracked_cache_changes_and_cleans_nested_untracked_caches(self):
        ws = cli.Workspace(self.root)
        fixture = self.root / "demo/.pytest_cache/fixture.txt"
        fixture.parent.mkdir()
        fixture.write_text("tracked\n", encoding="utf-8")
        worktree.git(self.root, "add", "-f", "demo/.pytest_cache/fixture.txt")
        worktree.git(self.root, "commit", "-m", "track cache fixture")
        meta = self.make_committed_dispatch()
        target = Path(meta["worktree"])
        tracked = target / "demo/.pytest_cache/fixture.txt"
        original = tracked.read_bytes()
        tracked.write_bytes(b"changed")
        merge.merge_dispatch(ws, self.thread, keep=True, run_check=lambda r: {"exit_code": 0})
        dry = merge.prune_merged_dispatches(ws, [self.thread])
        self.assertIn("demo/.pytest_cache/fixture.txt", dry["kept"][0]["tracked_changes"])
        tracked.write_bytes(original)
        for path in ("demo/.pytest_cache/untracked", "fresh/pkg.egg-info/PKG-INFO", "fresh/.mypy_cache/data",
                     "fresh/.ruff_cache/data", "fresh/__pycache__/cache"):
            (target / path).parent.mkdir(parents=True, exist_ok=True)
            self.modify(meta, path, "generated")
        dry = merge.prune_merged_dispatches(ws, [self.thread])
        self.assertIn("demo/.pytest_cache/untracked", dry["candidates"][0]["rebuildable_artifacts"])
        result = merge.prune_merged_dispatches(ws, [self.thread], yes=True)
        self.assertEqual(len(result["cleaned"]), 1, result)
        self.assertFalse(target.exists())

    def test_eperm_marks_pending_retry_and_retry_completes_without_rollback(self):
        ws = cli.Workspace(self.root)
        meta = self.make_committed_dispatch()
        target = Path(meta["worktree"])
        (target / ".venv").mkdir()
        (target / ".venv" / "locked.dll").write_text("busy", encoding="utf-8")

        # Simulate Windows EPERM on first cleanup attempt (only for worktree artifact, not tempfile)
        real_rmtree = shutil.rmtree
        def locked_rmtree(path, *a, **kw):
            if str(target) in str(path):
                raise PermissionError(errno.EPERM, "Operation not permitted")
            return real_rmtree(path, *a, **kw)

        with patch.object(merge.shutil, "rmtree", side_effect=locked_rmtree):
            res = merge.merge_dispatch(ws, self.thread, run_check=lambda r: {"exit_code": 0})
        self.assertTrue(res["ok"])
        self.assertEqual(res["cleanup"]["status"], "pending_retry")
        self.assertTrue(res["cleanup"]["retryable"])
        self.assertIn("沙箱建立的檔案，需要以較高權限刪除", res["cleanup"]["error"])
        # Merge is NOT rolled back; worktree & branch still present
        merge_sha = res["merge_sha"]
        self.assertEqual(worktree.git(self.root, "rev-parse", "HEAD"), merge_sha)
        self.assertTrue(target.is_dir())
        self.assertTrue(merge.ref_exists(self.root, f"refs/heads/{meta['branch']}"))

        # Simulate partial failure: worktree removed, then git branch -d locked -> pending_retry
        real_git = worktree.git
        def fail_branch_delete(root, *args):
            if args[:2] == ("branch", "-d"):
                raise worktree.WorktreeError("cannot lock ref: Permission denied")
            return real_git(root, *args)

        with patch.object(worktree, "git", side_effect=fail_branch_delete):
            partial = merge.merge_dispatch(ws, self.thread, delete_branch=True, run_check=lambda r: self.fail("already checked"))
        self.assertTrue(partial["already_merged"])
        self.assertEqual(partial["cleanup"]["status"], "pending_retry")
        self.assertTrue(partial["cleanup"]["worktree_removed"])
        self.assertFalse(partial["cleanup"]["branch_deleted"])
        self.assertFalse(target.exists())
        self.assertTrue(merge.ref_exists(self.root, f"refs/heads/{meta['branch']}"))

        # Retry the exact same command once lock is released -> finishes deleting branch!
        retry = merge.merge_dispatch(ws, self.thread, delete_branch=True, run_check=lambda r: self.fail("already checked"))
        self.assertTrue(retry["already_merged"])
        self.assertEqual(retry["cleanup"]["status"], "cleaned")
        self.assertEqual(worktree.git(self.root, "rev-parse", "HEAD"), merge_sha)
        self.assertFalse(target.exists())
        self.assertFalse(merge.ref_exists(self.root, f"refs/heads/{meta['branch']}"))

    def test_prune_merged_cleans_only_merged_dispatches_and_never_deletes_thread(self):
        ws = cli.Workspace(self.root)
        # Historical merged dispatch + newer unmerged dispatch in the same thread
        m1 = self.make_committed_dispatch(dispatch_id="1111111aaaaaaa1111111", worker="codex", title="第一任務")
        merge.merge_dispatch(ws, self.thread, dispatch_id="1111111aaaaaaa1111111", keep=True, run_check=lambda r: {"exit_code": 0})

        m2 = self.make_committed_dispatch(
            dispatch_id="2222222bbbbbbb2222222", worker="antigravity", title="第二任務",
            allowed=["other.py"], edit_file="other.py", edit_text="VALUE = 10\n",
        )

        # Dry run lists only m1, keeps m2
        dry = merge.prune_merged_dispatches(ws, [self.thread], yes=False)
        self.assertTrue(dry["dry_run"])
        self.assertEqual([c["dispatch_id"] for c in dry["candidates"]], ["1111111aaaaaaa1111111"])
        self.assertEqual([k["dispatch_id"] for k in dry["kept"]], ["2222222bbbbbbb2222222"])
        self.assertTrue(Path(m1["worktree"]).is_dir())

        # Execute prune --merged --yes via CLI (default: cleans worktree, keeps branch)
        out = io.StringIO()
        with patch.dict(os.environ, {cli.NESTED_ENV: ""}), patch("sys.stdout", out):
            cli.cmd_prune(ws, argparse.Namespace(
                thread=None, days=30, yes=True, keep_sessions=False, verbose=True, json=False, merged=True, delete_branch=False,
            ))
        # m1 worktree cleaned, branch kept; m2 intact; thread & manifest preserved!
        self.assertFalse(Path(m1["worktree"]).exists())
        self.assertTrue(merge.ref_exists(self.root, f"refs/heads/{m1['branch']}"))
        self.assertTrue(Path(m2["worktree"]).is_dir())
        self.assertTrue(merge.ref_exists(self.root, f"refs/heads/{m2['branch']}"))
        self.assertTrue(self.thread.is_dir())
        self.assertTrue(Path(m1["manifest"]).is_file())
        # Default prune --merged now sees no candidates for m1 (worktree already gone)
        dry_again = merge.prune_merged_dispatches(ws, [self.thread], yes=False)
        self.assertEqual(dry_again["candidates"], [])
        # prune --merged --delete-branch --yes deletes m1's kept branch
        out_del = io.StringIO()
        with patch.dict(os.environ, {cli.NESTED_ENV: ""}), patch("sys.stdout", out_del):
            cli.cmd_prune(ws, argparse.Namespace(
                thread=None, days=30, yes=True, keep_sessions=False, verbose=True, json=False, merged=True, delete_branch=True,
            ))
        self.assertFalse(merge.ref_exists(self.root, f"refs/heads/{m1['branch']}"))
        # Thread still protected from thread-deletion because m2 is unmerged!
        self.assertIn("保留", prune.why_keep(self.thread, cli.load_state(self.thread), None))

        # Now merge & clean m2 (default keeps branch) -> thread is no longer blocked by prune.why_keep!
        merge.merge_dispatch(ws, self.thread, dispatch_id="2222222bbbbbbb2222222", run_check=lambda r: {"exit_code": 0})
        self.assertTrue(merge.ref_exists(self.root, f"refs/heads/{m2['branch']}"))
        self.assertIsNone(prune.why_keep(self.thread, cli.load_state(self.thread), None))
        # Thread itself is STILL preserved after merge cleanup
        self.assertTrue(self.thread.is_dir())

    def test_legacy_agora_branch_supports_resume_finalize_and_merge(self):
        self.configure_parties()
        ws = cli.Workspace(self.root)
        did = "legacy0123456789abcdef"
        # Create legacy v1 dispatch (agora/<full_id>)
        meta = worktree.prepare(self.root, did, ["app.py", "demo/*"], self.thread)
        self.assertEqual(meta["branch"], f"agora/{did}")
        meta.update(worker="codex", commands={"check": [sys.executable, "-c", "pass"]})
        worktree.save(meta)
        self.venv(meta)
        cli.update_relay(
            self.thread,
            **{k: meta.get(k) for k in worktree.FIELDS},
            allowed_paths=meta["allowed_paths"],
            worker="codex",
            status="stopped",
            pid=99999999,
            pid_started=123.0,
        )
        (self.thread / "control.json").write_text('{"stop_requested": false}', encoding="utf-8")

        # 1. Resume legacy dispatch (keeps agora/<id> name!)
        resume_args = argparse.Namespace(
            thread=self.thread.name, sender="claude", to=None, message="繼續", file=None,
            session=None, from_summary=False, yes=False, tier=None, model=None, effort=None,
            subagents=None, trigger=None, allow=None, reason="續跑舊分派", user_override=False,
            dispatch_id=None, resume=did, name=None,
        )
        with patch.dict(os.environ, {cli.NESTED_ENV: ""}), patch("sys.stdout", io.StringIO()), \
                patch.object(cli, "party_usage", return_value={"remaining_pct": 80, "updated_at": time.time()}), \
                patch.object(cli.quota, "blocked", return_value=None), \
                patch.object(cli.models, "CACHE", self.root / "models.json"), \
                patch.object(cli.jobs, "spawn", return_value=77777777), \
                patch.object(cli.jobs, "pid_started", return_value=789.0):
            cli.cmd_assign(ws, resume_args)
        self.assertEqual(cli.load_state(self.thread)["relay"]["branch"], f"agora/{did}")
        cli.update_relay(self.thread, status="stopped")

        # 2. Finalize legacy dispatch
        self.modify(meta, "app.py", "VALUE = 42\n")
        self.message(meta)
        with patch.dict(os.environ, {cli.NESTED_ENV: ""}), patch("sys.stdout", io.StringIO()), \
                patch.object(cli, "run_task_check", return_value={"exit_code": 0, "output_tail": "OK"}):
            cli.cmd_finalize(ws, argparse.Namespace(thread=self.thread.name, dispatch_id=did))
        saved = self.read_meta(meta)
        self.assertEqual(saved["commit_status"], "committed")
        self.assertEqual(saved["branch"], f"agora/{did}")

        # 3. Merge & clean legacy dispatch (default keeps branch)
        res = merge.merge_dispatch(ws, self.thread, dispatch_id=did, run_check=lambda r: {"exit_code": 0})
        self.assertTrue(res["ok"])
        self.assertEqual(res["cleanup"]["status"], "cleaned")
        self.assertEqual((self.root / "app.py").read_text(encoding="utf-8"), "VALUE = 42\n")
        self.assertFalse(Path(meta["worktree"]).exists())
        self.assertTrue(merge.ref_exists(self.root, f"refs/heads/agora/{did}"))

    def test_cli_parsing_and_completions_description(self):
        with patch.object(cli, "cmd_merge") as mock_merge:
            cli.main(["--workspace", str(self.root), "merge", "t1", "--dispatch-id", "d1", "--keep"])
        parsed = mock_merge.call_args.args[1]
        self.assertEqual((parsed.thread, parsed.dispatch_id, parsed.keep, parsed.delete_branch), ("t1", "d1", True, False))

        with patch.object(cli, "cmd_merge") as mock_merge:
            cli.main(["--workspace", str(self.root), "merge", "t1", "--delete-branch"])
        parsed = mock_merge.call_args.args[1]
        self.assertEqual((parsed.thread, parsed.keep, parsed.delete_branch), ("t1", False, True))

        with patch.object(cli, "cmd_prune") as mock_prune:
            cli.main(["--workspace", str(self.root), "prune", "--merged", "--delete-branch", "--yes"])
        self.assertTrue(mock_prune.call_args.args[1].merged)
        self.assertTrue(mock_prune.call_args.args[1].delete_branch)
        self.assertTrue(mock_prune.call_args.args[1].yes)

        with patch.object(cli, "cmd_assign") as mock_assign:
            cli.main(["--workspace", str(self.root), "assign", "t1", "--from", "claude", "--allow", "app.py",
                      "--name", "feat-x", "--message", "hi"])
        self.assertEqual(mock_assign.call_args.args[1].name, "feat-x")

        base_item = {
            "id": "x" * 12,
            "thread": "t1",
            "topic": "測試",
            "type": "relay",
            "dispatch_id": "abcdef0123456789",
            "status": "done",
            "branch": "agy/測試-abcdef0",
            "commit_sha": "a" * 40,
            "commit_status": "committed",
        }
        desc = completions.describe(base_item)
        self.assertIn("branch=agy/測試-abcdef0", desc)
        self.assertIn("agora merge t1", desc)

        desc_kept_branch = completions.describe({
            **base_item,
            "merge_record": {"merged": True, "branch_kept": True},
            "cleanup_record": {"status": "cleaned", "worktree_removed": True, "branch_deleted": False, "branch_kept": True},
        })
        self.assertIn("已合併並清理 worktree，分支保留", desc_kept_branch)

        desc_del_branch = completions.describe({
            **base_item,
            "merge_record": {"merged": True, "branch_kept": False},
            "cleanup_record": {"status": "cleaned", "worktree_removed": True, "branch_deleted": True, "branch_kept": False},
        })
        self.assertIn("已合併並清理 worktree 與分支", desc_del_branch)


class TestMergeDependencyPreflight(MergeTestBase):
    def test_collect_dependencies_filters_expands_and_keeps_constraints_separate(self):
        tree = {
            "pyproject.toml": (
                '[project]\n'
                'name = "My_Demo_Pkg"\n'
                'dependencies = ["httpx>=0.24", "my-demo-pkg>=0.1", "direct @ https://example.com/a.whl"]\n'
                '[project.optional-dependencies]\n'
                'dev = ["my_demo_pkg[test]", "ruff>=0.1"]\n'
                'test = ["pytest>=7"]\n'
                'tests = ["coverage>=7"]\n'
                'docs = ["sphinx>=7"]\n'
            ),
            "requirements.txt": (
                "# comment\n"
                "-e .\n"
                "-e git+https://github.com/org/edit.git#egg=edit\n"
                "--find-links https://example.com/wheels\n"
                "--index-url https://pypi.org/simple\n"
                "git+https://github.com/org/vcs.git\n"
                "urlpkg @ https://example.com/urlpkg-1.0.whl\n"
                "./local-pkg\n"
                "../sibling-pkg\n"
                "wheel.whl\n"
                "my.demo.pkg==1.0\n"
                "-r reqs/base.txt\n"
                "-c constraints.txt\n"
                "requests>=2.28 \\\n"
                "    --hash=sha256:abcdef123456\n"
            ),
            "reqs/base.txt": (
                "-r ../requirements.txt\n"
                "-r extra.txt\n"
                "urllib3>=1.26  # inline comment\n"
            ),
            "reqs/extra.txt": "click==8.1.7\n",
            "constraints.txt": (
                "urllib3<3\n"
                "constraint-only-pkg==9.9.9\n"
                "-e .\n"
            ),
            "requirements-docs.txt": "mkdocs>=1.5\n",
        }
        deps = cli.collect_project_dependencies(tree.get)
        self.assertEqual(deps["sources"], ["pyproject.toml", "requirements.txt"])
        self.assertEqual(
            deps["requirements"],
            ["httpx>=0.24", "ruff>=0.1", "pytest>=7", "coverage>=7", "click==8.1.7", "urllib3>=1.26", "requests>=2.28"],
        )
        self.assertEqual(deps["constraints"], ["urllib3<3", "constraint-only-pkg==9.9.9"])
        self.assertNotIn("constraint-only-pkg==9.9.9", deps["requirements"])
        self.assertNotIn("sphinx>=7", deps["requirements"])
        self.assertNotIn("mkdocs>=1.5", deps["requirements"])

        skipped_reasons = {(item["item"], item["reason"]) for item in deps["skipped"]}
        self.assertIn(("-e .", "editable 安裝"), skipped_reasons)
        self.assertIn(("--find-links https://example.com/wheels", "pip 選項行"), skipped_reasons)
        self.assertIn(("--index-url https://pypi.org/simple", "pip 選項行"), skipped_reasons)
        self.assertIn(("git+https://github.com/org/vcs.git", "直接 URL／VCS"), skipped_reasons)
        self.assertIn(("urlpkg @ https://example.com/urlpkg-1.0.whl", "直接 URL／VCS"), skipped_reasons)
        self.assertIn(("./local-pkg", "本機路徑"), skipped_reasons)
        self.assertIn(("my.demo.pkg==1.0", "專案本身"), skipped_reasons)
        self.assertIn(("my_demo_pkg[test]", "專案本身"), skipped_reasons)
        self.assertIn(("requirements.txt", "循環引用 -r"), skipped_reasons)

    def test_missing_tomllib_skips_pyproject_with_notice_and_checks_requirements(self):
        tree = {
            "pyproject.toml": '[project]\nname = "demo"\ndependencies = ["httpx"]\n',
            "requirements.txt": "requests>=2.0\n",
        }
        with patch.object(cli, "tomllib", None):
            deps = cli.collect_project_dependencies(tree.get)
        self.assertEqual(deps["requirements"], ["requests>=2.0"])
        self.assertTrue(any("未預檢依賴" in n and "tomllib" in n for n in deps["notices"]))

    def test_missing_dependency_refuses_merge_without_venv_and_keeps_head(self):
        ws = cli.Workspace(self.root)
        (self.root / "tests").mkdir()
        (self.root / "tests/test_app.py").write_text(
            "import unittest\nclass T(unittest.TestCase):\n def test_ok(self):\n  self.assertTrue(True)\n",
            encoding="utf-8",
        )
        worktree.git(self.root, "add", "tests/test_app.py")
        worktree.git(self.root, "commit", "-m", "add unittest")
        self.make_committed_dispatch(
            allowed=["requirements.txt"],
            edit_file="requirements.txt",
            edit_text="agora-nonexistent-pkg-for-test-99999>=1.0\n",
        )
        head = worktree.git(self.root, "rev-parse", "HEAD")
        self.assertIsNone(cli.project_python(self.root))

        # Test both default check (no config) and custom Python check
        for check in (None, [sys._base_executable, "-m", "unittest", "discover", "-s", "tests"]):
            with self.subTest(check=check):
                config.save(self.root, {"check": check})
                with patch.object(merge, "run_main_check") as integration:
                    with self.assertRaisesRegex(
                        merge.MergeError,
                        r"整合環境缺失：缺少 agora-nonexistent-pkg-for-test-99999.*python -m venv \.venv.*pip install -r requirements\.txt",
                    ):
                        merge.merge_dispatch(ws, self.thread)
                integration.assert_not_called()
                self.assertEqual(worktree.git(self.root, "rev-parse", "HEAD"), head)

    def test_pip_report_nonempty_install_refuses_merge(self):
        ws = cli.Workspace(self.root)
        self.make_committed_dispatch(
            allowed=["requirements.txt"],
            edit_file="requirements.txt",
            edit_text="mplfinance>=0.12\n",
        )
        head = worktree.git(self.root, "rev-parse", "HEAD")
        fake_report = json.dumps({"install": [{"metadata": {"name": "mplfinance", "version": "0.12.10"}}]})
        real_run = subprocess.run

        def fake_pip_run(cmd, *args, **kwargs):
            if isinstance(cmd, list) and cmd[1:3] == ["-m", "pip"]:
                return subprocess.CompletedProcess(cmd, 0, fake_report, "")
            return real_run(cmd, *args, **kwargs)

        with patch.object(cli.subprocess, "run", side_effect=fake_pip_run), \
                patch.object(merge, "run_main_check") as integration:
            with self.assertRaisesRegex(merge.MergeError, r"整合環境缺失：缺少 mplfinance"):
                merge.merge_dispatch(ws, self.thread)
        integration.assert_not_called()
        self.assertEqual(worktree.git(self.root, "rev-parse", "HEAD"), head)

    def test_installed_dependencies_allow_merge_and_ignore_constraints_only_pkg(self):
        ws = cli.Workspace(self.root)
        (self.root / "constraints.txt").write_text("agora-constraint-only-missing-pkg==1.0\n", encoding="utf-8")
        worktree.git(self.root, "add", "constraints.txt")
        worktree.git(self.root, "commit", "-m", "add constraints")
        self.make_committed_dispatch(
            allowed=["requirements.txt"],
            edit_file="requirements.txt",
            edit_text="-c constraints.txt\npip\n",
        )
        # Real offline pip dry-run (--no-index): `pip` is installed, constraint-only pkg must NOT be required
        result = merge.merge_dispatch(ws, self.thread, keep=True, run_check=lambda r: {"exit_code": 0})
        self.assertTrue(result["ok"])
        self.assertEqual(result["preflight_warnings"], [])

    def test_merged_requirements_checks_both_main_and_dispatch_additions(self):
        ws = cli.Workspace(self.root)
        (self.root / "requirements.txt").write_text("base-1\nbase-2\nbase-3\nbase-4\nbase-5\n", encoding="utf-8")
        worktree.git(self.root, "add", "requirements.txt")
        worktree.git(self.root, "commit", "-m", "base requirements")
        self.make_committed_dispatch(
            allowed=["requirements.txt"],
            edit_file="requirements.txt",
            edit_text="base-1\nbase-2\nbase-3\nbase-4\nbase-5\ndispatch-added-pkg>=1.0\n",
        )
        # Main independently adds a package at the top of the same requirements.txt
        (self.root / "requirements.txt").write_text(
            "main-added-pkg>=2.0\nbase-1\nbase-2\nbase-3\nbase-4\nbase-5\n",
            encoding="utf-8",
        )
        worktree.git(self.root, "add", "requirements.txt")
        worktree.git(self.root, "commit", "-m", "main adds requirement")

        captured = {}
        real_run = subprocess.run

        def fake_run(cmd, *args, **kwargs):
            if isinstance(cmd, list) and cmd[1:3] == ["-m", "pip"]:
                r_idx = cmd.index("-r")
                captured["cmd"] = list(cmd)
                captured["env"] = dict(kwargs.get("env") or {})
                captured["reqs"] = Path(cmd[r_idx + 1]).read_text(encoding="utf-8").splitlines()
                return subprocess.CompletedProcess(cmd, 0, json.dumps({"install": []}), "")
            return real_run(cmd, *args, **kwargs)

        with patch.object(cli.subprocess, "run", side_effect=fake_run):
            result = merge.merge_dispatch(ws, self.thread, keep=True, run_check=lambda r: {"exit_code": 0})
        self.assertTrue(result["ok"])
        self.assertIn("main-added-pkg>=2.0", captured["reqs"])
        self.assertIn("dispatch-added-pkg>=1.0", captured["reqs"])
        self.assertIn("--isolated", captured["cmd"])
        self.assertIn("--no-index", captured["cmd"])
        self.assertIn("--dry-run", captured["cmd"])
        self.assertIn("--no-deps", captured["cmd"])
        self.assertEqual(captured["env"].get("PYTHONUTF8"), "1")
        self.assertEqual(captured["env"].get("PIP_DISABLE_PIP_VERSION_CHECK"), "1")

    def test_pip_tool_errors_and_old_git_warn_and_still_merge(self):
        cases = [
            ("no_pip", subprocess.CompletedProcess(["pip"], 1, "", "python.exe: No module named pip"), "沒有安裝 pip"),
            ("old_pip", subprocess.CompletedProcess(["pip"], 2, "", "no such option: --report"), "pip 版本過舊"),
            ("bad_json", subprocess.CompletedProcess(["pip"], 0, "not-json{{{", ""), "報告解析失敗"),
        ]
        for label, fake_proc, expected_text in cases:
            with self.subTest(case=label):
                ws = cli.Workspace(self.root)
                did = f"1122334455667788{label[:6].ljust(6, '0')}"
                self.make_committed_dispatch(
                    dispatch_id=did,
                    allowed=["requirements.txt"],
                    edit_file="requirements.txt",
                    edit_text=f"pkg-{label}>=1.0\n",
                )
                real_run = subprocess.run

                def fake_pip_run(cmd, *args, _proc=fake_proc, **kwargs):
                    if isinstance(cmd, list) and cmd[1:3] == ["-m", "pip"]:
                        return _proc
                    return real_run(cmd, *args, **kwargs)

                with patch.object(cli.subprocess, "run", side_effect=fake_pip_run):
                    result = merge.merge_dispatch(ws, self.thread, dispatch_id=did, keep=True, run_check=lambda r: {"exit_code": 0})
                self.assertTrue(result["ok"])
                self.assertTrue(any("未預檢依賴" in w and expected_text in w for w in result["preflight_warnings"]), result["preflight_warnings"])

        # Unsupported git merge-tree --write-tree warns and still merges
        ws = cli.Workspace(self.root)
        did = "9988776655443322oldgit"
        self.make_committed_dispatch(
            dispatch_id=did,
            allowed=["requirements.txt"],
            edit_file="requirements.txt",
            edit_text="pkg-oldgit>=1.0\n",
        )
        real_git_run = merge._git_run
        def fake_git_run(root, *args):
            if args[:2] == ("merge-tree", "--write-tree"):
                return subprocess.CompletedProcess(["git", *args], 129, "", "error: unknown option `write-tree'")
            return real_git_run(root, *args)

        with patch.object(merge, "_git_run", side_effect=fake_git_run):
            result = merge.merge_dispatch(ws, self.thread, dispatch_id=did, keep=True, run_check=lambda r: {"exit_code": 0})
        self.assertTrue(result["ok"])
        self.assertTrue(any("未預檢依賴" in w and "merge-tree" in w for w in result["preflight_warnings"]))

        # cmd_merge prints preflight warnings to stdout
        out = io.StringIO()
        with patch.object(merge, "merge_dispatch", return_value=result), patch("sys.stdout", out):
            merge.cmd_merge(ws, argparse.Namespace(thread=self.thread.name, dispatch_id=did, keep=True, delete_branch=False, json=False))
        self.assertIn("未預檢依賴", out.getvalue())

    def test_custom_non_python_check_skips_dep_preflight_with_notice(self):
        ws = cli.Workspace(self.root)
        self.make_committed_dispatch(
            allowed=["requirements.txt"],
            edit_file="requirements.txt",
            edit_text="agora-nonexistent-pkg-99999>=1.0\n",
        )
        # Direct `pytest` (not `python -m pytest`) or shell command skips dependency preflight with notice
        fake_pytest = self.root / ("pytest.cmd" if os.name == "nt" else "pytest")
        fake_pytest.write_text("@echo ok\n" if os.name == "nt" else "#!/bin/sh\necho ok\n", encoding="utf-8")
        if os.name != "nt":
            fake_pytest.chmod(0o755)
        config.save(self.root, {"check": [fake_pytest.name, "-q"]})
        result = merge.merge_dispatch(ws, self.thread, keep=True, run_check=lambda r: {"exit_code": 0})
        self.assertTrue(result["ok"])
        self.assertTrue(any("未預檢依賴" in w and "Python 直譯器" in w for w in result["preflight_warnings"]))

    def test_pip_find_links_env_is_ignored_by_isolated_flag(self):
        ws = cli.Workspace(self.root)
        wheelhouse = self.root / "wheelhouse"
        wheelhouse.mkdir()
        # Create a minimal valid wheel file for a fake package in PIP_FIND_LINKS
        import zipfile
        whl = wheelhouse / "agora_fake_isolated_pkg-1.0.0-py3-none-any.whl"
        with zipfile.ZipFile(whl, "w") as zf:
            zf.writestr("agora_fake_isolated_pkg-1.0.0.dist-info/METADATA", "Metadata-Version: 2.1\nName: agora-fake-isolated-pkg\nVersion: 1.0.0\n")
            zf.writestr("agora_fake_isolated_pkg-1.0.0.dist-info/WHEEL", "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
            zf.writestr("agora_fake_isolated_pkg-1.0.0.dist-info/RECORD", "")
        self.make_committed_dispatch(
            allowed=["requirements.txt"],
            edit_file="requirements.txt",
            edit_text="agora-fake-isolated-pkg==1.0.0\n",
        )
        head = worktree.git(self.root, "rev-parse", "HEAD")
        # Even with PIP_FIND_LINKS pointing to wheelhouse, --isolated ignores it and refuses merge
        with patch.dict(os.environ, {"PIP_FIND_LINKS": str(wheelhouse)}), \
                patch.object(merge, "run_main_check") as integration:
            with self.assertRaisesRegex(merge.MergeError, r"整合環境缺失：缺少 agora-fake-isolated-pkg"):
                merge.merge_dispatch(ws, self.thread)
        integration.assert_not_called()
        self.assertEqual(worktree.git(self.root, "rev-parse", "HEAD"), head)


class TestIntegrationCheckEvidence(MergeTestBase):
    def test_failed_integration_check_saves_full_logs_failed_ids_and_two_merges_do_not_overwrite(self):
        ws = cli.Workspace(self.root)
        (self.root / "tests").mkdir()
        test_lines = [
            "import unittest",
            "class EvidenceTest(unittest.TestCase):",
        ]
        for i in range(22):
            test_lines.append(f"    def test_fail_{i:02d}(self):")
            test_lines.append(f"        self.fail('MARKER_FAIL_{i:02d}_' + 'x' * 200)")
        for i in range(3):
            test_lines.append(f"    def test_err_{i:02d}(self):")
            test_lines.append(f"        raise RuntimeError('MARKER_ERR_{i:02d}_' + 'y' * 200)")
        (self.root / "tests" / "test_evidence.py").write_text("\n".join(test_lines) + "\n", encoding="utf-8")
        worktree.git(self.root, "add", "tests/test_evidence.py")
        worktree.git(self.root, "commit", "-m", "add failing tests")

        meta = self.make_committed_dispatch()
        dispatch_dir = Path(meta["manifest"]).parent

        # First merge: performs --no-ff merge, runs real integration check, fails
        res1 = merge.merge_dispatch(ws, self.thread)
        self.assertFalse(res1["ok"])
        self.assertFalse(res1["check_passed"])
        cr1 = res1["check_result"]
        log1 = Path(cr1["log"])
        test_log1 = Path(cr1["test_log"])
        rec1 = Path(cr1["record"])
        self.assertTrue(log1.is_file())
        self.assertTrue(test_log1.is_file())
        self.assertTrue(rec1.is_file())
        self.assertEqual(log1.parent.resolve(), dispatch_dir.resolve())
        self.assertTrue(log1.name.startswith("integration-check-"))
        self.assertTrue(test_log1.name.endswith(".tests.log"))

        # Full test output is preserved in .tests.log even though stdout/output_tail is truncated
        raw1 = test_log1.read_text(encoding="utf-8")
        self.assertIn("MARKER_FAIL_00_", raw1)
        self.assertIn("MARKER_ERR_02_", raw1)
        self.assertGreater(len(raw1), 3000)

        # All 25 failed/error test IDs parsed via baseline.py
        self.assertEqual(len(cr1["failed_tests"]), 25)
        self.assertTrue(cr1["failed_tests_complete"])
        self.assertEqual(cr1["failed_tests_framework"], "unittest")
        self.assertIn("test_evidence.EvidenceTest.test_fail_00", cr1["failed_tests"])
        self.assertIn("test_evidence.EvidenceTest.test_err_02", cr1["failed_tests"])

        # System note and progress.md list preview + total + log path
        transcript = (self.thread / "transcript.md").read_text(encoding="utf-8")
        self.assertIn("失敗／錯誤測試（共 25 項，列出前 20 項）", transcript)
        self.assertIn("test_evidence.EvidenceTest.test_fail_00", transcript)
        self.assertIn(str(test_log1), transcript)

        # cmd_merge output lists failed IDs and log path
        out = io.StringIO()
        with patch.object(merge, "merge_dispatch", return_value=res1), patch("sys.stdout", out):
            with self.assertRaises(SystemExit):
                merge.cmd_merge(ws, argparse.Namespace(thread=self.thread.name, json=False))
        rendered = out.getvalue()
        self.assertIn("失敗／錯誤測試（共 25 項，列出前 20 項）", rendered)
        self.assertIn("test_evidence.EvidenceTest.test_fail_00", rendered)
        self.assertIn(str(test_log1), rendered)

        # Second merge on the same dispatch: creates new log files without overwriting the first
        res2 = merge.merge_dispatch(ws, self.thread)
        self.assertFalse(res2["ok"])
        self.assertTrue(res2["already_merged"])
        cr2 = res2["check_result"]
        log2 = Path(cr2["log"])
        test_log2 = Path(cr2["test_log"])
        rec2 = Path(cr2["record"])
        self.assertNotEqual(log1, log2)
        self.assertNotEqual(test_log1, test_log2)
        self.assertNotEqual(rec1, rec2)
        self.assertTrue(log1.is_file() and log2.is_file())
        self.assertTrue(test_log1.is_file() and test_log2.is_file())
        self.assertTrue(rec1.is_file() and rec2.is_file())

    def test_environment_summary_fields_unset_markers_and_redaction(self):
        from agoralib import __version__
        (self.root / "tests").mkdir()
        (self.root / "tests" / "test_ok.py").write_text(
            "import unittest\nclass T(unittest.TestCase):\n def test_ok(self):\n  self.assertTrue(True)\n",
            encoding="utf-8",
        )
        record_dir = self.root / ".agora" / "test-env-records"
        clean_env = {k: v for k, v in os.environ.items() if k not in merge.ENV_SUMMARY_KEYS}
        clean_env["PYTHONPATH"] = "C:/demo/token=my_super_secret_123"

        with patch.dict(os.environ, clean_env, clear=True):
            # Verify _main_check_environment does not inject PYTHONUTF8 / PYTHONIOENCODING / PYTHONNOUSERSITE
            _, main_env = merge._main_check_environment(self.root)
            self.assertNotIn("PYTHONUTF8", main_env)
            self.assertNotIn("PYTHONIOENCODING", main_env)
            self.assertNotIn("PYTHONNOUSERSITE", main_env)

            cr = merge.run_main_check(self.root, record_dir=record_dir)

        self.assertEqual(cr["exit_code"], 0, cr)
        env_info = cr["environment"]
        self.assertEqual(env_info["agora_version"], __version__)
        self.assertTrue(env_info["python"])
        self.assertTrue(env_info["command"])
        self.assertEqual(Path(env_info["cwd"]).resolve(), self.root.resolve())
        self.assertTrue(env_info["stdout_encoding"])
        self.assertTrue(env_info["locale_encoding"])

        env_vars = env_info["env"]
        self.assertIn("[REDACTED]", env_vars["PYTHONPATH"])
        self.assertNotIn("my_super_secret_123", env_vars["PYTHONPATH"])
        for unset_key in ("PYTHONUTF8", "PYTHONIOENCODING", "VIRTUAL_ENV", "PYTHONNOUSERSITE", "PYTHONHOME"):
            self.assertEqual(env_vars[unset_key], "<未設定>")

    def test_timing_records_wait_and_run_seconds_separately(self):
        import threading
        from agoralib import checkqueue

        (self.root / "tests").mkdir()
        (self.root / "tests" / "test_slow.py").write_text(
            "import time, unittest\n"
            "class T(unittest.TestCase):\n"
            " def test_sleep(self):\n"
            "  time.sleep(0.15)\n",
            encoding="utf-8",
        )
        record_dir = self.root / ".agora" / "test-timing-records"
        slots_path = checkqueue.slots_dir(self.root)
        acquired = threading.Event()
        stop_holder = threading.Event()

        def hold_slot():
            with checkqueue.acquire_slot(self.root, label="blocker", poll_interval=0.05):
                acquired.set()
                deadline = time.monotonic() + 30.0
                while not stop_holder.is_set() and time.monotonic() < deadline:
                    if list(slots_path.glob("wait-*.json")):
                        time.sleep(0.3)
                        break
                    time.sleep(0.02)

        holder = threading.Thread(target=hold_slot, daemon=True)
        holder.start()
        self.assertTrue(acquired.wait(timeout=5.0))
        try:
            cr = merge.run_main_check(self.root, record_dir=record_dir)
        finally:
            stop_holder.set()
            holder.join(timeout=5.0)

        self.assertEqual(cr["exit_code"], 0, cr)
        timing = cr["timing"]
        self.assertIsNotNone(timing["wait_seconds"])
        self.assertIsNotNone(timing["run_seconds"])
        self.assertGreaterEqual(timing["wait_seconds"], 0.2)
        self.assertGreaterEqual(timing["run_seconds"], 0.1)
        record_data = json.loads(Path(cr["record"]).read_text(encoding="utf-8"))
        self.assertEqual(record_data["wait_seconds"], timing["wait_seconds"])
        self.assertEqual(record_data["run_seconds"], timing["run_seconds"])

    def test_already_merged_rerun_prints_verification_result(self):
        ws = cli.Workspace(self.root)
        meta = self.make_committed_dispatch()
        worktree.git(self.root, "merge", "--no-ff", "-m", "manual merge", meta["branch"])

        fake_check = {
            "exit_code": 0,
            "log": str(Path(meta["manifest"]).parent / "integration-check-test.log"),
            "timing": {"wait_seconds": 0.12, "run_seconds": 1.34, "total_seconds": 1.50},
        }
        out1 = io.StringIO()
        with patch.dict(os.environ, {cli.NESTED_ENV: ""}), \
                patch.object(merge, "run_main_check", return_value=fake_check) as mock_check, \
                patch("sys.stdout", out1):
            merge.cmd_merge(ws, argparse.Namespace(
                thread=self.thread.name, dispatch_id=None, keep=True, delete_branch=False, json=False,
            ))
        mock_check.assert_called_once()
        text1 = out1.getvalue()
        self.assertIn("略過建立合併節點", text1)
        self.assertIn("整合驗證通過（exit=0）", text1)
        self.assertIn("等待槽位 0.12s", text1)
        self.assertIn("實際執行 1.34s", text1)
        self.assertIn("integration-check-test.log", text1)

        # Second rerun after check already passed: does not re-run check or print pass banner again
        out2 = io.StringIO()
        with patch.dict(os.environ, {cli.NESTED_ENV: ""}), \
                patch.object(merge, "run_main_check", side_effect=AssertionError("should not re-run")), \
                patch("sys.stdout", out2):
            merge.cmd_merge(ws, argparse.Namespace(
                thread=self.thread.name, dispatch_id=None, keep=True, delete_branch=False, json=False,
            ))
        text2 = out2.getvalue()
        self.assertIn("略過建立合併節點", text2)
        self.assertNotIn("整合驗證通過", text2)

    def test_prune_merged_preview_prints_kept_reasons_without_verbose(self):
        ws = cli.Workspace(self.root)
        m1 = self.make_committed_dispatch(dispatch_id="aaa1111aaaaaaa1111111", worker="codex", title="已合併")
        merge.merge_dispatch(ws, self.thread, dispatch_id=m1["dispatch_id"], keep=True, run_check=lambda r: {"exit_code": 0})
        # Add an unknown untracked file to m1 so it is kept with a blocking reason
        (Path(m1["worktree"]) / "unknown-note.txt").write_text("keep", encoding="utf-8")

        self.make_committed_dispatch(
            dispatch_id="bbb2222bbbbbbb2222222", worker="antigravity", title="未合併",
            allowed=["other.py"], edit_file="other.py", edit_text="VALUE = 10\n",
        )

        out = io.StringIO()
        with patch.dict(os.environ, {cli.NESTED_ENV: ""}), patch("sys.stdout", out):
            cli.cmd_prune(ws, argparse.Namespace(
                thread=None, days=30, yes=False, keep_sessions=False, verbose=False, json=False, merged=True, delete_branch=False,
            ))
        preview = out.getvalue()
        self.assertIn("保留（不清理）", preview)
        self.assertIn("尚未合併到 main", preview)
        self.assertIn("unknown-note.txt", preview)

    def test_extract_failed_tests_supports_pytest_output(self):
        sample = (
            "FAILED tests/test_a.py::test_one - AssertionError: boom\n"
            "ERROR tests/test_b.py::test_two - RuntimeError: setup\n"
            "=================== 1 failed, 2 passed, 1 error in 0.12s ===================\n"
        )
        ids, complete, fw = merge._extract_failed_tests(sample, ["python", "-m", "pytest", "-q"], 1)
        self.assertEqual(ids, ["tests/test_a.py::test_one", "tests/test_b.py::test_two"])
        self.assertTrue(complete)
        self.assertEqual(fw, "pytest")


if __name__ == "__main__":
    unittest.main()
