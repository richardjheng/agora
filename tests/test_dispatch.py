"""派工交易、權重與中斷恢復；隔離專案，不呼叫 AI 或網路。"""

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

from agoralib import cli, completions, config, parties, quota, quota_hook, routing, shares


def make_thread(root, name):
    ws = cli.Workspace(Path(root))
    thread = ws.threads / name
    thread.mkdir(parents=True)
    cli.save_state(thread, {"topic": name, "parties": {p: cli.new_party() for p in parties.AI_PARTIES}, "messages": []})
    (thread / "transcript.md").write_text("", encoding="utf-8")
    return ws, thread


def args_for(name, **kw):
    return argparse.Namespace(**{"thread": name, "sender": "claude", "to": None, "message": "修改核心",
                                 "file": None, "session": None, "from_summary": False, "yes": False,
                                 "tier": None, "model": None, "effort": None, "subagents": None,
                                 "trigger": None, **kw})


def assign(ws, args, crash=None, fail_spawn=False, usages=None, output=None):
    original_update = cli.update_relay

    def update(thread, **fields):
        if fields.get("dispatch_created") and crash == "before_marker":
            os._exit(73)
        result = original_update(thread, **fields)
        if fields.get("dispatch_created") and crash == "after_marker":
            os._exit(73)
        return result

    with patch.dict(os.environ, {cli.NESTED_ENV: ""}), patch("sys.stdout", output if output is not None else io.StringIO()), \
            patch.object(cli, "party_usage", side_effect=lambda p, m=None: usages.get(p) if usages is not None
                         else {"remaining_pct": 80, "updated_at": time.time()}), \
            patch.object(cli.quota, "blocked", return_value=None), \
            patch.object(cli.models, "CACHE", ws.root / "models.json"), \
            patch.object(cli.snapshot, "create", return_value={}), \
            patch.object(cli.worktree, "prepare", side_effect=lambda root, key, allowed, thread: {
                "dispatch_id": key, "source_dirty": False, "base_sha": "base", "branch": f"agora/{key}",
                "allowed_paths": ["**"]}), patch.object(cli.worktree, "save"), \
            patch.object(cli.jobs, "spawn", side_effect=OSError("測試啟動失敗") if fail_spawn else None,
                         return_value=99999999) as spawn, \
            patch.object(cli.jobs, "pid_started", return_value=None), \
            patch.object(cli, "update_relay", side_effect=update):
        cli.cmd_assign(ws, args)
        return spawn.call_count


def process_assign(root, name, key, crash=None):
    ws = cli.Workspace(Path(root))
    assign(ws, args_for(name, dispatch_id=key), crash=crash)


