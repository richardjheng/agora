"""Completion delivery across watch and hooks; isolated projects, no AI/network."""

import argparse
import io
import json
import multiprocessing
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from agoralib import cli, completions, jobs, quota_hook


def concurrent_delivery(root, queue):
    output = []
    completions.deliver(Path(root), lambda items: output.extend(i["id"] for i in items), wait=10)
    queue.put(output)


class WatchFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        env = patch.dict(os.environ, {"AGORA_WORKSPACE": "", "AGORA_WORKTREE": "",
                                     "AGORA_CONTROL_ROOT": "", "AGORA_INVOKED": ""})
        env.start()
        self.addCleanup(env.stop)
        self.ws = cli.Workspace(self.root)

    def thread(self, name="t1", relay=None):
        thread = self.ws.threads / name
        thread.mkdir(parents=True, exist_ok=True)
        cli.save_state(thread, {"topic": "工作 " + name, "relay": relay or {}, "messages": []})
        return thread

    def done(self, name="t1", **fields):
        return self.thread(name, {"status": "done", "worker": "codex", "dispatch_id": name,
                                  "branch": "agora/" + name, "commit_sha": "a" * 40,
                                  "commit_status": "committed", **fields})

    def watch(self, timeout=None, sleep=None):
        out = io.StringIO()
        code = 0
        with patch("sys.stdout", out), patch.object(cli.time, "sleep", side_effect=sleep):
            try:
                cli.cmd_watch(self.ws, argparse.Namespace(timeout=timeout))
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue()

    def deliver(self):
        output = []
        count, active = completions.deliver(self.root, output.extend)
        return output, count, active


