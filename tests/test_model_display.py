"""模型標示規則、等級與每輪回報測試"""

import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agoralib import cli, config, parties, quota  # noqa: E402

# 測試不能動到使用者真正的「無法使用」標記（~/.agora/blocked.json）
_TEST_HOME = Path(tempfile.mkdtemp())
quota.BLOCKED_FILE = _TEST_HOME / "blocked.json"
cli.INSTALL_WRITES = _TEST_HOME / "install_writes.jsonl"
os.environ.pop(parties.NESTED_ENV, None)
os.environ.pop(parties.LOCKED_ENV, None)


def state_with(party_list, messages=()):
    return {"topic": "t", "parties": {p: {"session_id": None, "seen": 0, "model": None} for p in party_list},
            "messages": [{"speaker": s, "text": t, "time": "now", "via_cli": v} for s, t, v in messages]}


def pct(value, reset_in=3600):
    return {"remaining_pct": value, "resets_at": time.time() + reset_in, "updated_at": time.time()}


class TestModelLabel(unittest.TestCase):
    def test_model_label_cases(self):
        cases = [
            ("opus", "opus", "模型 opus"),
            (None, "gpt-6-astra", "模型 gpt-6-astra（CLI 預設）"),
            ("opus", "claude-opus-5-5", "請求 opus → 回報 claude-opus-5-5"),
            ("gemini-4-argon-high", None, "請求 gemini-4-argon-high（未回報）"),
            (None, None, "CLI 預設（未回報）"),
            ("", "", "CLI 預設（未回報）"),
        ]
        for requested, reported, expected in cases:
            with self.subTest(requested=requested, reported=reported):
                self.assertEqual(cli.model_label(requested, reported), expected)


