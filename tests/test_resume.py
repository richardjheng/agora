"""Resume & scope-extension integration tests in temporary Git repositories; no AI or network."""

import argparse
import io
import json
import multiprocessing
import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from agoralib import cli, completions, config, parties, shares, snapshot, worktree
try:
    from . import test_worktree as fixtures
except ImportError:
    import test_worktree as fixtures


def _process_resume_worker(root, thread_name, dispatch_id, result_file):
    ws = cli.Workspace(Path(root))
    args = argparse.Namespace(
        thread=thread_name, sender="claude", to=None, message=None, file=None,
        session=None, from_summary=False, yes=False, tier=None, model=None, effort=None,
        subagents=None, trigger=None, allow=None, reason=None, user_override=False,
        dispatch_id=None, resume=dispatch_id,
    )
    try:
        with patch.dict(os.environ, {cli.NESTED_ENV: ""}), patch("sys.stdout", io.StringIO()), \
                patch.object(cli.sandbox, "network_restricted", return_value=False), \
                patch.object(cli.sandbox, "is_codex_sandbox", return_value=False), \
                patch.object(cli, "party_usage", return_value={"remaining_pct": 80, "updated_at": time.time()}), \
                patch.object(cli.quota, "blocked", return_value=None), \
                patch.object(cli.models, "CACHE", Path(root) / "models.json"), \
                patch.object(cli.jobs, "spawn", return_value=99999999), \
                patch.object(cli.jobs, "pid_started", return_value=None):
            cli.cmd_assign(ws, args)
        Path(result_file).write_text("ok", encoding="utf-8")
    except SystemExit as exc:
        Path(result_file).write_text(f"exit:{exc}", encoding="utf-8")