class TestWatch(WatchFixture):
    def test_idle_returns_immediately_without_creating_home(self):
        code, out = self.watch()
        self.assertEqual(code, 0)
        self.assertIn("沒有進行中", out)
        self.assertFalse(self.ws.home.exists())

    def test_preexisting_result_has_branch_commit_next_step_and_is_reported_once(self):
        self.done()
        code, out = self.watch()
        self.assertEqual(code, 0)
        for text in ("分派 t1", "done", "branch=agora/t1", "a" * 40, "下一步", "審查 diff 與測試"):
            self.assertIn(text, out)
        self.assertEqual(self.deliver(), ([], 0, False))
        self.assertIn("沒有尚未回報", self.watch()[1])

    def test_any_worker_completion_returns_while_other_worker_still_active(self):
        first = self.thread("first", {"dispatch_id": "first", "worker": "codex", "status": "running", "pid": 123})
        self.thread("other", {"dispatch_id": "other", "worker": "antigravity", "status": "running", "pid": 124})

        def finish(_):
            cli.update_relay(first, status="done", commit_status="no_changes")

        with patch.object(jobs, "alive", return_value=True):
            code, out = self.watch(sleep=finish)
        self.assertEqual(code, 0)
        self.assertIn("分派 first", out)
        self.assertNotIn("分派 other /", out)
        self.assertIn("繼續執行 agora watch", out)

    def test_discussion_can_finish_before_relay(self):
        thread = self.thread("discussion")
        jobs.update(thread, job_id="chat1", status="running", kind="auto", pid=123)
        self.thread("work", {"status": "running", "worker": "codex", "pid": 124})
        with patch.object(jobs, "alive", return_value=True):
            code, out = self.watch(sleep=lambda _: jobs.update(thread, status="done", exit=0))
        self.assertEqual(code, 0)
        self.assertIn("討論 discussion / chat1", out)
        self.assertIn("閱讀逐字稿", out)

    def test_all_unreported_results_are_returned_together_including_failure(self):
        for i, status in enumerate(sorted(completions.TERMINAL)):
            self.done(str(i), status=status, commit_sha=None, commit_status="check_failed")
        code, out = self.watch()
        self.assertEqual(code, 0)  # completion notification is successful even if a worker failed
        for status in completions.TERMINAL:
            self.assertIn(status, out)
        self.assertIn("以較高等級重新分派", out)
        self.assertIn("commit=無（check_failed）", out)

    def test_timeout_zero_polls_once_and_leaves_background_work_untouched(self):
        thread = self.thread(relay={"dispatch_id": "run1", "worker": "codex", "status": "checking", "pid": 123})
        before = (thread / "state.json").read_bytes()
        with patch.object(jobs, "alive", return_value=True):
            code, out = self.watch(timeout=0)
        self.assertEqual(code, 2)
        self.assertIn("等待逾時", out)
        self.assertEqual(before, (thread / "state.json").read_bytes())
        self.assertFalse((self.ws.home / "completion-reported.json").exists())

    def test_timeout_uses_deadline_and_completion_at_boundary_wins(self):
        thread = self.thread(relay={"status": "running", "pid": 123})
        with patch.object(jobs, "alive", return_value=True), \
                patch.object(cli.time, "monotonic", side_effect=[0, 0, 1, 2, 3, 4]):
            code, _ = self.watch(timeout=2, sleep=lambda _: cli.update_relay(thread, status="failed"))
        self.assertEqual(code, 0)

    def test_positive_timeout_expires_without_consuming_active_work(self):
        self.thread(relay={"status": "running", "pid": 123})
        clock = [0]
        with patch.object(jobs, "alive", return_value=True), \
                patch.object(cli.time, "monotonic", side_effect=lambda: clock[0]):
            code, _ = self.watch(timeout=1, sleep=lambda duration: clock.__setitem__(0, clock[0] + duration))
        self.assertEqual(code, 2)
        self.assertEqual(clock[0], 1)

    def test_dead_relay_and_discussion_are_reported_as_interrupted(self):
        thread = self.thread(relay={"status": "running", "worker": "codex", "pid": 123})
        jobs.update(thread, status="running", kind="send", pid=124)
        with patch.object(jobs, "alive", return_value=False):
            items, count, active = self.deliver()
        self.assertEqual((count, active), (2, False))
        self.assertEqual({it["status"] for it in items}, {"interrupted"})

    def test_starting_grace_and_checking_are_not_completion(self):
        thread = self.thread(relay={"status": "starting", "started_ts": time.time()})
        jobs.update(thread, status="starting", started_ts=time.time())
        with patch.object(jobs, "alive", return_value=False):
            self.assertEqual(self.deliver(), ([], 0, True))
        cli.update_relay(thread, status="checking", pid=123)
        with patch.object(jobs, "alive", return_value=True):
            self.assertEqual(self.deliver(), ([], 0, True))

    def test_archived_dispatches_survive_reuse_and_removed_current_state(self):
        thread = self.thread()
        cli.update_relay(thread, dispatch_id="old", status="done", branch="agora/old")
        cli.update_relay(thread, dispatch_id="new", status="failed", branch="agora/new")
        (thread / "state.json").unlink()
        items, count, active = self.deliver()
        self.assertEqual((count, active), (2, False))
        self.assertEqual({it["dispatch_id"] for it in items}, {"old", "new"})

    def test_successive_discussions_get_distinct_id_and_preserve_results(self):
        thread = self.thread()
        with patch.object(jobs, "spawn", return_value=123):
            first = jobs.start(thread, "send", [], self.root)
            jobs.update(thread, status="done")
            second = jobs.start(thread, "send", [], self.root)
            jobs.update(thread, status="failed")
        self.assertNotEqual(first["job_id"], second["job_id"])
        self.assertEqual(self.deliver()[1], 2)

    def test_unarchived_legacy_discussion_is_saved_before_reuse(self):
        thread = self.thread()
        (thread / "job.json").write_text(json.dumps({"status": "done", "kind": "send", "started_ts": 1}), encoding="utf-8")
        with patch.object(jobs, "spawn", return_value=123):
            jobs.start(thread, "send", [], self.root)
        items, count, active = self.deliver()
        self.assertEqual((count, active), (1, True))
        self.assertEqual(items[0]["status"], "done")

    def test_failed_output_is_not_acknowledged(self):
        self.done()
        with self.assertRaises(BrokenPipeError):
            completions.deliver(self.root, lambda _: (_ for _ in ()).throw(BrokenPipeError()))
        self.assertEqual(self.deliver()[1], 1)

    def test_corrupt_ledger_is_not_overwritten_or_acknowledged(self):
        self.done()
        ledger = self.ws.home / "completion-reported.json"
        ledger.write_text("broken", encoding="utf-8")
        code, _ = self.watch()
        self.assertIn("無法讀寫完成回報紀錄", code)
        self.assertEqual(ledger.read_text(encoding="utf-8"), "broken")

    def test_corrupt_thread_does_not_hide_other_results(self):
        broken = self.thread("broken")
        (broken / "state.json").write_text("[", encoding="utf-8")
        self.done()
        self.assertEqual(self.deliver()[1], 1)

    def test_damaged_job_record_does_not_hide_same_threads_relay(self):
        thread = self.done()
        (thread / "job.json").write_text("[", encoding="utf-8")
        self.assertEqual(self.deliver()[1], 1)

    def test_concurrent_watchers_deliver_each_result_once(self):
        self.done()
        ctx = multiprocessing.get_context("spawn")
        queue = ctx.Queue()
        processes = [ctx.Process(target=concurrent_delivery, args=(str(self.root), queue)) for _ in range(3)]
        try:
            for proc in processes:
                proc.start()
            for proc in processes:
                proc.join(20)
                self.assertEqual(proc.exitcode, 0)
            self.assertEqual(sum(len(queue.get(timeout=5)) for _ in processes), 1)
        finally:
            for proc in processes:
                if proc.is_alive():
                    proc.terminate()
                    proc.join(5)
            queue.close()

    def test_cli_parser_and_negative_timeout(self):
        with patch.object(cli, "cmd_watch") as command:
            cli.main(["--workspace", str(self.root), "watch", "--timeout", "7"])
        self.assertEqual(command.call_args.args[1].timeout, 7)
        self.assertIn("--timeout 必須", self.watch(timeout=-1)[0])

    def test_controller_workspace_is_used(self):
        self.done()
        with patch.dict(os.environ, {"AGORA_WORKTREE": str(self.root / "task"),
                                     "AGORA_CONTROL_ROOT": str(self.root)}):
            task = cli.Workspace(self.root / "task")
        out = io.StringIO()
        with patch("sys.stdout", out):
            cli.cmd_watch(task, argparse.Namespace(timeout=0))
        self.assertIn("分派 t1", out.getvalue())
        self.assertFalse((task.root / ".agora").exists())


