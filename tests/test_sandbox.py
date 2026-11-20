"""Sandbox detection and side-effect boundaries; no real AI or network calls."""

import argparse
import io
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import ANY, Mock, patch

from agoralib import cli, jobs, parties, quota, sandbox


class DetectionTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {"CODEX_SANDBOX_NETWORK_DISABLED": "0",
                                     "USERNAME": "CodexSandboxOfflineFake"})
        env.start()
        self.addCleanup(env.stop)

    def windows_user(self, username=None, error=None, success=True):
        def query(buffer, size):
            if error:
                raise error
            buffer.value = username or ""
            return int(success)

        dll = Mock()
        dll.advapi32.GetUserNameW.side_effect = query
        return dll

    def test_environment_requires_exact_one_even_when_token_query_fails(self):
        for username in (None, ""):
            with patch.object(sandbox, "token_username", return_value=username):
                for value in ("1", "0", "true", ""):
                    with self.subTest(username=username, value=value), \
                            patch.dict(os.environ, {"CODEX_SANDBOX_NETWORK_DISABLED": value}):
                        self.assertEqual(sandbox.network_restricted(), value == "1")
                        self.assertFalse(sandbox.is_codex_sandbox())
                        if value == "1":
                            with self.assertRaises(parties.CallError) as caught:
                                sandbox.require_network()
                            self.assertIn("依據：環境變數 CODEX_SANDBOX_NETWORK_DISABLED=1",
                                          str(caught.exception))
                        else:
                            sandbox.require_network()

    def test_actual_token_offline_online_and_regular_users(self):
        for name, restricted, identity in (("CodexSandboxOffline", True, True),
                                            ("CodexSandboxOffline123", True, True),
                                            ("CodexSandboxOnline123", False, True), ("Richard", False, False)):
            for value in ("0", "1"):
                with self.subTest(name=name, value=value), patch.object(sandbox.sys, "platform", "win32"), \
                        patch.object(sandbox.ctypes, "windll", self.windows_user(name), create=True), \
                        patch.dict(os.environ, {"CODEX_SANDBOX_NETWORK_DISABLED": value}):
                    self.assertEqual(sandbox.token_username(), name)
                    self.assertEqual(sandbox.network_restricted(), restricted)
                    self.assertEqual(sandbox.is_codex_sandbox(), identity)
                    if restricted:
                        with self.assertRaises(parties.CallError) as caught:
                            sandbox.require_network()
                        self.assertEqual(caught.exception.kind, "sandbox")
                        self.assertIn(f"依據：token={name}", str(caught.exception))
                    else:
                        sandbox.require_network()

    def test_failed_query_is_unknown_and_does_not_trust_username(self):
        for dll in (self.windows_user(success=False), self.windows_user(error=OSError("unavailable")), object()):
            with self.subTest(dll=dll), patch.object(sandbox.sys, "platform", "win32"), \
                    patch.object(sandbox.ctypes, "windll", dll, create=True):
                self.assertIsNone(sandbox.token_username())
                self.assertFalse(sandbox.network_restricted())
                self.assertFalse(sandbox.is_codex_sandbox())

    def test_non_windows_does_not_query_token(self):
        dll = self.windows_user("CodexSandboxOffline")
        with patch.object(sandbox.sys, "platform", "linux"), \
                patch.object(sandbox.ctypes, "windll", dll, create=True):
            self.assertIsNone(sandbox.token_username())
            self.assertFalse(sandbox.network_restricted())
            with patch.dict(os.environ, {"CODEX_SANDBOX_NETWORK_DISABLED": "1"}):
                self.assertTrue(sandbox.network_restricted())
                self.assertFalse(sandbox.is_codex_sandbox())
            dll.advapi32.GetUserNameW.assert_not_called()


class OperationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for patcher in (
                patch.dict(os.environ, {"CODEX_SANDBOX_NETWORK_DISABLED": "1", parties.NESTED_ENV: "",
                                        jobs.JOB_ENV: "", cli.worktree.WORK_ENV: "", cli.worktree.CONTROL_ENV: "",
                                        cli.worktree.MANIFEST_ENV: "", parties.LOCKED_ENV: ""}),
                patch.object(sandbox, "token_username", return_value=None),
                patch.object(quota, "BLOCKED_FILE", self.root / "blocked.json")):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.ws = cli.Workspace(self.root)
        self.thread = self.ws.threads / "t1"
        self.thread.mkdir(parents=True)
        cli.save_state(self.thread, {"topic": "sandbox", "parties": {p: cli.new_party() for p in parties.AI_PARTIES},
                                     "messages": []})
        (self.thread / "transcript.md").write_text("# sandbox\n", encoding="utf-8")

    def files(self):
        return {p.relative_to(self.root).as_posix(): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}

    def args(self, **kwargs):
        return argparse.Namespace(**{"thread": "t1", "sender": "codex", "to": "antigravity",
                                     "message": "test", "file": None, "blind": False, **kwargs})

    def assert_rejected(self, command):
        before = self.files()
        with patch.object(jobs, "start") as start, patch.object(jobs, "spawn") as spawn, \
                patch.object(cli.worktree, "prepare") as prepare, patch.object(cli, "recover_assignments") as recover, \
                patch.object(parties.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(SystemExit, "依據：環境變數 CODEX_SANDBOX_NETWORK_DISABLED=1"):
                cli.main(["--workspace", str(self.root)] + command)
            for action in (start, spawn, prepare, recover, popen):
                action.assert_not_called()
        self.assertEqual(self.files(), before)

    def test_send_rejected_before_message_job_or_input_file(self):
        self.assert_rejected(["send", "t1", "--from", "codex", "--to", "antigravity", "--message", "hello"])

    def test_remaining_network_commands_rejected_in_foreground(self):
        commands = (["reply", "t1", "--party", "antigravity"], ["auto", "t1"],
                    ["summarize", "t1", "--by", "claude"],
                    ["assign", "t1", "--from", "codex", "--message", "work", "--allow", "app.py"],
                    ["assign", "t1", "--from", "codex", "--resume", "previous"],
                    ["relay", "t1", "--from", "codex", "--message", "work", "--allow", "app.py"])
        for command in commands:
            with self.subTest(command=command):
                self.assert_rejected(command)

    def test_invoked_flag_does_not_bypass_sandbox(self):
        with patch.dict(os.environ, {parties.NESTED_ENV: "1"}):
            self.assert_rejected(["send", "t1", "--from", "codex", "--to", "antigravity", "--message", "hi"])

    def test_send_without_targets_records_locally(self):
        for sender, to in (("codex", "none"), (parties.HUMAN, None)):
            with self.subTest(sender=sender, to=to), patch.object(cli, "in_background") as background, \
                    patch.object(cli, "invoke") as invoke, patch("sys.stdout", io.StringIO()):
                cli.cmd_send(self.ws, self.args(sender=sender, to=to))
                invoke.assert_not_called()
                background.assert_called_once()
        self.assertEqual(len(cli.load_state(self.thread)["messages"]), 2)

    def test_call_party_rejects_before_existing_auth_block(self):
        for existing in (False, True):
            with self.subTest(existing=existing):
                if existing:
                    quota.BLOCKED_FILE.write_text('{"antigravity":{"reason":"auth","message":"existing"}}', encoding="utf-8")
                before = self.files()
                with patch.object(quota, "blocked") as blocked, patch.object(quota, "mark_blocked") as mark, \
                        patch.object(quota, "clear_blocked") as clear, \
                        patch.dict(cli.CALLERS, {"antigravity": Mock()}) as callers:
                    with self.assertRaises(parties.CallError) as caught:
                        cli.call_party("antigravity")
                    self.assertEqual(caught.exception.kind, "sandbox")
                    self.assertFalse(caught.exception.blocked)
                    for action in (blocked, mark, clear, callers["antigravity"]):
                        action.assert_not_called()
                self.assertEqual(self.files(), before)

    def test_direct_provider_calls_never_start_cli_or_write_overflow(self):
        before = self.files()
        with patch.object(parties, "_run") as run, patch.object(parties, "agy_version") as version:
            for caller in (parties.call_antigravity, parties.call_claude, parties.call_codex):
                with self.subTest(caller=caller.__name__), self.assertRaises(parties.CallError) as caught:
                    caller("x" * 30000, None, {}, False, self.root, 1, self.thread)
                self.assertEqual(caught.exception.kind, "sandbox")
            run.assert_not_called()
            version.assert_not_called()
        self.assertEqual(self.files(), before)

    def test_online_auth_keeps_classification_stderr_and_global_blocks(self):
        quota.BLOCKED_FILE.write_text('{"codex":{"reason":"auth","message":"preserve"}}', encoding="utf-8")
        original = quota.BLOCKED_FILE.read_bytes()
        events = parties._RunEvents([{"event": "result", "result": {
            "status": "ERROR", "error": "authentication failed or timed out"}}])
        events.exit_code = 1
        with patch.object(sandbox, "token_username", return_value="CodexSandboxOnline123"), \
                patch.object(parties, "_run", return_value=(events, "auth stderr evidence")) as run, \
                patch.object(parties, "agy_version", return_value="test"), \
                patch.object(quota, "mark_blocked") as mark:
            with self.assertRaises(parties.CallError) as caught:
                cli.call_party("antigravity", "hi", None, {}, False, self.root, 1, self.thread)
            error = caught.exception
            self.assertEqual(error.kind, "auth")
            self.assertTrue(error.blocked)
            self.assertIn(sandbox.AUTH_NOTE, str(error))
            self.assertEqual(error.diagnostics["stderr_summary"], "auth stderr evidence")
            self.assertEqual(error.diagnostics["result_error"], "authentication failed or timed out")
            self.assertIn(sandbox.AUTH_NOTE, (self.thread / "progress.md").read_text(encoding="utf-8"))
            run.assert_called_once()
            mark.assert_not_called()
        self.assertEqual(quota.BLOCKED_FILE.read_bytes(), original)

    def test_non_sandbox_auth_still_marks_blocked(self):
        error = parties.CallError("authentication failed", kind="auth", blocked=True)
        with patch.object(sandbox, "token_username", return_value="Richard"), \
                patch.dict(cli.CALLERS, {"antigravity": Mock(side_effect=error)}):
            with self.assertRaises(parties.CallError):
                cli.call_party("antigravity")
        self.assertEqual(quota.blocked("antigravity")["reason"], "auth")

    def test_quota_skips_all_network_probes_without_mutation(self):
        with patch.object(quota, "REFRESH_LOCK", self.root / "refresh.lock"), \
                patch.object(quota.subprocess, "run") as run, patch.object(quota.subprocess, "Popen") as popen, \
                patch.object(quota, "_candidates") as candidates, patch.object(quota, "_request") as request, \
                patch.object(quota, "_write_json") as write:
            before = self.files()
            self.assertFalse(quota.probe_claude())
            self.assertIsNone(quota.query_antigravity())
            quota.refresh(force=True)
            quota.refresh_in_background()
            for action in (run, popen, candidates, request, write):
                action.assert_not_called()
            self.assertEqual(self.files(), before)

    def test_quota_command_reads_cache(self):
        cache = self.root / "claude-cache.json"
        cache.write_text(json.dumps({"updated_at": time.time(), "seven_day": {
            "used_percentage": 23, "resets_at": time.time() + 3600}}), encoding="utf-8")
        with patch.object(quota, "CLAUDE_SNAPSHOT", cache), patch.object(quota, "USAGE", {
                "claude": quota.claude_usage, "antigravity": lambda: None, "codex": lambda: None}), \
                patch.object(quota.subprocess, "run") as run, patch("sys.stdout", io.StringIO()) as out:
            quota.main(["--refresh", "--json"])
            self.assertEqual(json.loads(out.getvalue())["claude"]["remaining_pct"], 77)
            run.assert_not_called()

    def test_check_remains_available_with_local_command(self):
        with patch.object(cli, "command_config", return_value={"check": ["python", "-m", "unittest"]}), \
                patch.object(cli, "check_slot_context", return_value=(self.root, None, "test")), \
                patch.object(cli.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")), \
                patch.object(cli, "run_configured_command", return_value=subprocess.CompletedProcess([], 0, "", "")) as run, \
                patch("sys.stdout", io.StringIO()), patch.dict(os.environ, {cli.failure_baseline.RECORD_ENV: ""}):
            with self.assertRaises(SystemExit) as caught:
                cli.cmd_check(self.ws, argparse.Namespace(task=None))
            self.assertEqual(caught.exception.code, 0)
            run.assert_called_once()

    def test_models_read_stale_cache_but_cannot_start_online_refresh(self):
        cache = self.root / "models.json"
        data = {"fetched_at": 0, "codex": {"models": [{"id": "cached", "label": "cached"}]}}
        cache.write_text(json.dumps(data), encoding="utf-8")
        with patch.object(cli.models, "CACHE", cache), patch.object(cli.models, "catalog") as catalog, \
                patch("sys.stdout", io.StringIO()) as out:
            cli.cmd_models(self.ws, argparse.Namespace(refresh=False, json=True))
            self.assertEqual(json.loads(out.getvalue()), data)
            with self.assertRaisesRegex(SystemExit, "目前在 Codex 沙箱"):
                cli.cmd_models(self.ws, argparse.Namespace(refresh=True, json=True))
            catalog.assert_not_called()
        self.assertEqual(json.loads(cache.read_text(encoding="utf-8")), data)
        with patch.object(cli.models, "CACHE", self.root / "missing.json"), \
                patch.object(cli.models, "catalog") as catalog:
            with self.assertRaisesRegex(SystemExit, "目前在 Codex 沙箱"):
                cli.cmd_models(self.ws, argparse.Namespace(refresh=False, json=True))
            catalog.assert_not_called()

    def test_prune_only_blocks_when_codex_cli_would_be_started(self):
        state = cli.load_state(self.thread)
        state["sessions"] = [{"party": "codex", "id": "owned-session", "external": False}]
        cli.save_state(self.thread, state)
        before = self.files()
        with patch.object(cli.prune, "delete_thread", return_value={"done": [], "errors": []}) as delete, \
                patch.object(cli.prune, "compact_work_log") as compact, patch("sys.stdout", io.StringIO()):
            args = argparse.Namespace(thread=["t1"], days=30, yes=True, keep_sessions=False,
                                      json=True, verbose=False)
            with self.assertRaisesRegex(SystemExit, "目前在 Codex 沙箱"):
                cli.cmd_prune(self.ws, args)
            delete.assert_not_called()
            compact.assert_not_called()
            self.assertEqual(self.files(), before)
            args.yes = False
            cli.cmd_prune(self.ws, args)
            delete.assert_not_called()
            args.yes, args.keep_sessions = True, True
            cli.cmd_prune(self.ws, args)
            delete.assert_called_once_with(self.thread, ANY, False)


if __name__ == "__main__":
    unittest.main()
