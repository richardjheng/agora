"""CLI 串流統計的離線契約測試；不啟動 AI、不查真實 session 或寫額度快取。"""

import subprocess
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from agoralib import parties


class TestCallStats(unittest.TestCase):
    def invoke(self, party, events, stats=None, error=None, inspect=None, replay_only=False, live=None, **opts):
        if stats is not None:
            opts["_stats"] = stats

        def run(cmd, workspace, stdin, timeout, on_event, extra_env):
            if not replay_only:
                for event in events:
                    on_event(event)
                    if inspect:
                        inspect(event)
            if error:
                raise error
            return events, ""

        with patch.object(parties, "_run", run), \
                patch.object(parties.quota, "find_claude", return_value="claude"), \
                patch.object(parties.quota, "find_codex", return_value="codex"), \
                patch.object(parties, "find_agy", return_value="agy"), \
                patch.object(parties.quota, "update_codex_snapshot_from_session"), \
                patch.object(parties.quota, "write_claude_snapshot"):
            return parties.CALLERS[party]("test", None, opts, False, Path("."), 60, Path("."), live)

    @staticmethod
    def success(party, usage=None):
        if party == "claude":
            return [{"type": "result", "result": "OK", "session_id": "s", "usage": usage or {}}]
        if party == "antigravity":
            return [{"event": "result", "result": {"status": "SUCCESS", "response": "OK",
                                                     "conversation_id": "s", "usage": usage or {}}}]
        return [{"type": "thread.started", "thread_id": "s"},
                {"type": "item.completed", "item": {"type": "agent_message", "text": "OK"}},
                {"type": "turn.completed", "usage": usage or {}}]

    @staticmethod
    def partial(party):
        usage = {"input_tokens": 10, "output_tokens": 3, "cached_tokens": 2}
        if party == "claude":
            return {"type": "assistant", "message": {"id": "m", "model": "actual", "usage": usage,
                                                        "content": []}}
        if party == "antigravity":
            return {"event": "step_update", "step_update": {"step_type": "agent_response", "model": "actual",
                                                              "usage": usage}}
        return {"type": "turn.started", "model": "actual", "usage": usage}

    def test_success_cache_semantics_and_unchanged_return(self):
        cases = [
            ("claude", {"input_tokens": 10, "output_tokens": 3, "cache_read_input_tokens": 20,
                        "cache_creation_input_tokens": 5}, 38, 25, False, "claude:result.usage", "0s, $0.000"),
            ("codex", {"input_tokens": 30, "output_tokens": 3, "cached_input_tokens": 20},
             33, 20, True, "codex:turn.completed", "30+3 tokens"),
            ("antigravity", {"input_tokens": 30, "output_tokens": 3, "cached_tokens": 20, "total_tokens": 33},
             33, 20, None, "antigravity:result.usage", "0s, 33 tokens"),
        ]
        for party, usage, total, cached, included, source, meta in cases:
            with self.subTest(party=party):
                stats = {}
                result = self.invoke(party, self.success(party, usage), stats)
                self.assertEqual(result, ("OK", "s", meta))
                self.assertEqual(stats["total_tokens"], total)
                self.assertEqual(stats["cached_tokens"], cached)
                self.assertIs(stats["cached_in_input"], included)
                self.assertIsNone(stats["includes_subagents"])
                self.assertEqual(stats["usage_source"], source)
                self.assertTrue(stats["complete"])

    def test_stats_are_updated_before_run_returns_or_raises(self):
        for party in parties.AI_PARTIES:
            for error in (subprocess.TimeoutExpired("fake", 60), OSError("broken pipe"), parties.CallError("bad")):
                with self.subTest(party=party, error=type(error).__name__):
                    stats = {}

                    def inspect(event):
                        self.assertEqual(stats["input_tokens"], 10)
                        self.assertEqual(stats["model"], "actual")
                        self.assertFalse(stats["complete"])

                    with self.assertRaises(type(error)):
                        self.invoke(party, [self.partial(party)], stats, error=error, inspect=inspect)
                    self.assertEqual(stats["output_tokens"], 3)
                    self.assertFalse(stats["complete"])

    def test_error_results_preserve_usage_and_denials(self):
        usage = {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12}
        cases = [
            ("claude", [{"type": "result", "is_error": True, "usage": usage,
                         "permission_denials": [{"tool_name": "Bash", "tool_input": {"command": "bad"}}]}], True),
            ("antigravity", [{"event": "result", "result": {"status": "ERROR", "usage": usage,
                              "denied_actions": [{"action": "read_url"}]}}], True),
            ("codex", [{"type": "turn.failed", "usage": usage, "error": {"message": "failed"}}], True),
        ]
        for party, events, complete in cases:
            with self.subTest(party=party):
                stats = {}
                with self.assertRaises(parties.CallError) as caught:
                    self.invoke(party, events, stats)
                self.assertEqual(stats["total_tokens"], 12)
                self.assertIs(stats["complete"], complete)
                if party != "codex":
                    self.assertTrue(caught.exception.denied)

    def test_unknown_usage_and_requested_model_are_not_invented(self):
        for party in parties.AI_PARTIES:
            with self.subTest(party=party):
                stats = {}
                self.invoke(party, self.success(party), stats, model="requested-alias")
                for key in ("model", "input_tokens", "output_tokens", "cached_tokens", "total_tokens",
                            "cached_in_input", "includes_subagents", "usage_source"):
                    self.assertIsNone(stats[key], key)
                self.assertFalse(stats["complete"])

    def test_without_stats_keeps_opts_and_behavior(self):
        for party in parties.AI_PARTIES:
            with self.subTest(party=party):
                opts = {}
                # Also directly check the collector never inserts the opt-in key.
                parties._CallStats(opts, party).event(self.partial(party))
                self.assertEqual(opts, {})
                self.assertEqual(self.invoke(party, self.success(party))[0], "OK")

    def test_final_usage_replaces_partial_and_supports_nonstream_fallback(self):
        for party in parties.AI_PARTIES:
            for replay_only in (False, True):
                with self.subTest(party=party, replay_only=replay_only):
                    stats = {}
                    events = [self.partial(party)] + self.success(party, {"total_tokens": 40})
                    self.invoke(party, events, stats, replay_only=replay_only)
                    self.assertEqual(stats["total_tokens"], 40)
                    self.assertIsNone(stats["input_tokens"])
                    self.assertEqual(stats["model"], "actual")
                    self.assertTrue(stats["complete"])

    def test_final_without_usage_does_not_certify_partial_usage(self):
        for party in parties.AI_PARTIES:
            with self.subTest(party=party):
                stats = {}
                self.invoke(party, [self.partial(party)] + self.success(party), stats)
                self.assertEqual(stats["input_tokens"], 10)
                self.assertFalse(stats["complete"])

    def test_antigravity_unknown_cache_convention_does_not_guess_total(self):
        stats = {}
        self.invoke("antigravity", self.success("antigravity", {"input_tokens": 10, "output_tokens": 2,
                                                                 "cached_tokens": 5}), stats)
        self.assertIsNone(stats["total_tokens"])
        for included, total in ((True, 12), (False, 17)):
            self.invoke("antigravity", self.success("antigravity", {"input_tokens": 10, "output_tokens": 2,
                        "cached_tokens": 5, "cached_in_input": included, "includes_subagents": True}), stats)
            self.assertEqual(stats["total_tokens"], total)
            self.assertTrue(stats["includes_subagents"])

    def test_invalid_token_values_are_unknown_but_zero_is_known(self):
        for party in parties.AI_PARTIES:
            with self.subTest(party=party):
                stats = {}
                self.invoke(party, self.success(party, {"input_tokens": True, "output_tokens": "10",
                                  "cached_tokens": -1, "total_tokens": 0}), stats)
                self.assertEqual(stats["total_tokens"], 0)
                self.assertIsNone(stats["input_tokens"])
                self.assertIsNone(stats["output_tokens"])
                self.assertIsNone(stats["cached_tokens"])

    def test_claude_stream_messages_deduplicate_and_accumulate(self):
        stats = {}
        message = {"id": "m1", "model": "actual", "usage": {"input_tokens": 10, "output_tokens": 0,
                   "cache_read_input_tokens": 2, "cache_creation_input_tokens": 0}, "content": []}
        events = [
            {"type": "stream_event", "event": {"type": "message_start", "message": message}},
            {"type": "stream_event", "event": {"type": "message_delta", "usage": {"output_tokens": 3}}},
            {"type": "assistant", "message": {**message, "usage": {**message["usage"], "output_tokens": 3}}},
            {"type": "assistant", "message": {**message, "id": "m2"}},
            {"type": "assistant", "parent_tool_use_id": "child", "message": {
                "id": "child", "model": "child-model", "usage": {"input_tokens": 999}}},
        ]
        self.invoke("claude", events + self.success("claude"), stats)
        self.assertEqual((stats["input_tokens"], stats["output_tokens"], stats["cached_tokens"]), (20, 3, 4))
        self.assertEqual(stats["total_tokens"], 27)
        self.assertEqual(stats["model"], "actual")
        self.assertFalse(stats["complete"])

    def test_claude_tool_results_deduplicate(self):
        stats = {}
        tool = {"type": "tool_use", "name": "Agent", "id": "a"}
        events = [{"type": "stream_event", "event": {"type": "content_block_start", "content_block": tool}},
                  {"type": "assistant", "message": {"content": [tool, {**tool, "id": "b", "name": "Task"}]}},
                  {"type": "user", "message": {"content": [
                      {"type": "tool_result", "tool_use_id": "a"},
                      {"type": "tool_result", "tool_use_id": "b", "is_error": True},
                      {"type": "tool_result", "tool_use_id": "unrelated"}]}}]
        self.invoke("claude", events * 2 + self.success("claude"), stats)
        self.assertEqual(stats["subagents"], {"started": 2, "completed": 1, "failed": 1, "ids": ["a", "b"]})

    def test_claude_background_result_is_not_completion(self):
        stats = {}
        events = [{"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Task", "id": "a"}]}},
                  {"type": "user", "tool_use_result": {"isAsync": True}, "message": {"content": [
                      {"type": "tool_result", "tool_use_id": "a"}]}}]
        self.invoke("claude", events + self.success("claude"), stats)
        self.assertEqual(stats["subagents"]["started"], 1)
        self.assertEqual(stats["subagents"]["completed"], 0)

    def test_antigravity_subagent_states_deduplicate(self):
        stats = {}
        events = [{"event": "step_update", "step_update": {"step_type": "subagent", "step_id": ident, "state": state}}
                  for ident, state in [("a", "ACTIVE"), ("a", "ACTIVE"), ("a", "DONE"), ("a", "DONE"),
                                       ("b", "ACTIVE"), ("b", "ERROR"), ("c", "ERROR")]]
        self.invoke("antigravity", events + self.success("antigravity"), stats)
        self.assertEqual(stats["subagents"], {"started": 2, "completed": 1, "failed": 2, "ids": ["a", "b", "c"]})

    def test_codex_spawn_success_is_separate_from_tool_and_agent_completion(self):
        stats = {}

        def collab(tool, status, receivers, states=None):
            return {"type": "item.completed", "item": {"type": "collab_tool_call", "id": "call",
                    "tool": tool, "status": status, "receiver_thread_ids": receivers, "agents_states": states or {}}}

        events = [collab("spawn_agent", "in_progress", []), collab("spawn_agent", "failed", []),
                  collab("spawn_agent", "completed", ["a", "b"]), collab("spawn_agent", "completed", ["a", "b"]),
                  collab("send_input", "completed", ["c"]), collab("resume_agent", "completed", ["c"]),
                  collab("wait", "completed", ["a", "b"], {"a": {"status": "completed"}, "b": {"status": "errored"}}),
                  collab("close_agent", "completed", ["c"], {"c": {"status": "shutdown"}})]
        self.invoke("codex", events * 2 + self.success("codex"), stats, subagents=False)
        self.assertEqual(stats["subagents"], {"started": 2, "completed": 1, "failed": 1, "ids": ["a", "b"]})

    def test_unobserved_differs_from_observable_zero(self):
        for party in parties.AI_PARTIES:
            with self.subTest(party=party):
                stats = {}
                collector = parties._CallStats({"_stats": stats}, party)
                collector.event({"unknown": "event"})
                self.assertIsNone(stats["subagents"])
                event = {"claude": {"type": "assistant", "message": {"content": []}},
                         "antigravity": {"event": "step_update", "step_update": {"step_type": "agent_response"}},
                         "codex": {"type": "item.started", "item": {"type": "command_execution"}}}[party]
                collector.event(event)
                self.assertEqual(stats["subagents"], {"started": 0, "completed": 0, "failed": 0, "ids": []})

    def test_antigravity_interrupted_result_and_live_behavior_unchanged(self):
        stats = {}
        live = Mock()
        result = {"status": "ERROR", "error": "The stream was interrupted", "response": "OK",
                  "usage": {"total_tokens": 12}}
        events = [{"event": "step_update", "step_update": {"step_type": "agent_response", "text_delta": "OK"}},
                  {"event": "result", "result": result}]
        reply, _, meta = self.invoke("antigravity", events, stats, live=live)
        self.assertEqual(reply, "OK")
        self.assertIn(parties.STREAM_INTERRUPTED, meta)
        live.add_text.assert_called_once_with("OK")
        self.assertTrue(stats["complete"])
        self.assertEqual(stats["total_tokens"], 12)

    def test_final_stats_survive_timeout_after_final_event(self):
        for party in parties.AI_PARTIES:
            with self.subTest(party=party):
                stats = {}
                with self.assertRaises(subprocess.TimeoutExpired):
                    self.invoke(party, self.success(party, {"total_tokens": 12}), stats,
                                error=subprocess.TimeoutExpired("fake", 60))
                self.assertEqual(stats["total_tokens"], 12)
                self.assertTrue(stats["complete"])

    def test_subagent_progress_survives_timeout_for_every_cli(self):
        events = {
            "claude": {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Task", "id": "a"}]}},
            "antigravity": {"event": "step_update", "step_update": {
                "step_type": "subagent", "step_id": "a", "state": "ACTIVE"}},
            "codex": {"type": "item.completed", "item": {"type": "collab_tool_call", "tool": "spawn_agent",
                       "status": "completed", "receiver_thread_ids": ["a"]}},
        }
        for party, event in events.items():
            with self.subTest(party=party):
                stats = {}
                with self.assertRaises(subprocess.TimeoutExpired):
                    self.invoke(party, [event, event], stats, error=subprocess.TimeoutExpired("fake", 60))
                self.assertEqual(stats["subagents"], {"started": 1, "completed": 0, "failed": 0, "ids": ["a"]})

    def test_actual_models_from_init_and_result(self):
        for party, event in [
            ("claude", {"type": "system", "subtype": "init", "model": "actual"}),
            ("antigravity", {"event": "init", "model": "actual"}),
            ("codex", {"type": "thread.started", "thread_id": "s", "model": "actual"}),
        ]:
            with self.subTest(party=party):
                stats = {}
                self.invoke(party, [event] + self.success(party), stats, model="alias")
                self.assertEqual(stats["model"], "actual")
        stats = {}
        self.invoke("claude", [{"type": "result", "result": "OK", "modelUsage": {"actual": {}}}], stats)
        self.assertEqual(stats["model"], "actual")
        self.invoke("claude", [{"type": "result", "result": "OK", "modelUsage": {"one": {}, "two": {}}}], stats)
        self.assertIsNone(stats["model"])

    def test_partial_events_after_final_cannot_downgrade_final_usage(self):
        for party in parties.AI_PARTIES:
            with self.subTest(party=party):
                stats = {}
                collector = parties._CallStats({"_stats": stats}, party)
                for event in self.success(party, {"total_tokens": 40}):
                    collector.event(event)
                collector.event(self.partial(party))
                self.assertEqual(stats["total_tokens"], 40)
                self.assertTrue(stats["complete"])


if __name__ == "__main__":
    unittest.main()
