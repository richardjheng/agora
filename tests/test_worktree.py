"""Worktree integration tests: temporary repos only; no AI, pip, or network."""

import argparse
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import venv
from pathlib import Path
from unittest.mock import patch

from agoralib import cli, config, parties, prune, snapshot, worktree


class TestGitEnvironment(unittest.TestCase):
    def test_scoped_trust_preserves_existing_options_without_changing_parent(self):
        with tempfile.TemporaryDirectory() as tmp:
            control, task = Path(tmp) / "control", Path(tmp) / "task"
            original = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.quotepath", "GIT_CONFIG_VALUE_0": "false"}
            result = worktree.git_environment(original, (control, task))
            self.assertEqual(original["GIT_CONFIG_COUNT"], "1")
            self.assertEqual(result["GIT_CONFIG_COUNT"], "3")
            self.assertEqual(result["GIT_CONFIG_VALUE_0"], "false")
            self.assertEqual({result["GIT_CONFIG_VALUE_1"], result["GIT_CONFIG_VALUE_2"]},
                             {control.resolve().as_posix(), task.resolve().as_posix()})
            self.assertEqual(worktree.git_environment(result, (control, task)), result)

    def test_git_trust_is_only_injected_for_task_or_controller(self):
        with tempfile.TemporaryDirectory() as tmp:
            control, task, other = Path(tmp) / "control", Path(tmp) / "task", Path(tmp) / "other"
            env = {worktree.CONTROL_ENV: str(control), worktree.WORK_ENV: str(task)}
            with patch.dict(os.environ, env, clear=True), patch.object(worktree.subprocess, "run") as run:
                run.return_value = subprocess.CompletedProcess([], 0, "ok", "")
                worktree.git(control, "status")
                self.assertEqual(run.call_args.kwargs["env"]["GIT_CONFIG_COUNT"], "2")
                worktree.git(other, "status")
                self.assertNotIn("GIT_CONFIG_COUNT", run.call_args.kwargs["env"])
                self.assertNotIn("GIT_CONFIG_COUNT", os.environ)


class WorktreeTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for name in ("AGORA_WORKSPACE", worktree.CONTROL_ENV, worktree.WORK_ENV,
                     worktree.DISPATCH_ENV, worktree.MANIFEST_ENV, cli.LOCKED_ENV):
            os.environ.pop(name, None)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        worktree.git(self.root, "init", "-b", "main")
        worktree.git(self.root, "config", "user.email", "test@localhost")
        worktree.git(self.root, "config", "user.name", "Test")
        (self.root / ".gitignore").write_text(".agora/\n.venv/\n__pycache__/\n", encoding="utf-8")
        (self.root / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
        (self.root / "other.py").write_text("VALUE = 9\n", encoding="utf-8")
        (self.root / "demo").mkdir()
        (self.root / "demo" / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
        worktree.git(self.root, "add", ".")
        worktree.git(self.root, "commit", "-m", "base")
        self.thread = self.root / ".agora" / "threads" / "t1"
        self.thread.mkdir(parents=True)
        cli.save_state(self.thread, {"topic": "test", "parties": {}, "messages": []})
        (self.thread / "transcript.md").write_text("", encoding="utf-8")

    def task(self, key="one", allowed=None):
        return worktree.prepare(self.root, key, allowed or ["app.py", "demo/*"], self.thread)

    def message(self, meta):
        Path(meta["commit_message"]).write_text("Fix app value\n\nTests: isolated value assertion\n", encoding="utf-8")

    def modify(self, meta, name="app.py", text="VALUE = 2\n"):
        (Path(meta["worktree"]) / name).write_text(text, encoding="utf-8")

    def venv(self, meta):
        venv.EnvBuilder(with_pip=False).create(Path(meta["worktree"]) / ".venv")
        meta["environment_status"] = "ready"
        meta["commands"] = {"check": [sys.executable, "-c",
            "import app, demo, pathlib; assert app.VALUE == 2; assert pathlib.Path(demo.__file__).is_relative_to(pathlib.Path.cwd())"]}
        worktree.save(meta)

    def test_two_tasks_same_ai_and_repeated_thread_have_independent_resources(self):
        one, two = self.task(), self.task("two")
        self.modify(one)
        self.assertEqual((Path(two["worktree"]) / "app.py").read_text(), "VALUE = 1\n")
        self.assertEqual(one["base_sha"], two["base_sha"])
        self.assertNotEqual(one["manifest"], two["manifest"])
        self.assertEqual(worktree.git(self.root, "symbolic-ref", "--short", "HEAD"), "main")

    def test_dirty_source_staged_and_untracked_stay_in_source(self):
        (self.root / "app.py").write_text("SOURCE EDIT\n", encoding="utf-8")
        worktree.git(self.root, "add", "app.py")
        (self.root / "new.txt").write_text("source only", encoding="utf-8")
        before = worktree.git(self.root, "status", "--porcelain")
        meta = self.task()
        self.assertTrue(meta["source_dirty"])
        self.assertEqual((Path(meta["worktree"]) / "app.py").read_text(), "VALUE = 1\n")
        self.assertEqual(worktree.git(self.root, "status", "--porcelain"), before)
        self.assertFalse((Path(meta["worktree"]) / "new.txt").exists())

    def test_detached_head_refuses(self):
        worktree.git(self.root, "checkout", "--detach")
        with self.assertRaisesRegex(worktree.WorktreeError, "detached HEAD"):
            self.task()

    def test_branch_directory_and_manifest_collisions_are_not_overwritten(self):
        worktree.git(self.root, "branch", "agora/branch")
        with self.assertRaisesRegex(worktree.WorktreeError, "已存在"):
            self.task("branch")
        target = self.root / ".agora" / "worktrees" / "folder"
        target.mkdir(parents=True)
        (target / "keep").write_text("keep", encoding="utf-8")
        with self.assertRaisesRegex(worktree.WorktreeError, "已存在"):
            self.task("folder")
        self.assertEqual((target / "keep").read_text(), "keep")
        self.task()
        with self.assertRaisesRegex(worktree.WorktreeError, "已存在"):
            self.task()

    def test_requires_explicit_safe_scope(self):
        for values in (None, ["../app.py"], ["C:/app.py"], ["/app.py"]):
            with self.subTest(values=values), self.assertRaises(worktree.WorktreeError):
                worktree.prepare(self.root, "one", values, self.thread)

    def test_commit_includes_real_check_result_and_only_task_files(self):
        meta, other = self.task(), self.task("other")
        self.modify(meta)
        self.modify(other, text="VALUE = 3\n")
        self.message(meta)
        self.venv(meta)
        result = worktree.finalize(meta, cli.run_task_check)
        self.assertEqual(result["check_result"]["exit_code"], 0)
        self.assertEqual(result["commit_status"], "committed")
        self.assertTrue(Path(result["check_result"]["log"]).exists())
        self.assertIn("Agora-Check: exit=0", worktree.git(Path(meta["worktree"]), "show", "-s", "--format=%B"))
        self.assertEqual(snapshot.branch_changes(self.root, meta["base_sha"], meta["branch"]), [{"status": "M", "path": "app.py"}])
        self.assertEqual(snapshot.branch_changes(self.root, other["base_sha"], other["branch"]), [])
        self.assertEqual((self.root / "app.py").read_text(), "VALUE = 1\n")
        self.assertEqual(worktree.git(Path(meta["worktree"]), "status", "--porcelain"), "")
        with patch.object(cli, "run_task_check") as check:
            worktree.finalize(meta, check)
            check.assert_not_called()

    def test_out_of_scope_and_secret_changes_refuse_before_check(self):
        for i, name in enumerate(("other.py", ".env", ".agora/data.json")):
            meta = self.task(f"out{i}", ["**"] if name != "other.py" else ["app.py"])
            target = Path(meta["worktree"]) / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("blocked", encoding="utf-8")
            if name.startswith(".agora"):
                worktree.git(Path(meta["worktree"]), "add", "-f", name)
            self.message(meta)
            with patch.object(cli, "run_task_check") as check, self.assertRaisesRegex(worktree.WorktreeError, "範圍"):
                worktree.finalize(meta, check)
            check.assert_not_called()

    def test_wrong_dispatch_branch_worktree_and_head_refuse(self):
        meta = self.task()
        for key, value in (("dispatch_id", "wrong"), ("branch", "main"), ("worktree", str(self.root))):
            with self.subTest(key=key), self.assertRaises(worktree.WorktreeError):
                worktree.load({**meta, key: value})
        worktree.git(Path(meta["worktree"]), "checkout", "--detach")
        with self.assertRaises(worktree.WorktreeError):
            worktree.validate(meta)

    def test_missing_message_and_failed_check_do_not_commit(self):
        meta = self.task()
        self.modify(meta)
        with self.assertRaises(OSError):
            worktree.finalize(meta, lambda m: {"exit_code": 0})
        self.message(meta)
        for result in ({"exit_code": 1}, {"exit_code": None}):
            with self.assertRaisesRegex(worktree.WorktreeError, "check 失敗"):
                worktree.finalize(meta, lambda m: result)
            self.assertEqual(worktree.git(Path(meta["worktree"]), "rev-parse", "HEAD"), meta["base_sha"])
            self.assertEqual(meta["check_result"], result)

    def test_check_mutation_cannot_commit_unchecked_content(self):
        meta = self.task()
        self.modify(meta)
        self.message(meta)
        def check(_):
            self.modify(meta, text="VALUE = 7\n")
            return {"exit_code": 0}
        with self.assertRaisesRegex(worktree.WorktreeError, "check 期間"):
            worktree.finalize(meta, check)
        self.assertEqual(worktree.git(Path(meta["worktree"]), "rev-parse", "HEAD"), meta["base_sha"])

    def test_recovery_after_ref_update_does_not_duplicate_commit(self):
        meta = self.task()
        self.modify(meta)
        self.message(meta)
        original = worktree.save
        def crash(m):
            if m.get("commit_sha"):
                raise OSError("simulated crash")
            original(m)
        with patch.object(worktree, "save", side_effect=crash), self.assertRaises(OSError):
            worktree.finalize(meta, lambda m: {"exit_code": 0})
        persisted = json.loads(Path(meta["manifest"]).read_text(encoding="utf-8"))
        head = worktree.git(Path(meta["worktree"]), "rev-parse", "HEAD")
        with patch.object(cli, "run_task_check") as check:
            worktree.finalize(persisted, check)
            check.assert_not_called()
        self.assertEqual(persisted["commit_sha"], head)
        self.assertEqual(worktree.git(Path(meta["worktree"]), "rev-list", "--count", f"{meta['base_sha']}..HEAD"), "1")

    def test_own_venv_imports_and_stale_editable_are_detected(self):
        meta = self.task()
        with self.assertRaisesRegex(worktree.WorktreeError, "缺少"):
            worktree.verify_environment(meta)
        self.venv(meta)
        with patch.dict(os.environ, {"PYTHONPATH": str(self.root)}):
            worktree.verify_environment(meta)
            self.assertNotIn("PYTHONPATH", worktree.environment(meta))
        target = Path(meta["worktree"])
        site = target / ".venv" / ("Lib/site-packages" if os.name == "nt" else f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages")
        dist = site / "bad-1.0.dist-info"
        dist.mkdir()
        (dist / "METADATA").write_text("Name: bad\nVersion: 1.0\n", encoding="utf-8")
        (dist / "direct_url.json").write_text(json.dumps({"url": self.root.as_uri(), "dir_info": {"editable": True}}), encoding="utf-8")
        with self.assertRaisesRegex(worktree.WorktreeError, "editable install outside"):
            worktree.verify_environment(meta)

    def test_stale_package_copy_inside_venv_cannot_replace_worktree_source(self):
        meta = self.task()
        self.venv(meta)
        target = Path(meta["worktree"])
        site = target / ".venv" / ("Lib/site-packages" if os.name == "nt" else f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages")
        stale = site / "demo"
        stale.mkdir()
        (stale / "__init__.py").write_text("VALUE = 0\n", encoding="utf-8")
        (target / "demo" / "__init__.py").unlink()
        with self.assertRaisesRegex(worktree.WorktreeError, "module outside worktree source"):
            worktree.verify_environment(meta)

    def test_control_config_and_progress_remain_at_controller(self):
        meta = self.task()
        config.save(self.root, {"check": ["python", "-c", "pass"]})
        with patch.dict(os.environ, worktree.environment(meta)):
            ws = cli.Workspace(cli.resolve_workspace(None))
            self.assertEqual(ws.root, Path(meta["worktree"]))
            self.assertEqual(ws.threads, self.root / ".agora" / "threads")
            self.assertEqual(ws.config(), config.load(self.root))

    def test_prune_preserves_unmerged_and_running_completed_tasks(self):
        meta = self.task()
        state = {"relay": {**meta, "status": "done"}}
        with patch.object(prune.jobs, "alive", return_value=True):
            self.assertIn("尚未退出", prune.why_keep(self.thread, state, None))
        with patch.object(prune.jobs, "alive", return_value=False):
            self.assertIn("保留", prune.why_keep(self.thread, state, None))
        cli.save_state(self.thread, state)
        self.assertTrue(prune.delete_thread(self.thread, {"sessions": []}, False)["errors"])
        self.assertTrue(self.thread.exists())

    def test_environment_creation_failure_keeps_resources(self):
        meta = self.task()
        with patch.object(worktree.venv.EnvBuilder, "create", side_effect=OSError("locked")):
            with self.assertRaisesRegex(OSError, "locked"):
                worktree.setup_environment(meta)
        self.assertEqual(meta["environment_status"], "failed")
        self.assertTrue(Path(meta["worktree"]).exists())
        self.assertTrue(Path(meta["manifest"]).exists())

    def assign_task(self, subagents=None, fail_spawn=False):
        config.save(self.root, {"parties": {"codex": {"subagents": True}, "antigravity": {"enabled": False}}})
        args = argparse.Namespace(thread="t1", sender="claude", to=None, message="Fix app", file=None,
            session=None, from_summary=False, yes=False, tier=None, model=None, effort=None,
            subagents=subagents, trigger=None, allow=["app.py"], dispatch_id="worker")
        ws = cli.Workspace(self.root)
        with patch.dict(os.environ, {cli.NESTED_ENV: ""}), patch("sys.stdout", io.StringIO()), \
                patch.object(cli, "party_usage", return_value={"remaining_pct": 80}), \
                patch.object(cli.quota, "blocked", return_value=None), \
                patch.object(cli.models, "CACHE", self.root / "models.json"), \
                patch.object(cli.jobs, "spawn", return_value=99999999,
                             side_effect=OSError("locked") if fail_spawn else None), \
                patch.object(cli.jobs, "pid_started", return_value=None):
            cli.cmd_assign(ws, args)
        return ws, cli.load_state(self.thread)["relay"]

    def test_worker_runs_in_task_but_controls_in_main_and_commits_after_return(self):
        ws, relay = self.assign_task()
        meta = worktree.load(relay)
        self.venv(meta)
        seen = {}
        def worker(party, prompt, session, opts, is_work, cwd, timeout, scratch, live=None):
            seen.update(opts=opts, cwd=cwd, scratch=scratch, prompt=prompt)
            self.assertEqual(worktree.git(cwd, "rev-parse", "HEAD"), relay["base_sha"])
            self.modify(meta)
            self.message(meta)
            return "Done\n【工作完成】", "session", "ok"
        with patch.object(cli, "party_usage", return_value={"remaining_pct": 80}), \
                patch.object(cli, "call_party", side_effect=worker), patch("sys.stdout", io.StringIO()):
            cli.cmd_work(ws, argparse.Namespace(thread="t1", dispatch_id="worker"))
        self.assertEqual(seen["cwd"], Path(relay["worktree"]))
        self.assertEqual(seen["scratch"], self.thread)
        self.assertEqual(seen["opts"]["control_dir"], str(self.thread))
        self.assertFalse(seen["opts"]["subagents"])
        self.assertNotIn("能獨立進行的子工作", seen["prompt"])
        done = cli.load_state(self.thread)["relay"]
        self.assertEqual(done["status"], "done")
        self.assertEqual(done["commit_status"], "committed")
        self.assertEqual(done["check_result"]["exit_code"], 0)
        out = io.StringIO()
        with patch("sys.stdout", out):
            cli.cmd_status(ws, argparse.Namespace(thread="t1", wait=None, json=True))
        status = json.loads(out.getvalue())
        self.assertEqual(status["commit_sha"], done["commit_sha"])
        self.assertEqual(status["branch"], "agora/worker")

    def test_explicit_subagents_adds_guidance_and_stop_prevents_commit(self):
        ws, relay = self.assign_task("on")
        meta = worktree.load(relay)
        self.venv(meta)
        def worker(party, prompt, session, opts, *args, **kw):
            self.assertTrue(opts["subagents"])
            self.assertIn("能獨立進行的子工作（大量搜尋、讀檔、互不重疊的修改）用子代理並行，小工作自己做", prompt)
            self.modify(meta)
            self.message(meta)
            (self.thread / "control.json").write_text('{"stop_requested":true}', encoding="utf-8")
            return "Done\n【工作完成】", "session", "ok"
        with patch.object(cli, "party_usage", return_value={"remaining_pct": 80}), \
                patch.object(cli, "call_party", side_effect=worker), patch("sys.stdout", io.StringIO()):
            cli.cmd_work(ws, argparse.Namespace(thread="t1", dispatch_id="worker"))
        self.assertEqual(cli.load_state(self.thread)["relay"]["status"], "stopped")
        self.assertEqual(worktree.git(Path(meta["worktree"]), "rev-parse", "HEAD"), meta["base_sha"])

    def test_stop_arriving_during_check_blocks_commit(self):
        meta = self.task()
        self.modify(meta)
        self.message(meta)
        with self.assertRaisesRegex(worktree.WorktreeError, "要求停止"):
            worktree.finalize(meta, lambda m: {"exit_code": 0}, lambda: False)
        self.assertIsNone(meta["commit_sha"])

    def test_frozen_command_paths_map_once_and_use_task_python(self):
        meta = self.task()
        command = worktree.rebase_command({"check": [sys.executable, str(self.root / "app.py")]}, meta)
        self.assertEqual(command["check"], [str(worktree.python_path(Path(meta["worktree"]))),
                                            str(Path(meta["worktree"]) / "app.py")])
        shell = worktree.rebase_command('"' + sys.executable + '" -m unittest', meta)
        self.assertEqual(shell, '"' + str(worktree.python_path(Path(meta["worktree"]))) + '" -m unittest')

    def test_cli_control_directory_is_added_to_each_party(self):
        meta = self.task()
        opts = {"control_dir": str(self.thread), "add_dirs": [str(self.thread)], "subagents": False}
        for caller in (parties.call_codex, parties.call_claude, parties.call_antigravity):
            with self.subTest(caller=caller.__name__), patch.object(parties, "_run", return_value=([
                    {"type": "result", "result": "done", "session_id": "s"},
                    {"event": "result", "result": {"status": "SUCCESS", "response": "done", "conversation_id": "s"}},
                    {"type": "item.completed", "item": {"type": "agent_message", "text": "done"}}], "")) as run, \
                    patch.object(parties.quota, "find_codex", return_value="codex"), \
                    patch.object(parties.quota, "find_claude", return_value="claude"):
                caller("test", None, opts, True, Path(meta["worktree"]), 10, self.thread)
            self.assertIn(str(self.thread), run.call_args.args[0])

    def test_changes_can_select_previous_dispatch_on_same_thread(self):
        one = self.task()
        self.modify(one)
        self.message(one)
        worktree.finalize(one, lambda m: {"exit_code": 0})
        two = self.task("two")
        cli.save_state(self.thread, {"relay": two})
        out = io.StringIO()
        with patch("sys.stdout", out):
            cli.cmd_changes(cli.Workspace(self.root), argparse.Namespace(thread="t1", json=True, dispatch_id="one"))
        result = json.loads(out.getvalue())
        self.assertEqual(result["branch"], one["branch"])
        self.assertEqual([f["path"] for f in result["files"]], ["app.py"])

    def test_spawn_failure_keeps_resources_and_same_name_cannot_be_reused(self):
        with self.assertRaisesRegex(SystemExit, "無法啟動"):
            self.assign_task(fail_spawn=True)
        relay = cli.load_state(self.thread)["relay"]
        meta = worktree.load(relay)
        self.assertEqual(relay["status"], "failed")
        with self.assertRaisesRegex(worktree.WorktreeError, "已存在"):
            self.task("worker")
        self.assertTrue(Path(meta["worktree"]).is_dir())

    def test_worker_recovers_published_commit_without_calling_ai(self):
        ws, relay = self.assign_task()
        meta = worktree.load(relay)
        self.modify(meta)
        self.message(meta)
        worktree.finalize(meta, lambda m: {"exit_code": 0})
        expected = meta["commit_sha"]
        meta.update(commit_sha=None, commit_status="checked")
        worktree.save(meta)   # simulate a crash between update-ref and saving its SHA
        with patch.object(cli, "call_party") as call:
            cli.cmd_work(ws, argparse.Namespace(thread="t1", dispatch_id="worker"))
        call.assert_not_called()
        self.assertEqual(cli.load_state(self.thread)["relay"]["commit_sha"], expected)
        self.assertEqual(cli.load_state(self.thread)["relay"]["status"], "done")

    def test_base_excluded_files_are_preserved_without_being_read_or_restaged(self):
        (self.root / "baseline.key").write_text("synthetic test fixture", encoding="utf-8")
        worktree.git(self.root, "add", "baseline.key")
        worktree.git(self.root, "commit", "-m", "fixture")
        meta = self.task()
        self.modify(meta)
        self.message(meta)
        worktree.finalize(meta, lambda m: {"exit_code": 0})
        self.assertEqual(worktree.git(Path(meta["worktree"]), "diff", "--name-only", meta["base_sha"], meta["commit_sha"]), "app.py")

    def test_syntax_check_skips_worktree_venv_and_handles_spaces(self):
        name = "module with spaces.py"
        (self.root / name).write_text("VALUE = 1\n", encoding="utf-8")
        ignored = self.root / ".venv"
        ignored.mkdir()
        (ignored / "broken.py").write_text("not valid python !", encoding="utf-8")
        config.save(self.root, {"check": [sys.executable, "-c", "pass"]})
        with patch("sys.stdout", io.StringIO()) as out, self.assertRaises(SystemExit) as exit:
            cli.cmd_check(cli.Workspace(self.root), argparse.Namespace())
        self.assertEqual(exit.exception.code, 0)
        self.assertIn("0 個錯誤", out.getvalue())

    def test_worker_environment_keeps_nested_guard_and_removes_pythonpath(self):
        meta = self.task()
        with patch.dict(os.environ, {cli.NESTED_ENV: "", "PYTHONPATH": str(self.root)}):
            events, _ = parties._run([sys.executable, "-c",
                "import os,json; print(json.dumps({'invoked':os.getenv('AGORA_INVOKED'), 'path':os.getenv('PYTHONPATH')}))"],
                Path(meta["worktree"]), None, 5, extra_env=worktree.environment(meta))
        self.assertEqual(events[-1], {"invoked": "1", "path": None})


if __name__ == "__main__":
    unittest.main()


class TestCheckTask(unittest.TestCase):
    def test_check_task_requires_worktree_dispatch(self):
        import tempfile
        from agoralib import cli
        with tempfile.TemporaryDirectory() as tmp:
            ws = cli.Workspace(Path(tmp))
            thread = ws.threads / "t1"
            thread.mkdir(parents=True)
            cli.save_state(thread, {"topic": "t", "parties": {}, "messages": [], "relay": {"worker": "codex"}})
            with self.assertRaises(SystemExit) as ctx:
                cli.cmd_check(ws, type("Args", (), {"task": "t1"})())
            self.assertIn("不是隔離 worktree 分派", str(ctx.exception.code))