class ResumeTests(unittest.TestCase):
    setUp = fixtures.WorktreeTests.setUp
    task = fixtures.WorktreeTests.task
    message = fixtures.WorktreeTests.message
    modify = fixtures.WorktreeTests.modify
    venv = fixtures.WorktreeTests.venv

    def configure(self):
        config.save(self.root, {
            "parties": {
                "claude": {"enabled": True, "share": 0},
                "codex": {
                    "enabled": True,
                    "share": 10,
                    "tiers": {"heavy": {"model": "codex-heavy-m", "effort": "high"}},
                },
                "antigravity": {
                    "enabled": True,
                    "share": 5,
                    "tiers": {"heavy": {"model": "agy-heavy-m"}},
                },
            },
            "check": [sys.executable, "-c", "pass"],
        })

    def read_meta(self, meta):
        return json.loads(Path(meta["manifest"]).read_text(encoding="utf-8"))

    def control(self, stopped, at="2026-11-19 12:00:00"):
        (self.thread / "control.json").write_text(
            json.dumps({"stop_requested": stopped, "at": at}), encoding="utf-8"
        )

    def assign_initial(self, dispatch_id="d1", allow=None, tier="heavy", text="修改 app.py 並補測試",
                       to=None, reason=None, subagents=None):
        self.configure()
        ws = cli.Workspace(self.root)
        args = argparse.Namespace(
            thread=self.thread.name, sender="claude", to=to, message=text, file=None,
            session=None, from_summary=False, yes=False, tier=tier, model=None, effort=None,
            subagents=subagents, trigger=None, allow=allow if allow is not None else ["app.py"],
            reason=reason, user_override=False, dispatch_id=dispatch_id, resume=None,
        )
        with patch.dict(os.environ, {cli.NESTED_ENV: ""}), patch("sys.stdout", io.StringIO()), \
                patch.object(cli, "party_usage", return_value={"remaining_pct": 80, "updated_at": time.time()}), \
                patch.object(cli.quota, "blocked", return_value=None), \
                patch.object(cli.models, "CACHE", self.root / "models.json"), \
                patch.object(cli.jobs, "spawn", return_value=99999999), \
                patch.object(cli.jobs, "pid_started", return_value=123.0):
            cli.cmd_assign(ws, args)
        relay = cli.load_state(self.thread)["relay"]
        meta = worktree.load(relay)
        self.venv(meta)
        return ws, relay, meta

    def run_worker(self, ws, dispatch_id, handler, run_id=None, check=None):
        with patch.object(cli, "party_usage", return_value={"remaining_pct": 80, "updated_at": time.time()}), \
                patch.object(cli, "call_party", side_effect=handler) as call, \
                patch.object(cli, "run_task_check", side_effect=check or (lambda m: {"exit_code": 0})), \
                patch("sys.stdout", io.StringIO()):
            cli.cmd_work(ws, argparse.Namespace(thread=self.thread.name, dispatch_id=dispatch_id, run_id=run_id))
        return call

    def resume_call(self, ws, dispatch_id="d1", allow=None, reason=None, message=None, file=None,
                    to=None, user_override=False, subagents=None, usages=None, extra_args=None):
        ns = dict(
            thread=self.thread.name, sender="claude", to=to, message=message, file=file,
            session=None, from_summary=False, yes=False, tier=None, model=None, effort=None,
            subagents=subagents, trigger=None, allow=allow, reason=reason,
            user_override=user_override, dispatch_id=None, resume=dispatch_id,
        )
        if extra_args:
            ns.update(extra_args)
        out = io.StringIO()
        with patch.dict(os.environ, {cli.NESTED_ENV: ""}), patch("sys.stdout", out), \
                patch.object(cli, "party_usage", side_effect=lambda p, m=None: usages.get(p) if usages is not None
                             else {"remaining_pct": 80, "updated_at": time.time()}), \
                patch.object(cli.quota, "blocked", return_value=None), \
                patch.object(cli.models, "CACHE", self.root / "models.json"), \
                patch.object(cli.jobs, "spawn", return_value=88888888) as spawn, \
                patch.object(cli.jobs, "pid_started", return_value=456.0):
            cli.cmd_assign(ws, argparse.Namespace(**ns))
        return out.getvalue(), spawn

    def pause_initial(self, dispatch_id="d1", allow=None, session_id="sess-orig-1",
                      edit_app="VALUE = 2\n", stop_after=False):
        ws, relay, meta = self.assign_initial(dispatch_id=dispatch_id, allow=allow)

        def first_round(party, prompt, session, opts, is_work, cwd, timeout, scratch, live=None):
            if edit_app is not None:
                self.modify(meta, "app.py", edit_app)
            if stop_after:
                self.control(True, at="2026-11-19 12:05:00")
            return "先暫停\n【已暫停】", session_id, "ok"

        self.run_worker(ws, dispatch_id, first_round)
        return ws, cli.load_state(self.thread)["relay"], self.read_meta(meta)

    def test_paused_resume_continues_same_worktree_session_and_commits_combined_changes(self):
        ws, paused, meta = self.pause_initial(allow=["app.py", "demo/*"])
        orig_branch = meta["branch"]
        orig_worktree = meta["worktree"]
        orig_base = meta["base_sha"]
        orig_commands = dict(meta["commands"])
        orig_model = paused["model"]
        orig_tier = paused["tier"]

        # Acknowledge initial stopped completion so we can verify resume emits a new completion.
        initial_items = []
        completions.deliver(self.root, initial_items.extend)
        self.assertEqual(len(initial_items), 1)
        self.assertEqual(initial_items[0]["status"], "stopped")

        with patch.object(worktree, "prepare") as prep:
            output, spawn = self.resume_call(ws, "d1", message="請順便補上 demo/__init__.py", reason="繼續完成")
        prep.assert_not_called()
        spawn.assert_called_once()
        self.assertIn("已續跑分派 d1", output)

        resumed = cli.load_state(self.thread)["relay"]
        self.assertEqual(resumed["branch"], orig_branch)
        self.assertEqual(resumed["worktree"], orig_worktree)
        self.assertEqual(resumed["base_sha"], orig_base)
        self.assertEqual(resumed["model"], orig_model)
        self.assertEqual(resumed["tier"], orig_tier)
        self.assertEqual(resumed["session_id"], "sess-orig-1")
        self.assertEqual(resumed["resume_count"], 1)
        self.assertEqual(len(resumed["resumes"]), 1)
        self.assertEqual(resumed["resumes"][0]["reason"], "繼續完成")
        self.assertTrue(resumed["resumes"][0]["session_continued"])
        self.assertEqual(self.read_meta(meta)["commands"], orig_commands)

        progress_text = (self.thread / "progress.md").read_text(encoding="utf-8")
        self.assertIn("續跑第 1 次", progress_text)
        self.assertIn("請順便補上 demo/__init__.py", progress_text)

        seen = {}

        def second_round(party, prompt, session, opts, is_work, cwd, timeout, scratch, live=None):
            seen.update(party=party, prompt=prompt, session=session, cwd=cwd, opts=opts)
            self.assertEqual((Path(cwd) / "app.py").read_text(encoding="utf-8"), "VALUE = 2\n")
            self.modify(meta, "demo/__init__.py", "VALUE = 20\n")
            self.message(meta)
            return "完成\n【工作完成】", session, "ok"

        self.run_worker(ws, "d1", second_round, run_id=resumed["run_id"])

        self.assertEqual(seen["session"], "sess-orig-1")
        self.assertIn("【Agora 工作分派・繼續】", seen["prompt"])
        self.assertIn("請順便補上 demo/__init__.py", seen["prompt"])
        self.assertEqual(seen["cwd"], Path(orig_worktree))

        done = cli.load_state(self.thread)["relay"]
        saved_meta = self.read_meta(meta)
        self.assertEqual(done["status"], "done")
        self.assertEqual(done["commit_status"], "committed")
        self.assertEqual(done["commit_sha"], saved_meta["commit_sha"])
        changed_files = [x["path"] for x in snapshot.branch_changes(self.root, orig_base, orig_branch)]
        self.assertEqual(sorted(changed_files), ["app.py", "demo/__init__.py"])

        # History in dispatches/<id>/relay.json and completions:
        archived = completions.read_object(Path(meta["manifest"]).parent / "relay.json")
        self.assertEqual(archived["resume_count"], 1)
        self.assertEqual(len(archived["resumes"]), 1)
        self.assertEqual(archived["commit_sha"], saved_meta["commit_sha"])

        new_items = []
        count, active = completions.deliver(self.root, new_items.extend)
        self.assertEqual(count, 1)
        self.assertFalse(active)
        self.assertEqual(new_items[0]["status"], "done")
        self.assertEqual(new_items[0]["commit_sha"], saved_meta["commit_sha"])
        # Both initial stopped and resumed done completion files exist on disk.
        self.assertGreaterEqual(len(list((self.root / ".agora" / "completions").glob("*.json"))), 2)

    def test_scope_extension_records_audit_invalidates_old_check_and_commits_new_paths(self):
        ws, paused, meta = self.pause_initial(allow=["app.py"])
        # Simulate prior check_result & checked_tree on manifest before scope extension.
        meta["check_result"] = {"exit_code": 0, "time": "2026-11-19 12:01:00"}
        meta["checked_tree"] = "deadbeef" * 5
        meta["commit_status"] = "check_failed"
        worktree.save(meta)
        cli.update_relay(self.thread, check_result=meta["check_result"], commit_status="check_failed")

        # Missing --reason must refuse without mutating manifest or check_result.
        self.control(True, at="2026-11-19 12:04:00")
        with self.assertRaisesRegex(SystemExit, "--reason"):
            self.resume_call(ws, "d1", allow=["other.py"])
        self.assertEqual(self.read_meta(meta)["allowed_paths"], ["app.py"])
        self.assertIsNotNone(self.read_meta(meta)["check_result"])
        self.assertTrue(cli.stop_requested(self.thread))

        # Secret literals must still be refused before touching manifest or control.json.
        for secret in (".env", ".env.local", "private.key", "../escape.py"):
            with self.subTest(secret=secret), self.assertRaises(SystemExit) as caught:
                self.resume_call(ws, "d1", allow=[secret], reason="不合法範圍")
            self.assertTrue("機密檔規則" in str(caught.exception) or "無效的允許範圍" in str(caught.exception))
            self.assertEqual(self.read_meta(meta)["allowed_paths"], ["app.py"])
            self.assertTrue(cli.stop_requested(self.thread))

        # Valid extension (comma-separated + literal secret template .env.example) succeeds.
        self.resume_call(ws, "d1", allow=["other.py, .env.example"], reason="需要同步更新 other.py 與範本")

        updated_meta = self.read_meta(meta)
        updated_relay = cli.load_state(self.thread)["relay"]
        self.assertEqual(updated_meta["allowed_paths"], ["app.py", "other.py", ".env.example"])
        self.assertEqual(updated_relay["allowed_paths"], updated_meta["allowed_paths"])
        self.assertEqual(worktree.load(updated_relay)["allowed_paths"], ["app.py", "other.py", ".env.example"])
        # Old check_result & checked_tree invalidated, commit_status back to pending.
        self.assertIsNone(updated_meta["check_result"])
        self.assertIsNone(updated_meta["checked_tree"])
        self.assertEqual(updated_meta["commit_status"], "pending")
        self.assertEqual(updated_relay["commit_status"], "pending")
        self.assertIsNone(updated_relay["check_result"])
        # scope_extensions audit record.
        ext = updated_meta["scope_extensions"][-1]
        self.assertEqual(ext["paths"], ["other.py", ".env.example"])
        self.assertEqual(ext["reason"], "需要同步更新 other.py 與範本")
        self.assertEqual(ext["by"], "claude")
        self.assertTrue(ext["at"])

        # Worker modifies newly allowed paths and commits cleanly.
        def finish_worker(party, prompt, session, opts, is_work, cwd, timeout, scratch, live=None):
            self.assertIn("other.py", prompt)
            self.assertIn(".env.example", prompt)
            self.modify(meta, "other.py", "VALUE = 10\n")
            self.modify(meta, ".env.example", "SAMPLE_KEY=placeholder\n")
            self.message(meta)
            return "完成\n【工作完成】", session, "ok"

        self.run_worker(ws, "d1", finish_worker, run_id=updated_relay["run_id"])
        final_meta = self.read_meta(meta)
        self.assertEqual(final_meta["commit_status"], "committed")
        committed_files = worktree.git(
            Path(meta["worktree"]), "diff", "--name-only", final_meta["base_sha"], final_meta["commit_sha"]
        ).splitlines()
        self.assertEqual(sorted(committed_files), [".env.example", "app.py", "other.py"])
        self.assertEqual(
            worktree.git(Path(meta["worktree"]), "show", f"{final_meta['commit_sha']}:.env.example"),
            "SAMPLE_KEY=placeholder",
        )

    def test_committed_dispatch_refuses_resume_including_recovered_commit(self):
        ws, paused, meta = self.pause_initial()
        self.message(meta)
        worktree.finalize(meta, lambda m: {"exit_code": 0})
        cli.update_relay(self.thread, **{k: meta.get(k) for k in worktree.FIELDS}, status="done")

        with self.assertRaisesRegex(SystemExit, "已提交.*改開新分派"):
            self.resume_call(ws, "d1")

        # Also simulate unrecorded commit recovered via recover_commit().
        meta["commit_sha"] = None
        meta["commit_status"] = "checked"
        worktree.save(meta)
        cli.update_relay(self.thread, commit_sha=None, commit_status="checked", status="stopped")
        with self.assertRaisesRegex(SystemExit, "已提交.*改開新分派"):
            self.resume_call(ws, "d1")

    def test_active_worker_finalizer_missing_pid_worktree_or_corrupt_control_refuse(self):
        ws, paused, meta = self.pause_initial(stop_after=True)

        # Live worker PID refuses without clearing stop.
        cli.update_relay(self.thread, pid=os.getpid(), pid_started=cli.jobs.pid_started(os.getpid()))
        with self.assertRaisesRegex(SystemExit, "仍在執行"):
            self.resume_call(ws, "d1")
        self.assertTrue(cli.stop_requested(self.thread))
        self.assertNotIn("stop_clearances", self.read_meta(meta))

        # Restore dead PID, then test active finalizer.
        cli.update_relay(self.thread, pid=99999999, pid_started=123.0)
        state = cli.load_state(self.thread)
        state["finalizer"] = {"dispatch_id": "d1", "status": "checking", "pid": os.getpid(),
                              "pid_started": cli.jobs.pid_started(os.getpid())}
        cli.save_state(self.thread, state)
        with self.assertRaisesRegex(SystemExit, "仍在執行"):
            self.resume_call(ws, "d1")
        state.pop("finalizer")
        cli.save_state(self.thread, state)

        # Starting grace window refuses.
        cli.update_relay(self.thread, status="starting", started_ts=time.time())
        with self.assertRaisesRegex(SystemExit, "正在啟動"):
            self.resume_call(ws, "d1")
        cli.update_relay(self.thread, status="stopped")

        # Missing PID evidence refuses.
        cli.update_relay(self.thread, pid=None, pid_started=None)
        with self.assertRaisesRegex(SystemExit, "PID 紀錄"):
            self.resume_call(ws, "d1")
        cli.update_relay(self.thread, pid=99999999, pid_started=123.0)

        # Corrupt control.json fails closed without overwriting it.
        (self.thread / "control.json").write_text("{corrupt", encoding="utf-8")
        with self.assertRaisesRegex(SystemExit, "control.json"):
            self.resume_call(ws, "d1")
        self.assertEqual((self.thread / "control.json").read_text(encoding="utf-8"), "{corrupt")
        self.control(True, at="2026-11-19 12:05:00")

        # Missing worktree directory refuses.
        bad_meta = self.read_meta(meta)
        orig_wt = bad_meta["worktree"]
        bad_meta["worktree"] = str(self.root / "no-such-worktree")
        worktree.save(bad_meta)
        cli.update_relay(self.thread, worktree=bad_meta["worktree"])
        with self.assertRaisesRegex(SystemExit, "worktree 不存在"):
            self.resume_call(ws, "d1")

        # Wrong thread in manifest refuses.
        bad_meta["worktree"] = orig_wt
        bad_meta["thread"] = str(self.root / "wrong-thread")
        worktree.save(bad_meta)
        cli.update_relay(self.thread, worktree=orig_wt)
        with self.assertRaisesRegex(SystemExit, "討論串不符"):
            self.resume_call(ws, "d1")

    def test_stop_clearance_audit_and_new_stop_during_resumed_run(self):
        ws, paused, meta = self.pause_initial(stop_after=True)
        self.assertTrue(cli.stop_requested(self.thread))

        self.resume_call(ws, "d1", reason="確認暫停後繼續")
        self.assertFalse(cli.stop_requested(self.thread))
        clearance = self.read_meta(meta)["stop_clearances"][-1]
        self.assertEqual(clearance["by"], "resume")
        self.assertEqual(clearance["stop_at"], "2026-11-19 12:05:00")
        self.assertTrue(clearance["cleared_at"])

        resumed = cli.load_state(self.thread)["relay"]

        def worker_hit_by_new_stop(party, prompt, session, opts, is_work, cwd, timeout, scratch, live=None):
            self.modify(meta, "app.py", "VALUE = 3\n")
            self.message(meta)
            # New stop arrives while resumed worker is running.
            self.control(True, at="2026-11-19 12:20:00")
            return "完成\n【工作完成】", session, "ok"

        self.run_worker(ws, "d1", worker_hit_by_new_stop, run_id=resumed["run_id"])
        self.assertEqual(cli.load_state(self.thread)["relay"]["status"], "stopped")
        self.assertIsNone(self.read_meta(meta)["commit_sha"])
        self.assertEqual(worktree.git(Path(meta["worktree"]), "rev-parse", "HEAD"), meta["base_sha"])

    def test_concurrent_two_resumes_only_one_succeeds(self):
        ws, paused, meta = self.pause_initial()
        ctx = multiprocessing.get_context("spawn")
        r1 = self.root / "r1.txt"
        r2 = self.root / "r2.txt"
        p1 = ctx.Process(target=_process_resume_worker, args=(str(self.root), self.thread.name, "d1", str(r1)))
        p2 = ctx.Process(target=_process_resume_worker, args=(str(self.root), self.thread.name, "d1", str(r2)))
        try:
            p1.start()
            p2.start()
            for p in (p1, p2):
                p.join(30)
                self.assertEqual(p.exitcode, 0)
        finally:
            for p in (p1, p2):
                if p.is_alive():
                    p.terminate()
                    p.join(5)
        results = [r1.read_text(encoding="utf-8"), r2.read_text(encoding="utf-8")]
        self.assertEqual(results.count("ok"), 1, f"Expected exactly 1 success, got {results}")
        self.assertEqual(sum(1 for x in results if x.startswith("exit:")), 1)
        self.assertEqual(cli.load_state(self.thread)["relay"]["resume_count"], 1)

    def test_concurrent_resume_and_finalize_mutually_exclusive(self):
        # Direction 1: finalize running -> resume refused.
        ws, paused, meta = self.pause_initial()
        self.message(meta)

        def check_blocks_resume(task):
            with self.assertRaisesRegex(SystemExit, "仍在執行"):
                self.resume_call(ws, "d1")
            return {"exit_code": 0}

        with patch.dict(os.environ, {cli.NESTED_ENV: ""}), patch("sys.stdout", io.StringIO()), \
                patch.object(cli, "run_task_check", side_effect=check_blocks_resume):
            cli.cmd_finalize(ws, argparse.Namespace(thread=self.thread.name, dispatch_id="d1"))
        self.assertIsNotNone(self.read_meta(meta)["commit_sha"])

        # Direction 2: resume started -> finalize refused.
        ws2, paused2, meta2 = self.pause_initial(dispatch_id="d2")
        self.message(meta2)
        self.resume_call(ws2, "d2")
        with patch.dict(os.environ, {cli.NESTED_ENV: ""}), patch("sys.stdout", io.StringIO()), \
                self.assertRaisesRegex(SystemExit, "正在啟動|仍在執行"):
            cli.cmd_finalize(ws2, argparse.Namespace(thread=self.thread.name, dispatch_id="d2"))
        self.assertIsNone(self.read_meta(meta2)["commit_sha"])

    def test_switch_worker_starts_new_session_with_original_handoff_and_progress_path(self):
        ws, paused, meta = self.pause_initial()
        self.assertEqual(paused["worker"], "codex")
        self.assertEqual(paused["session_id"], "sess-orig-1")

        self.resume_call(ws, "d1", to="antigravity", reason="改派 Antigravity 接續",
                         message="請注意檢查邊界條件")
        resumed = cli.load_state(self.thread)["relay"]
        self.assertEqual(resumed["worker"], "antigravity")
        self.assertIsNone(resumed["session_id"])
        self.assertEqual(resumed["model"], "agy-heavy-m")
        self.assertEqual(resumed["tier"], "heavy")
        self.assertEqual(resumed["resume_switched_from"], "codex")
        self.assertFalse(resumed["resumes"][-1]["session_continued"])

        seen = {}

        def new_party_worker(party, prompt, session, opts, is_work, cwd, timeout, scratch, live=None):
            seen.update(party=party, prompt=prompt, session=session, opts=opts)
            self.message(meta)
            return "完成\n【工作完成】", "agy-new-sess", "ok"

        self.run_worker(ws, "d1", new_party_worker, run_id=resumed["run_id"])
        self.assertEqual(seen["party"], "antigravity")
        self.assertIsNone(seen["session"])
        self.assertIn("【Agora 工作分派】", seen["prompt"])
        self.assertIn("修改 app.py 並補測試", seen["prompt"])
        self.assertIn(str(self.thread / "progress.md"), seen["prompt"])
        self.assertIn("請注意檢查邊界條件", seen["prompt"])
        self.assertEqual(seen["opts"]["model"], "agy-heavy-m")
        self.assertEqual(cli.load_state(self.thread)["relay"]["status"], "done")

    def test_resume_does_not_double_count_or_advance_auto_schedule(self):
        ws, paused, meta = self.pause_initial()
        counts_before = shares.recent_usage(self.root, parties.AI_PARTIES)
        schedule_before = shares.dispatch_state(self.root)["schedule"]
        self.assertEqual(counts_before["codex"]["dispatch_auto"], 1)
        self.assertEqual(counts_before["antigravity"]["dispatch_auto"], 0)

        # Resume twice (same worker, then switched worker) -> still counts only once!
        self.resume_call(ws, "d1")
        cli.update_relay(self.thread, status="stopped")
        self.resume_call(ws, "d1", to="antigravity", reason="第二次續跑")

        counts_after = shares.recent_usage(self.root, parties.AI_PARTIES)
        schedule_after = shares.dispatch_state(self.root)["schedule"]
        self.assertEqual(counts_after["codex"]["dispatch_auto"], 1)
        self.assertEqual(counts_after["codex"]["dispatch_explicit"], 0)
        self.assertEqual(counts_after["antigravity"]["dispatch_auto"], 0)
        self.assertEqual(counts_after["antigravity"]["dispatch_explicit"], 0)
        self.assertEqual(schedule_after, schedule_before)
        self.assertEqual(cli.load_state(self.thread)["relay"]["resume_count"], 2)
        self.assertEqual(len(cli.load_state(self.thread)["relay"]["resumes"]), 2)

    def test_requested_scope_line_in_prompts_status_and_failure_messages(self):
        self.assertIn("需要範圍：a.py, tests/b.py", cli.WORK_PREAMBLE)
        self.assertIn("需要範圍：a.py, tests/b.py", cli.CONTINUE_PROMPT)

        ws, paused, meta = self.pause_initial(allow=["app.py"])
        with open(self.thread / "progress.md", "a", encoding="utf-8") as f:
            f.write("\n## 2026-11-19 12:10:00 · Codex\n範圍不足，暫停。\n需要範圍：other.py, demo/__init__.py\n")

        # Text status shows suggestion without auto-approving.
        out = io.StringIO()
        with patch("sys.stdout", out):
            cli.cmd_status(ws, argparse.Namespace(thread=self.thread.name, wait=None, json=False))
        text = out.getvalue()
        self.assertIn("other.py, demo/__init__.py", text)
        self.assertIn("--resume d1", text)
        self.assertIn('--allow "other.py,demo/__init__.py"', text)
        self.assertIn("不自動核准", text)
        # Manifest allowed_paths must NOT be auto-modified.
        self.assertEqual(self.read_meta(meta)["allowed_paths"], ["app.py"])

        # JSON status exposes requested_scope and resume_hint.
        out_json = io.StringIO()
        with patch("sys.stdout", out_json):
            cli.cmd_status(ws, argparse.Namespace(thread=self.thread.name, wait=None, json=True))
        data = json.loads(out_json.getvalue())
        self.assertEqual(data["requested_scope"], ["other.py", "demo/__init__.py"])
        self.assertIn("--resume d1", data["resume_hint"])

        # Resume failure message also carries the suggestion hint.
        with self.assertRaises(SystemExit) as caught:
            self.resume_call(ws, "d1", allow=["other.py"])  # missing --reason
        self.assertIn("--reason", str(caught.exception))
        self.assertIn("--resume d1", str(caught.exception))
        self.assertIn("other.py,demo/__init__.py", str(caught.exception))

        # After resuming with expanded scope, old 需要範圍 line is no longer shown.
        self.resume_call(ws, "d1", allow=["other.py, demo/__init__.py"], reason="核准範圍")
        cli.update_relay(self.thread, status="stopped")
        out_after = io.StringIO()
        with patch("sys.stdout", out_after):
            cli.cmd_status(ws, argparse.Namespace(thread=self.thread.name, wait=None, json=True))
        after_data = json.loads(out_after.getvalue())
        self.assertEqual(after_data["requested_scope"], [])
        self.assertIsNone(after_data["resume_hint"])

    def test_resume_cli_parser_boundaries_and_file_note(self):
        ws, paused, meta = self.pause_initial()
        note_file = self.root / "extra.md"
        note_file.write_text("來自檔案的補充指示", encoding="utf-8")

        with patch.dict(os.environ, {cli.NESTED_ENV: ""}), patch.object(cli, "cmd_assign") as mock_assign:
            cli.main(["--workspace", str(self.root), "assign", self.thread.name, "--resume", "d1",
                      "--from", "claude", "--allow", "other.py", "--reason", "測試參數", "--file", str(note_file)])
        parsed = mock_assign.call_args.args[1]
        self.assertEqual(parsed.resume, "d1")
        self.assertEqual(parsed.allow, ["other.py"])
        self.assertEqual(parsed.reason, "測試參數")
        self.assertEqual(parsed.file, str(note_file))

        # Incompatible flags refuse cleanly.
        for bad in ({"dispatch_id": "d2"}, {"from_summary": True}, {"session": "s1"},
                    {"tier": "light"}, {"model": "m"}, {"effort": "low"}):
            with self.subTest(bad=bad), self.assertRaises(SystemExit):
                self.resume_call(ws, "d1", extra_args=bad)

        # Nested agent cannot run assign --resume.
        with patch.dict(os.environ, {cli.NESTED_ENV: "1"}), self.assertRaises(SystemExit):
            cli.cmd_assign(ws, argparse.Namespace(thread=self.thread.name, sender="claude", resume="d1"))

        # --file supplemental note is appended to progress.md and prompt.
        self.resume_call(ws, "d1", file=str(note_file))
        self.assertIn("來自檔案的補充指示", (self.thread / "progress.md").read_text(encoding="utf-8"))

    def test_historical_dispatch_resume_and_max_rounds_budget(self):
        ws, paused1, meta1 = self.pause_initial(dispatch_id="old1")
        # Pretend old1 hit MAX_WORK_ROUNDS.
        cli.update_relay(self.thread, rounds=cli.MAX_WORK_ROUNDS, status="max_rounds")
        # Create a newer completed dispatch d2 on the same thread.
        ws, relay2, meta2 = self.assign_initial(dispatch_id="new2")
        cli.update_relay(self.thread, status="done")

        # Resume the older historical dispatch old1!
        self.resume_call(ws, "old1", reason="繼續舊分派")
        resumed = cli.load_state(self.thread)["relay"]
        self.assertEqual(resumed["dispatch_id"], "old1")
        self.assertEqual(resumed["rounds"], cli.MAX_WORK_ROUNDS)

        called = []

        def finish(party, prompt, session, opts, is_work, cwd, timeout, scratch, live=None):
            called.append(True)
            self.message(meta1)
            return "完成\n【工作完成】", session, "ok"

        self.run_worker(ws, "old1", finish, run_id=resumed["run_id"])
        self.assertEqual(len(called), 1)
        self.assertEqual(cli.load_state(self.thread)["relay"]["status"], "done")
        self.assertEqual(cli.load_state(self.thread)["relay"]["rounds"], cli.MAX_WORK_ROUNDS + 1)

    def test_extend_scope_unit_behavior(self):
        meta = self.task("ext", ["app.py"])
        meta["check_result"] = {"exit_code": 0}
        meta["checked_tree"] = "abc"
        meta["commit_status"] = "checked"
        with self.assertRaisesRegex(worktree.WorktreeError, "--reason"):
            worktree.extend_scope(meta, ["other.py"], "", "claude")
        with self.assertRaisesRegex(worktree.WorktreeError, "機密檔規則"):
            worktree.extend_scope(meta, [".env"], "理由", "claude")
        self.assertEqual(meta["allowed_paths"], ["app.py"])
        added = worktree.extend_scope(meta, ["app.py, other.py"], "需要修改 other.py", "claude", at="2026-11-19 12:30:00")
        self.assertEqual(added, ["other.py"])
        self.assertEqual(meta["allowed_paths"], ["app.py", "other.py"])
        self.assertIsNone(meta["check_result"])
        self.assertIsNone(meta["checked_tree"])
        self.assertEqual(meta["commit_status"], "pending")
        self.assertEqual(meta["scope_extensions"], [{
            "paths": ["other.py"],
            "reason": "需要修改 other.py",
            "at": "2026-11-19 12:30:00",
            "by": "claude",
        }])


if __name__ == "__main__":
    unittest.main()