class TestPlanModelLevel(unittest.TestCase):
    def setUp(self):
        self._cache = tempfile.TemporaryDirectory()
        patcher = patch.object(cli.models, "CACHE", Path(self._cache.name) / "models.json")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._cache.cleanup)

    def test_plan_model_level(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = cli.Workspace(Path(tmp))
            config.update_parties(ws.root, tiers={"claude.heavy": "opus:max", "claude.standard": "sonnet"})
            s = state_with(["claude"])

            # 給 --tier heavy 且有設定 → "heavy"
            plan_heavy = cli.plan_model(ws, s, "claude", "heavy", None, None)
            self.assertEqual(plan_heavy.get("level"), "heavy")

            # 沒給 --tier 但套用標準等級設定 → "standard"
            plan_standard = cli.plan_model(ws, s, "claude", None, None, None)
            self.assertEqual(plan_standard.get("level"), "standard")

        with tempfile.TemporaryDirectory() as tmp2:
            ws_empty = cli.Workspace(Path(tmp2))
            s_empty = state_with(["claude"])

            # 沒有任何等級設定 → None
            plan_none = cli.plan_model(ws_empty, s_empty, "claude", None, None, None)
            self.assertIsNone(plan_none.get("level"))

            # 給 --tier heavy 但沒有任何等級設定 → None
            plan_heavy_unset = cli.plan_model(ws_empty, s_empty, "claude", "heavy", None, None)
            self.assertIsNone(plan_heavy_unset.get("level"))


class TestWorkRoundReporting(unittest.TestCase):
    def setUp(self):
        self._cache = tempfile.TemporaryDirectory()
        patcher = patch.object(cli.models, "CACHE", Path(self._cache.name) / "models.json")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._cache.cleanup)

    def test_work_round_reporting_and_headings(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = cli.Workspace(Path(tmp))
            thread = ws.threads / "t1"
            thread.mkdir(parents=True)
            state = state_with(["claude", "antigravity"])
            state["relay"] = {
                "worker": "antigravity",
                "from": "claude",
                "handoff": "測試模型標示",
                "rounds": 0,
                "status": "starting",
                "frozen": True,
                "tier": "heavy",
                "model": "gemini-4-argon-high",
                "effort": None,
                "subagents": None,
            }
            cli.save_state(thread, state)
            (thread / "transcript.md").write_text("", encoding="utf-8")

            call_count = 0

            def fake_agy(prompt, session_id, opts, work, workspace, timeout, scratch, live=None):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    opts["_stats"]["model"] = "gemini-4-argon-high"
                    return "完成第一輪進度", "s1", "1s"
                else:
                    opts["_stats"]["model"] = None
                    return "完成第二輪進度", "s1", "1s"

            with patch.dict(cli.CALLERS, {"antigravity": fake_agy}), \
                 patch.object(cli, "party_usage", return_value=pct(80)), \
                 patch.object(cli, "git_status", return_value=""), \
                 patch.object(quota, "refresh"), \
                 patch.object(cli, "MAX_WORK_ROUNDS", 2), \
                 patch("sys.stdout", io.StringIO()):
                cli.cmd_work(ws, type("Args", (), {"thread": "t1"})())

            state = cli.load_state(thread)
            relay = state["relay"]
            transcript = (thread / "transcript.md").read_text(encoding="utf-8")

            # 驗證跑了兩輪
            self.assertEqual(relay["rounds"], 2)
            # 第一輪回報模型、第二輪不回報 → relay.last_model_reported 第二輪後為 None
            self.assertIsNone(relay["last_model_reported"])
            self.assertEqual(relay["last_model_requested"], "gemini-4-argon-high")
            self.assertIsNone(relay["last_effort_requested"])
            # 第一輪有回報，保存在 model_reported
            self.assertEqual(relay.get("model_reported"), "gemini-4-argon-high")
            # 逐字稿第二輪標題含「（未回報）」
            self.assertIn("（未回報）", transcript)
            self.assertIn("請求 gemini-4-argon-high（未回報）", transcript)
            # messages[].model_requested 有值
            self.assertEqual(len(state["messages"]), 2)
            self.assertEqual(state["messages"][0]["model_requested"], "gemini-4-argon-high")
            self.assertEqual(state["messages"][1]["model_requested"], "gemini-4-argon-high")

    def make_thread(self, tmp, **fields):
        ws = cli.Workspace(Path(tmp))
        thread = ws.threads / "t1"
        thread.mkdir(parents=True)
        state = state_with(["claude", "antigravity"])
        state["relay"] = {"worker": "antigravity", "from": "claude", "handoff": "模型標示",
                          "rounds": 0, "status": "starting", "frozen": True, "model": None,
                          "effort": "high", **fields}
        cli.save_state(thread, state)
        (thread / "transcript.md").write_text("", encoding="utf-8")
        return ws, thread

    def run_work(self, ws, caller, rounds=2):
        with patch.dict(cli.CALLERS, {"antigravity": caller}), \
             patch.object(cli, "party_usage", return_value=pct(80)), \
             patch.object(cli, "git_status", return_value=""), \
             patch.object(quota, "refresh"), \
             patch.object(cli, "MAX_WORK_ROUNDS", rounds), \
             patch("sys.stdout", io.StringIO()):
            cli.cmd_work(ws, SimpleNamespace(thread="t1"))

    def status(self, ws, as_json=False):
        out = io.StringIO()
        with patch.object(cli, "relay_alive", return_value=True), patch("sys.stdout", out):
            cli.cmd_status(ws, SimpleNamespace(thread="t1", wait=0, json=as_json))
        return json.loads(out.getvalue()) if as_json else out.getvalue()

    def activity(self, ws):
        out = io.StringIO()
        with patch.object(cli, "relay_alive", return_value=True), \
             patch.object(cli.jobs, "active", return_value=None), patch("sys.stdout", out):
            cli.cmd_activity(ws, SimpleNamespace(json=True))
        return json.loads(out.getvalue())[0]

    def test_default_model_freeze_preserves_completed_request_in_status_and_activity(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws, thread = self.make_thread(tmp)
            requests = []

            def caller(prompt, session_id, opts, *args, **kwargs):
                requests.append(opts.get("model"))
                if len(requests) == 1:
                    self.assertIn("最近完成一輪：尚無完成的輪次", self.status(ws))
                else:
                    # 第二輪執行中：目前請求已凍結成 X，歷史仍是第一輪的 CLI 預設。
                    state = cli.load_state(thread)
                    message = state["messages"][-1]
                    label = cli.model_label(message["model_requested"], message["stats"]["model"])
                    self.assertEqual(label, "模型 X（CLI 預設）")
                    self.assertIn(label, (thread / "transcript.md").read_text(encoding="utf-8"))
                    self.assertIn(f"最近完成一輪：{label}", self.status(ws))
                    self.assertIn("目前請求：X（推理強度 high）", self.status(ws))
                    for data in (self.status(ws, as_json=True), self.activity(ws)):
                        self.assertEqual(data["model"], "X")
                        self.assertIn("last_model_requested", data)
                        self.assertIsNone(data["last_model_requested"])
                        self.assertEqual(data["last_effort_requested"], "high")
                        self.assertEqual(data["last_model_reported"], "X")
                opts["_stats"]["model"] = "X"
                return "完成一步", "s1", "1s"

            self.run_work(ws, caller)
            self.assertEqual(requests, [None, "X"])
            state = cli.load_state(thread)
            self.assertEqual(state["relay"]["last_model_requested"], "X")
            self.assertEqual(state["relay"]["last_model_reported"], "X")
            message = state["messages"][-1]
            label = cli.model_label(message["model_requested"], message["stats"]["model"])
            self.assertEqual(label, "模型 X")
            self.assertIn(f"最近完成一輪：{label}", self.status(ws))
            self.assertIn(f"接手第 2 輪, {label}", (thread / "transcript.md").read_text(encoding="utf-8"))

    def test_failed_call_updates_request_and_clears_previous_report(self):
        failures = [parties.CallError("呼叫失敗"), subprocess.TimeoutExpired("fake", 1),
                    FileNotFoundError("找不到 CLI")]
        for failure in failures:
            for reported in (None, "Y"):
                with self.subTest(failure=type(failure).__name__, reported=reported), \
                     tempfile.TemporaryDirectory() as tmp:
                    ws, thread = self.make_thread(tmp, model="B", effort=None, rounds=1,
                                                 last_model_requested="A", last_effort_requested="low",
                                                 last_model_reported="A")

                    def caller(prompt, session_id, opts, *args, **kwargs):
                        opts["_stats"]["model"] = reported
                        raise failure

                    self.run_work(ws, caller)
                    relay = cli.load_state(thread)["relay"]
                    self.assertEqual(relay["status"], "failed")
                    self.assertEqual(relay["last_model_requested"], "B")
                    self.assertIsNone(relay["last_effort_requested"])
                    self.assertEqual(relay["last_model_reported"], reported)

    def test_legacy_relay_does_not_reconstruct_history_from_current_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws, thread = self.make_thread(tmp, model="current", model_reported="first", rounds=1)
            text = self.status(ws)
            self.assertIn("目前請求：current（推理強度 high）", text)
            self.assertIn("最近完成一輪：CLI 預設（未回報）", text)
            for data in (self.status(ws, as_json=True), self.activity(ws)):
                for key in ("last_model_requested", "last_effort_requested", "last_model_reported"):
                    self.assertIn(key, data)
                    self.assertIsNone(data[key])

    def test_status_separates_current_effort_from_completed_effort(self):
        text = cli.relay_model_line({"model": "B", "effort": "high", "rounds": 1,
                                     "last_model_requested": "A", "last_effort_requested": "low",
                                     "last_model_reported": "C"})
        self.assertIn("目前請求：B（推理強度 high）", text)
        self.assertIn("最近完成一輪：請求 A → 回報 C（推理強度 low）", text)


if __name__ == "__main__":
    unittest.main()
