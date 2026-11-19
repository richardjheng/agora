"""Antigravity diagnostic evidence and retry suppression; all agy calls are mocked."""

import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agoralib import cli, parties, quota


TELEMETRY = ('Failed to shutdown telemetry client: Post "https://play.googleapis.com/log": '
             'connectex: An attempt was made to access a socket in a way forbidden by its access permissions.\n')


class TestAgyDiagnostics(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.workspace = self.root / "workspace"
        for patcher in (patch.object(quota, "BLOCKED_FILE", self.root / "blocked.json"),
                        patch.object(parties, "agy_version", return_value="agy test-1")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def call(self, result, stderr="", steps=(), opts=None, work=False):
        events = parties._RunEvents([{"event": "result", "result": result}])
        events.exit_code = 17 if result.get("status") == "ERROR" else 0

        def run(cmd, workspace, stdin_text, timeout, on_event=None, extra_env=None):
            self.command = cmd
            for name, params in steps:
                on_event({"event": "step_update", "step_update": {
                    "step_type": "tool", "state": "ACTIVE", "tool_name": name,
                    "tool_info": {"parameters": params}}})
            return events, stderr

        with patch.object(parties, "_run", side_effect=run) as runner:
            self.runner = runner
            return parties.call_antigravity("private prompt", None, opts or {}, work, self.workspace, 3, self.root)

    def fail(self, result=None, stderr="", **kwargs):
        with self.assertRaises(parties.CallError) as caught:
            self.call(result or {"status": "ERROR", "error": "unexplained failure"}, stderr, **kwargs)
        error = caught.exception
        record = (self.root / "progress.md").read_text(encoding="utf-8")
        self.assertIn('"agy_version": "agy test-1"', record)
        self.assertIn('"exit_code": 17', record)
        return error, record

    def test_success_with_telemetry_remains_success(self):
        reply, _, meta = self.call({"status": "SUCCESS", "response": "OK"}, TELEMETRY)
        self.assertEqual(reply, "OK")
        self.assertIn("附註：telemetry", meta)
        self.runner.assert_called_once()
        self.assertFalse((self.root / "progress.md").exists())

    def test_ordinary_stderr_is_not_a_telemetry_warning(self):
        self.assertNotIn("telemetry", self.call({"status": "SUCCESS", "response": "OK"}, "ordinary warning\n")[2])

    def test_auth_plus_telemetry_preserves_primary_error_and_evidence(self):
        error, record = self.fail({"status": "ERROR", "error": "authentication failed or timed out",
                                   "num_turns": 0}, TELEMETRY)
        self.assertEqual(error.kind, "auth")
        self.assertTrue(error.blocked)
        self.assertFalse(error.quota_exhausted)
        self.assertIn("重新登入", str(error))
        self.assertIn("原因未明", error.diagnostics["auth_cause"])
        self.assertEqual(error.diagnostics["result_error"], "authentication failed or timed out")
        self.assertIn("play.googleapis.com", record)
        self.assertIn("telemetry_note", record)

    def test_telemetry_alone_does_not_prove_network_failure(self):
        self.assertIsNone(parties.error_kind(None, TELEMETRY))
        error, _ = self.fail(stderr=TELEMETRY)
        self.assertEqual(error.kind, "unknown")
        self.assertIn("原因未明", str(error))
        self.assertFalse(error.blocked)

    def test_classification_uses_evidence(self):
        examples = {"not logged in": "auth", "no available credentials": "auth",
                    "credentials not found": "auth", "authentication timed out": "auth",
                    "connection refused": "network", "DNS failure": "network",
                    "failed to resolve api.example.test": "network", "connection timed out": "network",
                    "request timed out": None, "access denied": None, "unknown failure": None,
                    "HTTP 429": "quota", "out of credits": "billing"}
        for message, expected in examples.items():
            with self.subTest(message=message):
                self.assertEqual(parties.error_kind({"error": message}), expected)
        self.assertEqual(parties.error_kind(None, "not logged in; " + TELEMETRY), "auth")
        self.assertEqual(parties.error_kind({"stderr": TELEMETRY, "error": "not logged in"}), "auth")
        self.assertEqual(parties.error_kind(None, TELEMETRY + "connection refused"), "network")
        self.assertIsNone(parties.error_kind({"session_id": "401", "duration_seconds": 429}))

    def test_auth_401_and_credentials_narrowing(self):
        non_auth = [
            ("line 401", None),
            ("exit=401", None),
            ("tests/test_401.py", None),
            ("401 tests passed", None),
            ("SyntaxError at line 401 in tests/test_401.py (exit=401)", None),
            ("credentials.py: argument required", None),
            ("load credentials from disk; config required", None),
        ]
        for text, expected in non_auth:
            with self.subTest(non_auth=text):
                self.assertEqual(parties.error_kind({"error": text}), expected)
                self.assertEqual(parties.error_kind(None, text), expected)
        self.assertIsNone(parties.error_kind({"exit_code": 401}))

        http_auth = [
            "HTTP 401",
            "HTTP/1.1 401",
            "status 401",
            "status code 401",
            "statusCode: 401",
            "401 Unauthorized",
            '{"code": 401, "message": "invalid token"}',
            "credentials are required",
            "credentials have expired",
        ]
        for text in http_auth:
            with self.subTest(http_auth=text):
                self.assertEqual(parties.error_kind({"error": text}), "auth")
                self.assertEqual(parties.error_kind(None, text), "auth")
        self.assertEqual(parties.error_kind({"code": 401}), "auth")

        # Claude / Codex style error payloads
        self.assertEqual(parties.error_kind({"type": "result", "is_error": True, "result": "API Error: 401 Unauthorized"}), "auth")
        self.assertEqual(parties.error_kind({"type": "error", "message": "unexpected status 401 Unauthorized"}), "auth")
        self.assertIsNone(parties.error_kind({"type": "result", "is_error": True, "result": "failed at line 401 (exit=401)"}))
        self.assertIsNone(parties.error_kind({"type": "error", "message": "401 tests passed in tests/test_401.py"}))

    def test_auth_evidence_before_long_telemetry_is_not_truncated_before_classification(self):
        error, _ = self.fail(stderr="not logged in\n" + "warning\n" * 400 + TELEMETRY)
        self.assertEqual(error.kind, "auth")
        self.assertLessEqual(len(error.diagnostics["stderr_summary"]), 2000)

    def test_auth_causes_are_distinct_when_proven(self):
        for message, cause in (("no available credentials", "沒有可用憑證"),
                               ("authentication timed out", "驗證逾時")):
            error, _ = self.fail({"status": "ERROR", "error": message})
            self.assertEqual(error.diagnostics["auth_cause"], cause)

    def test_network_error_gives_connection_advice(self):
        error, _ = self.fail({"status": "ERROR", "error": "connection refused"})
        self.assertEqual(error.kind, "network")
        self.assertIn("防火牆", str(error))
        self.assertFalse(error.blocked)

    def test_timeout_keeps_stderr_without_leaking_command_prompt(self):
        exc = subprocess.TimeoutExpired(["agy", "-p", "private prompt"], 3,
                                        stderr=(TELEMETRY + "token=timeout-secret").encode())
        with patch.object(parties, "_run", side_effect=exc), self.assertRaises(subprocess.TimeoutExpired) as caught:
            parties.call_antigravity("private prompt", None, {}, False, self.workspace, 3, self.root)
        error = caught.exception
        self.assertEqual(error.kind, "timeout")
        self.assertIsNone(error.diagnostics["exit_code"])
        self.assertIn("telemetry", str(error))
        self.assertNotIn("private prompt", str(error))
        self.assertNotIn("timeout-secret", (self.root / "progress.md").read_text(encoding="utf-8"))

    def test_denials_preserve_original_events_and_mark_inferred_target(self):
        denied = [{"action": "read_file", "display_name": "Read File", "detail": "permission denied"}]
        steps = [("read_file", {"path": "first.md"}), ("read_file", {"path": "last.md"})]
        error, record = self.fail({"status": "ERROR", "error": "permission denied", "denied_actions": denied},
                                  steps=steps, work=True, opts={"control_dir": "control folder"})
        self.assertEqual(error.kind, "denied")
        self.assertTrue(error.denied)
        self.assertEqual(error.denials[0]["target"], "推定目標：last.md")
        self.assertTrue(error.denials[0]["target_inferred"])
        self.assertIn("推定目標", cli.denial_note_for(error.denials))
        self.assertEqual(error.diagnostics["denied_actions"], denied)
        self.assertEqual(error.diagnostics["cwd"], str(self.workspace))
        self.assertEqual(error.diagnostics["args"], {"--add-dir": ["control folder"], "--mode": ["accept-edits"]})
        self.assertIn('"detail": "permission denied"', record)

    def test_denial_without_matching_step_does_not_invent_target(self):
        denied = parties.agy_denials([{"action": "read_file"}], [("other", {"path": "wrong.md"})])
        self.assertIsNone(denied[0]["target"])
        self.assertFalse(denied[0]["target_inferred"])

    def test_error_and_raw_events_are_redacted_before_persistence(self):
        result = {"status": "ERROR", "error": 'authentication failed password="password-secret"',
                  "denied_actions": [{"action": "read_file", "token": "event-secret"}]}
        stderr = ('Authorization: Bearer bearer-secret\n'
                  '{"api_key": "api-secret", "refresh_token": "refresh-secret"}\n'
                  'https://user:url-secret@example.test/?code=query-secret\n')
        error, record = self.fail(result, stderr)
        output = str(error) + record + json.dumps(error.diagnostics)
        for secret in ("password-secret", "event-secret", "bearer-secret", "api-secret",
                       "refresh-secret", "url-secret", "query-secret"):
            self.assertNotIn(secret, output)
        self.assertIn("[REDACTED]", output)
        self.assertEqual(result["denied_actions"][0]["token"], "event-secret")

    def test_auth_mark_suppresses_shared_send_and_assign_call_path_until_unblocked(self):
        def auth(*args, **kwargs):
            raise parties.call_error("authentication failed；請執行 agy 重新登入後再試", None, "not logged in")

        with patch.dict(cli.CALLERS, {"antigravity": Mock(side_effect=auth)}):
            for _ in range(2):
                with self.assertRaises(parties.CallError) as caught:
                    cli.call_party("antigravity", "p", None, {}, False, self.root, 3, self.root)
                self.assertTrue(caught.exception.blocked)
            cli.CALLERS["antigravity"].assert_called_once()
        self.assertEqual(quota.blocked("antigravity")["reason"], "auth")
        self.assertIn("重新登入", quota.blocked("antigravity")["message"])
        self.assertIn("agora quota --unblock antigravity", str(caught.exception))
        with patch.dict(quota.USAGE, {"antigravity": lambda: None}):
            self.assertTrue(quota.unavailable(quota.usage_for("antigravity", "uncached-model")))
        quota.clear_blocked("antigravity")
        with patch.dict(cli.CALLERS, {"antigravity": Mock(return_value=("OK", "c", "meta"))}):
            self.assertEqual(cli.call_party("antigravity"), ("OK", "c", "meta"))
        self.assertIsNone(quota.blocked("antigravity"))

    def test_assignment_candidate_explains_auth_and_unblock_without_calling_agy(self):
        quota.mark_blocked("antigravity", "重新登入", reason="auth")
        with patch.dict(quota.USAGE, {"antigravity": lambda: None}):
            usage = quota.usage_for("antigravity", "uncached-model")
        info = cli.usage_info({"model": "uncached-model"}, usage)
        text = cli.describe_candidate("antigravity", info)
        self.assertIn("auth", text)
        self.assertIn("重新登入", text)
        self.assertIn("agora quota --unblock antigravity", text)

    def test_repeated_discussion_request_does_not_send_again_after_auth(self):
        ws = cli.Workspace(self.root)
        thread = self.root / "thread"
        thread.mkdir()
        cli.save_state(thread, {"topic": "auth", "parties": {"antigravity": {
            "session_id": None, "seen": 0, "model": None}}, "messages": []})
        failure = parties.call_error("authentication failed；請執行 agy 重新登入", None, "not logged in")
        caller = Mock(side_effect=failure)
        with patch.dict(cli.CALLERS, {"antigravity": caller}), \
                patch.object(cli, "call_opts", return_value={}), \
                patch.object(cli, "git_status", return_value=""), \
                patch.object(cli.jobs, "set_current"), \
                patch.object(cli.shares, "record_call", return_value={}), \
                patch("sys.stderr", io.StringIO()):
            for _ in range(2):
                with self.assertRaises(cli.CallFailed) as caught:
                    cli.invoke(ws, thread, "antigravity")
        caller.assert_called_once()
        self.assertIn("agora quota --unblock antigravity", str(caught.exception))

    def test_success_clears_mark_recorded_during_call(self):
        def success():
            quota.mark_blocked("antigravity", "old auth", reason="auth")
            return "OK", "c", "meta"

        with patch.dict(cli.CALLERS, {"antigravity": success}):
            cli.call_party("antigravity")
        self.assertIsNone(quota.blocked("antigravity"))

    def test_unparseable_output_keeps_exit_code_and_redacted_stderr(self):
        error = parties.CallError("unparseable")
        error.diagnostics = {"exit_code": 23, "output": "not logged in", "stderr": "token=parse-secret"}
        with patch.object(parties, "_run", side_effect=error), self.assertRaises(parties.CallError) as caught:
            parties.call_antigravity("p", None, {}, False, self.workspace, 3, self.root)
        self.assertEqual(caught.exception.kind, "auth")
        self.assertEqual(caught.exception.diagnostics["exit_code"], 23)
        self.assertNotIn("parse-secret", str(caught.exception))

    def test_scratch_equal_to_workspace_does_not_write_root_progress(self):
        with patch.object(parties, "_run", return_value=([{"status": "ERROR", "error": "bad"}], "")), \
                self.assertRaises(parties.CallError) as caught:
            parties.call_antigravity("p", None, {}, False, self.root, 3, self.root)
        self.assertFalse((self.root / "progress.md").exists())
        self.assertIn("record_error", caught.exception.diagnostics)

    def test_missing_executable_keeps_file_not_found_contract_and_diagnostics(self):
        with patch.object(parties, "_run", side_effect=FileNotFoundError("agy missing")), \
                self.assertRaises(FileNotFoundError) as caught:
            parties.call_antigravity("p", None, {}, False, self.workspace, 3, self.root)
        self.assertEqual(caught.exception.diagnostics["kind"], "unknown")
        self.assertIn("原因未明", str(caught.exception))
        self.assertTrue((self.root / "progress.md").exists())


class TestAgyLocalVersionAndRunner(unittest.TestCase):
    def test_version_is_cached_and_bounded_without_online_probe(self):
        parties.agy_version.cache_clear()
        self.addCleanup(parties.agy_version.cache_clear)
        with patch.object(parties.subprocess, "run", return_value=Mock(stdout="agy 1.2.3\n", returncode=0)) as run:
            self.assertEqual(parties.agy_version("mock-agy"), "agy 1.2.3")
            self.assertEqual(parties.agy_version("mock-agy"), "agy 1.2.3")
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], ["mock-agy", "--version"])
        self.assertEqual(run.call_args.kwargs["timeout"], 3)

    def test_runner_retains_actual_exit_code(self):
        proc = Mock(stdin=io.StringIO(), stdout=io.StringIO('{"event":"result","result":{"status":"ERROR"}}\n'),
                    stderr=io.StringIO("stderr summary\n"), returncode=19)
        proc.poll.return_value = 19
        with patch.object(parties.subprocess, "Popen", return_value=proc):
            events, stderr = parties._run(["mock-agy"], Path("."), None, 1)
        self.assertEqual(events.exit_code, 19)
        self.assertEqual(stderr, "stderr summary\n")


if __name__ == "__main__":
    unittest.main()
