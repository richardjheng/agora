"""Finalize retained dispatches in temporary repositories; no AI or network."""

import argparse
import io
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from agoralib import checkqueue, cli, completions, worktree
try:
    from . import test_worktree as fixtures
except ImportError:
    import test_worktree as fixtures


class FinalizeTests(unittest.TestCase):
    # Reuse repository fixtures without inheriting/rerunning the worktree suite.
    setUp = fixtures.WorktreeTests.setUp
    task = fixtures.WorktreeTests.task
    message = fixtures.WorktreeTests.message
    modify = fixtures.WorktreeTests.modify
    venv = fixtures.WorktreeTests.venv

    def retained(self, key="one", status="stopped"):
        meta = self.task(key)
        meta.update(worker="codex", commands={"check": None}, check_result={"exit_code": 0})
        worktree.save(meta)
        self.modify(meta)
        self.message(meta)
        cli.update_relay(self.thread, **{k: v for k, v in meta.items() if k != "thread"},
                         status=status, pid=99999999, pid_started=123.0)
        self.control(False)
        return meta

    def control(self, stopped, at="old-stop"):
        (self.thread / "control.json").write_text(json.dumps({"stop_requested": stopped, "at": at}), encoding="utf-8")

    def read_meta(self, meta):
        return json.loads(Path(meta["manifest"]).read_text(encoding="utf-8"))

    def finalize(self, key=None, check=None):
        with patch.dict(os.environ, {cli.NESTED_ENV: ""}), patch("sys.stdout", new_callable=io.StringIO) as output:
            with patch.object(cli, "run_task_check", side_effect=check or (lambda m: {"exit_code": 0})) as run:
                cli.cmd_finalize(cli.Workspace(self.root), argparse.Namespace(thread=self.thread.name, dispatch_id=key))
                return output.getvalue(), run

    def refused(self, text, key=None, check=None):
        with self.assertRaises(SystemExit) as caught:
            self.finalize(key, check)
        self.assertIn(text, str(caught.exception))
        self.assertNotEqual(caught.exception.code, 0)

    def test_paused_dispatch_commits_rechecks_and_updates_relay(self):
        meta = self.retained()
        output, run = self.finalize()
        saved = self.read_meta(meta)
        self.assertEqual(saved["commit_status"], "committed")
        self.assertIn(saved["commit_sha"], output)
        self.assertEqual(run.call_count, 1)  # Prior passing check never skips verification.
        relay = cli.load_state(self.thread)["relay"]
        self.assertEqual(relay["status"], "done")
        self.assertEqual(relay["branch"], meta["branch"])
        self.assertEqual(relay["commit_sha"], saved["commit_sha"])
        self.assertEqual(relay["pid"], 99999999)
        self.assertNotIn("finalizer", cli.load_state(self.thread))
        self.assertIn("Agora：finalize", (self.thread / "progress.md").read_text(encoding="utf-8"))
        self.assertEqual(worktree.git(self.root, "symbolic-ref", "--short", "HEAD"), "main")
        items, active = completions.snapshot(self.root)
        done = [x for x in items if x["status"] == "done"]
        self.assertFalse(active)
        self.assertEqual(done[0]["commit_sha"], saved["commit_sha"])

    def test_committed_is_idempotent_even_if_message_removed(self):
        meta = self.retained()
        self.finalize()
        before = self.read_meta(meta)
        Path(meta["commit_message"]).unlink()
        with patch.object(worktree, "finalize") as finalize:
            output, run = self.finalize()
        finalize.assert_not_called()
        run.assert_not_called()
        self.assertIn(before["commit_sha"], output)
        self.assertEqual(self.read_meta(meta), before)

    def test_live_pid_refuses_even_when_status_is_stopped(self):
        meta = self.retained()
        self.control(True)
        cli.update_relay(self.thread, pid=os.getpid(), pid_started=cli.jobs.pid_started(os.getpid()))
        self.refused("仍在執行")
        self.assertTrue(cli.stop_requested(self.thread))
        self.assertNotIn("stop_clearances", self.read_meta(meta))
        self.assertIsNone(self.read_meta(meta)["commit_sha"])

    def test_missing_pid_refuses_without_clearing_old_stop(self):
        meta = self.retained()
        self.control(True)
        cli.update_relay(self.thread, pid=None, pid_started=None)
        with patch.object(worktree, "finalize") as finalize:
            self.refused("PID 紀錄")
        finalize.assert_not_called()
        self.assertTrue(cli.stop_requested(self.thread))
        self.assertNotIn("stop_clearances", self.read_meta(meta))

    def test_starting_grace_refuses_even_if_pid_is_not_alive(self):
        meta = self.retained()
        self.control(True)
        cli.update_relay(self.thread, status="starting", started_ts=cli.time.time())
        self.refused("正在啟動")
        self.assertTrue(cli.stop_requested(self.thread))
        self.assertIsNone(self.read_meta(meta)["commit_sha"])

    def test_concurrent_finalize_and_assignment_refuse(self):
        meta = self.retained()

        def check(task):
            self.refused("仍在執行")
            with self.assertRaisesRegex(SystemExit, "正在 finalize"):
                cli._cmd_assign_locked(cli.Workspace(self.root),
                                       argparse.Namespace(thread=self.thread.name, sender="human"))
            self.assertEqual(cli.current_relay(self.thread)["dispatch_id"], meta["dispatch_id"])
            return {"exit_code": 0}

        self.finalize(check=check)
        self.assertIsNotNone(self.read_meta(meta)["commit_sha"])

    def test_committed_manifest_without_relay_remains_idempotent(self):
        meta = self.retained()
        self.finalize()
        saved = self.read_meta(meta)
        state = cli.load_state(self.thread)
        state.pop("relay")
        cli.save_state(self.thread, state)
        (Path(meta["manifest"]).parent / "relay.json").unlink()
        Path(meta["commit_message"]).unlink()
        with patch.object(worktree, "finalize") as finalize:
            for _ in range(2):
                output, run = self.finalize("one")
                self.assertIn(saved["commit_sha"], output)
                run.assert_not_called()
        finalize.assert_not_called()
        self.assertEqual(self.read_meta(meta), saved)

    def test_pid_and_creation_time_are_checked(self):
        self.retained()
        real = cli.jobs.pid_alive
        with patch.object(cli.jobs, "pid_alive", side_effect=real) as alive:
            self.finalize()
        self.assertIn(((99999999, 123.0), {}), [(x.args, x.kwargs) for x in alive.call_args_list])

    def test_missing_or_empty_message_refuses_before_check_or_stop_clear(self):
        meta = self.retained()
        self.control(True)
        for value in (None, "  \n"):
            if value is None:
                Path(meta["commit_message"]).unlink()
            else:
                Path(meta["commit_message"]).write_text(value, encoding="utf-8")
            with patch.object(worktree, "finalize") as finalize:
                self.refused("commit-message.md")
            finalize.assert_not_called()
            self.assertTrue(cli.stop_requested(self.thread))

    def test_out_of_scope_file_is_preserved_and_not_checked_or_committed(self):
        meta = self.retained()
        extra = Path(meta["worktree"]) / "leftover.txt"
        extra.write_text("retain this result", encoding="utf-8")
        self.refused("超出允許修改範圍", check=lambda m: self.fail("Out-of-scope work must not run checks"))
        self.assertEqual(extra.read_text(), "retain this result")
        self.assertEqual(worktree.git(Path(meta["worktree"]), "rev-parse", "HEAD"), meta["base_sha"])
        self.assertEqual(cli.load_state(self.thread)["relay"]["status"], "failed")

    def test_check_failure_retains_work_and_does_not_commit(self):
        meta = self.retained(status="failed")
        self.refused("check 失敗", check=lambda m: {"exit_code": 1})
        saved = self.read_meta(meta)
        self.assertEqual(saved["commit_status"], "check_failed")
        self.assertIsNone(saved["commit_sha"])
        self.assertEqual((Path(meta["worktree"]) / "app.py").read_text(), "VALUE = 2\n")

    def test_old_stop_cleared_with_manifest_audit_then_committed(self):
        meta = self.retained()
        self.control(True, at="2026-11-19 12:00:00")

        def check(task):
            self.assertFalse(cli.stop_requested(self.thread))
            entry = self.read_meta(task)["stop_clearances"][-1]
            self.assertEqual(entry["stop_at"], "2026-11-19 12:00:00")
            self.assertEqual(entry["by"], "finalize")
            self.assertTrue(entry["cleared_at"])
            return {"exit_code": 0}

        self.finalize(check=check)
        self.assertIsNotNone(self.read_meta(meta)["commit_sha"])

    def test_recall_during_finalize_prevents_commit(self):
        meta = self.retained()

        def check(task):
            self.assertEqual(cli.current_relay(self.thread)["status"], "checking")
            self.assertEqual(cli.activity(cli.Workspace(self.root))[0]["dispatch_id"], meta["dispatch_id"])
            cli.cmd_recall(cli.Workspace(self.root), argparse.Namespace(thread=self.thread.name, wait=None))
            return {"exit_code": 0}

        self.refused("已要求停止", check=check)
        self.assertTrue(cli.stop_requested(self.thread))
        self.assertIsNone(self.read_meta(meta)["commit_sha"])
        self.assertEqual(cli.load_state(self.thread)["relay"]["status"], "stopped")

    def test_stop_just_before_update_ref_prevents_commit(self):
        meta = self.retained()
        original = worktree.git

        def git(root, *args):
            result = original(root, *args)
            if args[0] == "commit-tree":
                self.control(True, "new-stop")
            return result

        with patch.object(worktree, "git", side_effect=git):
            self.refused("已要求停止")
        self.assertEqual(original(Path(meta["worktree"]), "rev-parse", "HEAD"), meta["base_sha"])

    def test_stop_while_waiting_for_check_slot_cancels(self):
        meta = self.retained()

        def check(task):
            def output(text):
                if "等待驗證中" in text:
                    self.control(True, "queued-stop")

            with self.assertRaises(checkqueue.CheckStopped):
                with checkqueue.acquire_slot(self.root, limit=1, poll_interval=0.001,
                        stop_check=lambda: cli.stop_requested(self.thread), output=output):
                    self.fail("Queued check must not start")
            return {"exit_code": 1}

        with checkqueue.acquire_slot(self.root, limit=1, output=lambda text: None):
            self.refused("未執行", check=check)
        self.assertIsNone(self.read_meta(meta)["commit_sha"])
        self.assertEqual(cli.load_state(self.thread)["relay"]["status"], "stopped")

    def test_stop_during_final_tree_check_prevents_ref_update(self):
        meta = self.retained()
        real = worktree.task_tree
        calls = 0

        def task_tree(task):
            nonlocal calls
            tree = real(task)
            calls += 1
            if calls == 3:  # The last tree comparison immediately before update-ref.
                self.control(True, "stop-during-tree-check")
            return tree

        with patch.object(worktree, "task_tree", side_effect=task_tree):
            self.refused("已要求停止")
        self.assertEqual(worktree.git(Path(meta["worktree"]), "rev-parse", "HEAD"), meta["base_sha"])

    def test_corrupt_control_fails_closed_before_or_during_finalize(self):
        meta = self.retained()
        path = self.thread / "control.json"
        path.write_text("{broken", encoding="utf-8")
        self.assertTrue(cli.stop_requested(self.thread))
        self.refused("control.json")
        self.assertEqual(path.read_text(), "{broken")
        self.control(False)

        def check(task):
            path.write_text("{broken-again", encoding="utf-8")
            return {"exit_code": 0}

        self.refused("已要求停止", check=check)
        self.assertIsNone(self.read_meta(meta)["commit_sha"])

    def test_missing_control_is_compatible_with_legacy_threads(self):
        self.retained()
        (self.thread / "control.json").unlink()
        self.assertFalse(cli.stop_requested(self.thread))
        self.finalize()

    def test_nonobject_and_unreadable_control_mean_stop(self):
        self.retained()
        (self.thread / "control.json").write_text("[]", encoding="utf-8")
        self.assertTrue(cli.stop_requested(self.thread))
        with patch.object(Path, "read_text", side_effect=PermissionError("denied")):
            self.assertTrue(cli.stop_requested(self.thread))

    def test_explicit_historical_dispatch_keeps_latest_relay(self):
        old = self.retained()
        latest = self.retained("two")
        self.finalize("one")
        self.assertIsNotNone(self.read_meta(old)["commit_sha"])
        self.assertIsNone(self.read_meta(latest)["commit_sha"])
        self.assertEqual(cli.load_state(self.thread)["relay"]["dispatch_id"], "two")
        saved = completions.read_object(Path(old["manifest"]).parent / "relay.json")
        self.assertEqual(saved["status"], "done")

    def test_historical_dispatch_without_pid_evidence_refuses(self):
        old = self.retained()
        self.retained("two")
        (Path(old["manifest"]).parent / "relay.json").unlink()
        self.refused("PID 紀錄", "one")

    def test_finalize_result_reported_after_old_stop_already_acknowledged(self):
        meta = self.retained()
        completions.deliver(self.root, lambda items: None)
        self.finalize()
        received = []
        count, active = completions.deliver(self.root, received.extend)
        self.assertEqual(count, 1)
        self.assertFalse(active)
        self.assertEqual(received[0]["commit_sha"], self.read_meta(meta)["commit_sha"])
        self.finalize()
        self.assertEqual(completions.deliver(self.root, lambda items: None)[0], 0)

    def test_missing_worktree_and_wrong_thread_refuse(self):
        meta = self.retained()
        meta["worktree"] = str(self.root / "missing-worktree")
        worktree.save(meta)
        cli.update_relay(self.thread, **{k: meta.get(k) for k in worktree.FIELDS})
        self.refused("worktree 不存在")
        meta["thread"] = str(self.root / "other-thread")
        worktree.save(meta)
        self.refused("討論串不符")

    def test_real_frozen_check_runs_in_task_environment(self):
        meta = self.retained()
        self.venv(meta)
        with patch.dict(os.environ, {cli.NESTED_ENV: ""}), patch("sys.stdout", new_callable=io.StringIO):
            cli.cmd_finalize(cli.Workspace(self.root), argparse.Namespace(thread=self.thread.name, dispatch_id=None))
        saved = self.read_meta(meta)
        self.assertEqual(saved["check_result"]["exit_code"], 0)
        self.assertEqual(saved["check_result"]["command"][0], str(worktree.python_path(Path(meta["worktree"]))))
        self.assertEqual(saved["commit_status"], "committed")

    def test_cli_and_host_only_boundary(self):
        self.retained()
        with patch.dict(os.environ, {cli.NESTED_ENV: ""}), patch.object(cli, "cmd_finalize") as finalize:
            cli.main(["--workspace", str(self.root), "finalize", "t1", "--dispatch-id", "one"])
        self.assertEqual(finalize.call_args.args[1].dispatch_id, "one")
        self.assertNotIn("finalize", cli.AGENT_COMMANDS)
        with patch.dict(os.environ, {cli.NESTED_ENV: "1"}), self.assertRaises(SystemExit):
            cli.cmd_finalize(cli.Workspace(self.root), argparse.Namespace(thread="t1", dispatch_id=None))


if __name__ == "__main__":
    unittest.main()