class TestDispatch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        config.update_parties(self.root, shares={"claude": "0", "antigravity": "5", "codex": "10"})

    def make(self, name="t1"):
        return make_thread(self.root, name)

    def counts(self):
        return shares.recent_usage(self.root, parties.AI_PARTIES)

    def test_persisted_5_to_10_every_three_successful_dispatches(self):
        selected = []
        for i in range(12):
            ws, thread = self.make(f"t{i}")
            assign(ws, args_for(thread.name))
            selected.append(cli.load_state(thread)["relay"]["worker"])
        for i in range(10):
            self.assertEqual(selected[i:i + 3].count("codex"), 2)
        self.assertEqual(self.counts()["codex"]["dispatch_auto"], 8)
        self.assertEqual(self.counts()["antigravity"]["dispatch_auto"], 4)

    def test_same_id_retry_is_noop_but_new_id_same_work_counts(self):
        ws, thread = self.make()
        args = args_for("t1", dispatch_id="work-1")
        self.assertEqual(assign(ws, args), 1)
        schedule = shares.dispatch_state(self.root)["schedule"]
        self.assertEqual(assign(ws, args), 0)
        self.assertEqual(shares.dispatch_state(self.root)["schedule"], schedule)
        with self.assertRaisesRegex(SystemExit, "不同工作單"):
            assign(ws, args_for("t1", dispatch_id="work-1", message="其他工作"))
        cli.update_relay(thread, status="done")
        assign(ws, args_for("t1", dispatch_id="work-2"))
        self.assertEqual(sum(u["dispatch_auto"] for u in self.counts().values()), 2)

    def test_spawn_failure_does_not_count_or_advance_and_can_retry(self):
        ws, thread = self.make()
        args = args_for("t1", dispatch_id="retry")
        with self.assertRaisesRegex(SystemExit, "無法啟動"):
            assign(ws, args, fail_spawn=True)
        self.assertEqual(shares.dispatch_state(self.root)["dispatches"], {})
        self.assertEqual(cli.load_state(thread)["relay"]["status"], "failed")
        assign(ws, args)
        self.assertEqual(cli.load_state(thread)["relay"]["worker"], "codex")
        self.assertEqual(self.counts()["codex"]["dispatch_auto"], 1)

    def test_new_assignment_archives_unreported_previous_result(self):
        for status in ("done", "running"):
            ws, thread = self.make("old-" + status)
            state = cli.load_state(thread)
            state["relay"] = {"status": status, "dispatch_id": "previous-" + status,
                              "worker": "codex", "pid": 99999999, "branch": "agora/previous"}
            cli.save_state(thread, state)
            assign(ws, args_for(thread.name, dispatch_id="next-" + status))
        output = []
        completions.deliver(self.root, output.extend)
        previous = {item["dispatch_id"]: item for item in output if item.get("dispatch_id", "").startswith("previous-")}
        self.assertEqual(previous["previous-done"]["status"], "done")
        self.assertEqual(previous["previous-running"]["status"], "interrupted")

    def test_explicit_dispatch_does_not_advance_automatic_schedule(self):
        ws, _ = self.make("explicit")
        with self.assertRaisesRegex(SystemExit, "--reason"):
            assign(ws, args_for("explicit", to="antigravity"))
        assign(ws, args_for("explicit", to="antigravity", reason="專長契合"))
        ws, thread = self.make("automatic")
        assign(ws, args_for("automatic"))
        self.assertEqual(cli.load_state(thread)["relay"]["worker"], "codex")
        self.assertEqual(self.counts()["antigravity"]["dispatch_explicit"], 1)

    def test_zero_share_requires_user_override_and_reason_and_records_both(self):
        ws, thread = self.make()
        for extra in ({}, {"reason": "專長"}, {"user_override": True}):
            with self.assertRaisesRegex(SystemExit, "使用者明確"):
                assign(ws, args_for("t1", to="claude", **extra))
        assign(ws, args_for("t1", to="claude", user_override=True, reason="Richard 明確指定 Claude 實作"))
        relay = cli.load_state(thread)["relay"]
        entry = shares.dispatch_state(self.root)["dispatches"][relay["dispatch_id"]]
        self.assertTrue(entry["user_override"])
        self.assertIn("Richard", entry["reason"])
        self.assertEqual(self.counts()["claude"]["dispatch_explicit"], 1)
        self.assertEqual(self.counts()["claude"]["dispatch_auto"], 0)

    def test_eligibility_boundaries_and_schedule_reset(self):
        weights = {"antigravity": 5, "codex": 10}
        usage = {p: {"remaining_pct": quota.THRESHOLD_PCT, "updated_at": time.time()} for p in weights}
        schedule = {}
        self.assertEqual(shares.pick(weights, weights, schedule, usage), "codex")
        low = {**usage["codex"], "remaining_pct": quota.THRESHOLD_PCT - 1}
        self.assertEqual(shares.pick(weights, weights, schedule, {**usage, "codex": low}), "antigravity")
        self.assertEqual(shares.pick(weights, weights, schedule, usage), "codex")  # 恢復資格不追補
        self.assertEqual(shares.pick(weights, {"antigravity": 20, "codex": 10}, schedule, usage), "antigravity")
        self.assertIsNone(shares.pick(weights, weights, schedule, {p: None for p in weights}))
        self.assertEqual(shares.pick(weights, weights, schedule, usage), "codex")
        self.assertIsNone(shares.pick(weights, dict.fromkeys(weights, 0), schedule, usage))
        self.assertIsNone(shares.pick(["codex"], weights, {}, {"codex": {**usage["codex"], "blocked": {"reason": "帳務"}}}))

    def test_stale_quota_assigns_automatically_and_explicitly_with_auditable_warning(self):
        config.update_parties(self.root, shares={"codex": "0"})
        now = time.time()
        for explicit in (False, True):
            for reset in (False, True):
                with self.subTest(explicit=explicit, reset=reset):
                    name = f"stale-{explicit}-{reset}"
                    ws, thread = self.make(name)
                    usage = {"remaining_pct": 1 if reset else quota.THRESHOLD_PCT,
                             "updated_at": now - 12 * 60, "resets_at": now - 1 if reset else now + 3600}
                    extra = {"to": "antigravity", "reason": "使用者指定"} if explicit else {}
                    out = io.StringIO()
                    with patch.object(shares.time, "time", return_value=now):
                        self.assertEqual(assign(ws, args_for(name, **extra),
                                                usages={"antigravity": usage}, output=out), 1)
                    relay = cli.load_state(thread)["relay"]
                    self.assertEqual(relay["worker"], "antigravity")
                    entry = shares.dispatch_state(self.root)["dispatches"][relay["dispatch_id"]]
                    self.assertEqual(entry["selection"], "explicit" if explicit else "auto")
                    info = entry["candidates"]["antigravity"]
                    self.assertTrue(info["is_stale"])
                    self.assertEqual(info["age_minutes"], 12)
                    self.assertEqual(info["updated_at"], usage["updated_at"])
                    self.assertEqual(info["remaining_pct"], usage["remaining_pct"])
                    self.assertEqual(info["effective_remaining_pct"], 100 if reset else usage["remaining_pct"])
                    self.assertNotIn("excluded", info)
                    self.assertIn("額度資料過期（12 分鐘前）", info["quota_note"])
                    self.assertIn(info["quota_note"], out.getvalue())
                    if reset:
                        self.assertIn("視為已重置", out.getvalue())

    def test_unknown_low_and_blocked_have_same_eligibility_and_specific_errors(self):
        config.update_parties(self.root, shares={"codex": "0"})
        now = time.time()
        stale = {"remaining_pct": quota.THRESHOLD_PCT - 1, "updated_at": now - 12 * 60,
                 "resets_at": now + 3600}
        cases = [(stale, "額度不足", ("額度未知", "被封鎖")),
                 (None, "額度未知", ("額度不足", "被封鎖")),
                 ({}, "額度未知", ("額度不足", "被封鎖")),
                 ({"remaining_pct": None}, "額度未知", ("額度不足", "被封鎖")),
                 ({"updated_at": now - 720, "resets_at": now - 1}, "額度未知", ("額度不足", "被封鎖")),
                 ({**stale, "resets_at": now - 1, "blocked": {"reason": "訂閱失效"}},
                  "被封鎖（訂閱失效）", ("額度未知", "額度不足"))]
        for explicit in (False, True):
            for i, (usage, reason, absent) in enumerate(cases):
                with self.subTest(explicit=explicit, usage=usage):
                    name = f"excluded-{explicit}-{i}"
                    ws, _ = self.make(name)
                    extra = {"to": "antigravity", "reason": "使用者指定"} if explicit else {}
                    with self.assertRaises(SystemExit) as caught:
                        assign(ws, args_for(name, **extra), usages={"antigravity": usage})
                    text = str(caught.exception.code)
                    self.assertIn(reason, text)
                    for other in absent:
                        self.assertNotIn(other, text)
                    if usage and usage.get("remaining_pct") is not None:
                        self.assertIn("額度資料過期（12 分鐘前）", text)
        self.assertEqual(shares.dispatch_state(self.root)["dispatches"], {})

    def test_elapsed_reset_keeps_main_model_and_worker_does_not_wait(self):
        now = time.time()
        usage = {"remaining_pct": 1, "resets_at": now - 1, "updated_at": now - 720}
        plan = {"model": "main", "source": "測試", "fallback": {"model": "fallback"}}
        with patch.object(cli, "party_usage", return_value=usage) as query:
            chosen, _ = cli.choose_model("antigravity", plan)
        self.assertEqual(chosen["model"], "main")
        query.assert_called_once_with("antigravity", "main")
        ws, thread = self.make()
        assign(ws, args_for("t1"))
        relay = cli.load_state(thread)["relay"]
        with patch.object(cli, "party_usage", return_value=usage), \
                patch.object(cli, "sleep_until") as sleep, \
                patch.object(cli, "call_party", return_value=("完成\n【工作完成】", "s", "ok")) as call, \
                patch.object(cli, "git_status", return_value=""), patch("sys.stdout", io.StringIO()):
            cli.cmd_work(ws, argparse.Namespace(thread="t1", dispatch_id=relay["dispatch_id"]))
        sleep.assert_not_called()
        self.assertEqual(call.call_count, 1)
        self.assertEqual(cli.load_state(thread)["relay"]["status"], "done")

    def test_all_zero_and_low_exit_without_spawn_or_wait(self):
        ws, _ = self.make()
        config.update_parties(self.root, shares={"antigravity": "0", "codex": "0"})
        with self.assertRaisesRegex(SystemExit, "沒有可自動分派"):
            assign(ws, args_for("t1"))
        config.update_parties(self.root, shares={"codex": "10"})
        with patch.object(cli, "party_usage", return_value={"remaining_pct": 1, "resets_at": time.time() + 60}), \
                self.assertRaisesRegex(SystemExit, "不等待重置"):
            cli.pick_worker(ws, "claude", ws.enabled(), cli.load_state(ws.threads / "t1"))
        self.assertEqual(shares.dispatch_state(self.root)["dispatches"], {})

    def test_usage_recovers_missing_log_and_deduplicates_projection(self):
        ws, _ = self.make()
        assign(ws, args_for("t1"))
        shares.log_path(self.root).write_text("", encoding="utf-8")
        self.assertEqual(self.counts()["codex"]["dispatch_auto"], 1)
        with shares.dispatch_lock(self.root):
            data = shares.recover_dispatch(self.root)
            shares.recover_dispatch(self.root)
        entry = next(iter(data["dispatches"].values()))
        shares.append(self.root, entry)
        self.assertEqual(self.counts()["codex"]["dispatch_auto"], 1)

    def run_process(self, name, key, crash=None):
        p = multiprocessing.get_context("spawn").Process(target=process_assign, args=(str(self.root), name, key, crash))
        p.start()
        p.join(30)
        if p.is_alive():
            p.terminate()
            p.join(5)
            self.fail("測試派工程式未結束")
        self.assertEqual(p.exitcode, 73 if crash else 0)

    def test_process_restart_before_and_after_creation_marker(self):
        for point in ("before_marker", "after_marker"):
            ws, thread = self.make(point)
            self.run_process(point, point, crash=point)
            with shares.dispatch_lock(self.root):
                cli.recover_assignments(ws)
            before = len(shares.dispatch_state(self.root)["dispatches"])
            self.run_process(point, point)
            after = len(shares.dispatch_state(self.root)["dispatches"])
            self.assertEqual(after - before, 1 if point == "before_marker" else 0)
        self.assertEqual(sum(u["dispatch_auto"] for u in self.counts().values()), 2)

    def test_cross_thread_process_contention_preserves_proportions(self):
        ctx = multiprocessing.get_context("spawn")
        procs = []
        for i in range(6):
            name = f"parallel{i}"
            self.make(name)
            procs.append(ctx.Process(target=process_assign, args=(str(self.root), name, name)))
        try:
            for p in procs:
                p.start()
            for p in procs:
                p.join(30)
                self.assertEqual(p.exitcode, 0)
        finally:
            for p in procs:
                if p.is_alive():
                    p.terminate()
                    p.join(5)
        self.assertEqual(self.counts()["codex"]["dispatch_auto"], 4)
        self.assertEqual(self.counts()["antigravity"]["dispatch_auto"], 2)

    def test_uncommitted_or_replaced_worker_cannot_execute(self):
        ws, thread = self.make()
        assign(ws, args_for("t1", dispatch_id="current"))
        with patch.object(cli, "call_party") as call:
            cli.cmd_work(ws, argparse.Namespace(thread="t1", dispatch_id="old"))
        call.assert_not_called()
        cli.update_relay(thread, dispatch_created=False)
        with patch.object(cli, "call_party") as call:
            cli.cmd_work(ws, argparse.Namespace(thread="t1", dispatch_id="current"))
        call.assert_not_called()

    def test_worker_prompt_contains_zero_role_and_explicit_override(self):
        ws, thread = self.make()
        assign(ws, args_for("t1", to="claude", user_override=True, reason="Richard 指定實作"))
        relay = cli.load_state(thread)["relay"]
        with patch.object(cli, "party_usage", return_value={"remaining_pct": 80}), \
                patch.object(cli, "call_party", return_value=("完成\n【工作完成】", "s", "ok")) as call, \
                patch.object(cli, "git_status", return_value=""), patch("sys.stdout", io.StringIO()):
            cli.cmd_work(ws, argparse.Namespace(thread="t1", dispatch_id=relay["dispatch_id"]))
        self.assertEqual(call.call_count, 1)
        self.assertIn(routing.ZERO_SHARE_ROLE, call.call_args.args[1])
        self.assertIn("本工作已記錄使用者明確指定你實作：Richard 指定實作", call.call_args.args[1])
        self.assertEqual(cli.load_state(thread)["relay"]["status"], "done")

    def test_worker_exits_if_quota_changes_or_weight_becomes_zero(self):
        for name, zero in (("quota_changed", False), ("weight_changed", True)):
            ws, thread = self.make(name)
            assign(ws, args_for(name))
            relay = cli.load_state(thread)["relay"]
            if zero:
                config.update_parties(self.root, shares={relay["worker"]: "0"})
            with patch.object(cli, "party_usage", return_value={"remaining_pct": 1}), \
                    patch.object(cli, "sleep_until") as sleep, patch.object(cli, "call_party") as call:
                cli.cmd_work(ws, argparse.Namespace(thread=name, dispatch_id=relay["dispatch_id"]))
            call.assert_not_called()
            sleep.assert_not_called()
            self.assertEqual(cli.load_state(thread)["relay"]["status"], "stopped")
    def test_zero_role_in_route_and_hook_for_single_or_multiple_enabled(self):
        ws, _ = self.make()
        for enabled in (["claude"], ["claude", "codex"]):
            text = routing.instruction("claude", enabled, config.shares(self.root))
            self.assertIn(routing.ZERO_SHARE_ROLE, text)
            self.assertNotIn("直接自己處理", text)
        with patch.object(quota_hook, "project_root", return_value=self.root):
            self.assertIn(routing.ZERO_SHARE_ROLE, quota_hook.routing_line("claude"))
        out = io.StringIO()
        with patch("sys.stdout", out):
            cli.cmd_route(ws, argparse.Namespace(me="claude", json=True))
        self.assertIn(routing.ZERO_SHARE_ROLE, json.loads(out.getvalue())["instruction"])