class TestCompletionHooks(WatchFixture):
    def hook(self, mode, invoked=False, invocation=1):
        stdout = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
        stdin = io.TextIOWrapper(io.BytesIO(json.dumps({"invocationNum": invocation}).encode()), encoding="utf-8")
        with patch("sys.stdout", stdout), patch("sys.stdin", stdin), patch("sys.stderr", io.StringIO()), \
                patch("sys.argv", ["quota_hook.py", mode]), \
                patch.dict(os.environ, {"AGORA_INVOKED": "1" if invoked else "", quota_hook.quota.PROBE_ENV: ""}), \
                patch.object(quota_hook, "project_root", return_value=self.root), \
                patch.object(quota_hook.quota, "AGORA_HOME", self.root), \
                patch.object(quota_hook.quota, "all_usage", return_value={}), \
                patch.object(quota_hook, "load_state", return_value={}), \
                patch.object(quota_hook, "routing_line", return_value=None), \
                patch.object(quota_hook.quota, "refresh_in_background") as refresh:
            quota_hook.main()
            refresh.assert_not_called()
        return json.loads(stdout.buffer.getvalue())

    def test_all_four_hooks_report_new_results_and_share_watch_ledger(self):
        for mode in ("claude-prompt", "claude-tool", "codex-prompt", "agy-pre"):
            with self.subTest(mode=mode):
                self.done(mode)
                out = self.hook(mode, invocation=2)
                text = json.dumps(out, ensure_ascii=False)
                self.assertIn("上次回報後已結束的分派／討論", text)
                self.assertIn("branch=agora/" + mode, text)
                self.assertNotIn("上次回報後已結束", json.dumps(self.hook(mode), ensure_ascii=False))
                self.assertEqual(self.deliver()[1], 0)

    def test_invoked_workers_do_not_report_or_consume_completions(self):
        self.done()
        for mode in ("claude-prompt", "claude-tool", "codex-prompt", "agy-pre"):
            self.assertNotIn("上次回報後已結束", json.dumps(self.hook(mode, invoked=True), ensure_ascii=False))
        self.assertEqual(self.deliver()[1], 1)

    def test_watch_acknowledgement_suppresses_hook_and_tool_hook_is_otherwise_empty(self):
        self.done()
        self.watch()
        self.assertEqual(self.hook("claude-tool"), {})

    def test_corrupt_ledger_keeps_quota_hook_usable(self):
        self.done()
        (self.ws.home / "completion-reported.json").write_text("broken", encoding="utf-8")
        out = self.hook("claude-prompt")
        self.assertIn("【額度】", out["hookSpecificOutput"]["additionalContext"])
        self.assertNotIn("上次回報後已結束", json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
