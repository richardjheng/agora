"""跨分派 check 排隊與槽位回收測試（含多程序驗證；以事件檔同步，不依賴短秒數斷言）。"""

import argparse
import io
import json
import multiprocessing
import os
import sys
import tempfile
import time
import unittest
import venv
from pathlib import Path
from unittest.mock import patch

from agoralib import checkqueue, cli, config, worktree


def _wait_until(predicate, timeout=30.0, interval=0.05, message="等待條件逾時"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise AssertionError(message)


def _slot_worker(control_root: str, label: str, entered_file: str, release_file: str,
                 log_file: str, order_file: str = "", poll_interval: float = 0.05,
                 report_interval: float = 0.2):
    """子程序：取得槽位後寫入 entered_file，等到 release_file 出現才釋放。"""
    lines = []

    def emit(msg: str):
        lines.append(msg)
        Path(log_file).write_text("\n".join(lines) + "\n", encoding="utf-8")

    with checkqueue.acquire_slot(
        Path(control_root),
        label=label,
        poll_interval=poll_interval,
        report_interval=report_interval,
        output=emit,
    ):
        if order_file:
            with open(order_file, "a", encoding="utf-8") as f:
                f.write(label + "\n")
                f.flush()
        Path(entered_file).write_text(str(os.getpid()), encoding="utf-8")
        deadline = time.monotonic() + 30.0
        while not Path(release_file).exists():
            if time.monotonic() >= deadline:
                raise TimeoutError(f"{label} 等待 release 旗標逾時")
            time.sleep(0.05)


def _cmd_check_worker(work_root: str, env_overrides: dict, out_file: str):
    """子程序：執行 cli.cmd_check，將 stdout/stderr 與 exit code 寫入 out_file。"""
    buf = io.StringIO()
    code = 0
    with patch.dict(os.environ, env_overrides), patch("sys.stdout", buf), patch("sys.stderr", buf):
        try:
            ws = cli.Workspace(Path(work_root))
            cli.cmd_check(ws, argparse.Namespace(task=None))
        except SystemExit as exc:
            if isinstance(exc.code, int):
                code = exc.code
            else:
                if exc.code:
                    buf.write(str(exc.code) + "\n")
                code = 1
    Path(out_file).write_text(json.dumps({"code": code, "output": buf.getvalue()}, ensure_ascii=False), encoding="utf-8")


class TestCheckConcurrencyConfig(unittest.TestCase):
    def test_defaults_and_invalid_values_fall_back_to_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(config.check_concurrency(root), 1)
            for valid in (1, 2, 4):
                config.save(root, {"check_concurrency": valid})
                self.assertEqual(config.check_concurrency(root), valid)
            for invalid in (0, -1, -10, True, False, "2", 1.5, None, [], {}):
                with self.subTest(value=invalid):
                    config.save(root, {"check_concurrency": invalid})
                    self.assertEqual(config.check_concurrency(root), 1)


class TestCheckQueueUnit(unittest.TestCase):
    def test_releases_slot_on_exception_keyboard_interrupt_and_system_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            slots = checkqueue.slots_dir(root)
            for exc in (RuntimeError("boom"), KeyboardInterrupt(), SystemExit(2)):
                with self.subTest(exc=type(exc).__name__):
                    with self.assertRaises(type(exc)):
                        with checkqueue.acquire_slot(root, label="err-test", output=lambda _: None) as info:
                            self.assertTrue(info["slot_path"].exists())
                            raise exc
                    self.assertEqual(list(slots.glob("slot-*.json")), [])
                    self.assertEqual(list(slots.glob("wait-*.json")), [])
            # 釋放後可立刻重新取得
            with checkqueue.acquire_slot(root, label="after-err", output=lambda _: None) as info:
                self.assertTrue(info["slot_path"].exists())
            self.assertEqual(list(slots.glob("slot-*.json")), [])

    def test_reap_never_touches_git_index_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            git_lock = root / ".git" / "index.lock"
            git_lock.parent.mkdir(parents=True)
            git_lock.write_text("locked", encoding="utf-8")
            old = time.time() - 86400
            os.utime(git_lock, (old, old))
            with checkqueue.acquire_slot(root, label="git-safe", output=lambda _: None):
                pass
            self.assertTrue(git_lock.exists())

    def test_prompts_and_skills_warn_not_to_interrupt_check(self):
        self.assertIn("不要中斷", cli.WORK_PREAMBLE)
        self.assertIn("不要中斷", cli.CONTINUE_PROMPT)
        repo_root = Path(__file__).resolve().parent.parent
        for rel in ("skills/claude/agora/SKILL.md", "skills/agents/agora/SKILL.md"):
            text = (repo_root / rel).read_text(encoding="utf-8")
            self.assertIn("check_concurrency", text, rel)
            self.assertIn("不要中斷", text, rel)

    def test_cmd_check_permission_error_reports_clear_message_and_nonzero_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            config.save(root, {"check": [sys.executable, "-c", "pass"]})
            ws = cli.Workspace(root)
            slots = checkqueue.slots_dir(root)
            buf = io.StringIO()
            with patch.dict(os.environ, {}, clear=False), \
                    patch("sys.stdout", buf), patch("sys.stderr", buf), \
                    patch.object(checkqueue, "_queue_lock",
                                 side_effect=PermissionError(13, "Permission denied", str(slots / "queue.lock"))):
                os.environ.pop(worktree.MANIFEST_ENV, None)
                os.environ.pop(cli.LOCKED_ENV, None)
                with self.assertRaises(SystemExit) as ctx:
                    cli.cmd_check(ws, argparse.Namespace(task=None))
            msg = str(ctx.exception.code)
            self.assertNotEqual(ctx.exception.code, 0)
            self.assertIn("驗證槽位目錄不可寫", msg)
            self.assertIn(str(slots), msg)
            self.assertIn("未執行測試", msg)
            self.assertNotIn("驗證結果：exit=", buf.getvalue())


class TestCheckQueueMultiprocess(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.flags = self.root / "flags"
        self.flags.mkdir()
        self.ctx = multiprocessing.get_context("spawn")
        self.procs = []

    def tearDown(self):
        for p in self.procs:
            if p.is_alive():
                p.kill()
            p.join(5)

    def _start_worker(self, label: str, order_file: str = ""):
        entered = self.flags / f"entered-{label}"
        release = self.flags / f"release-{label}"
        log = self.flags / f"log-{label}.txt"
        p = self.ctx.Process(
            target=_slot_worker,
            args=(str(self.root), label, str(entered), str(release), str(log), order_file),
        )
        p.start()
        self.procs.append(p)
        return p, entered, release, log

    def test_concurrency_one_and_strict_fifo_order(self):
        config.save(self.root, {"check_concurrency": 1})
        slots = checkqueue.slots_dir(self.root)
        order_file = self.flags / "order.txt"

        p1, e1, r1, l1 = self._start_worker("w1", str(order_file))
        _wait_until(lambda: e1.exists(), message="w1 未取得槽位")

        p2, e2, r2, l2 = self._start_worker("w2", str(order_file))
        _wait_until(lambda: len(list(slots.glob("wait-*.json"))) == 1, message="w2 未入隊")

        p3, e3, r3, l3 = self._start_worker("w3", str(order_file))
        _wait_until(lambda: len(list(slots.glob("wait-*.json"))) == 2, message="w3 未入隊")

        p4, e4, r4, l4 = self._start_worker("w4", str(order_file))
        _wait_until(lambda: len(list(slots.glob("wait-*.json"))) == 3, message="w4 未入隊")

        # 同時最多 1 個在執行
        self.assertEqual(len(list(slots.glob("slot-*.json"))), 1)
        self.assertFalse(e2.exists())
        self.assertFalse(e3.exists())
        self.assertFalse(e4.exists())
        _wait_until(lambda: l3.exists() and "等待驗證中（第 2 位，前面：" in l3.read_text(encoding="utf-8"),
                    message="w3 未印出等待訊息")

        r1.write_text("go", encoding="utf-8")
        p1.join(15)
        self.assertEqual(p1.exitcode, 0)
        _wait_until(lambda: e2.exists(), message="w2 未依 FIFO 取得槽位")
        self.assertEqual(len(list(slots.glob("slot-*.json"))), 1)
        self.assertFalse(e3.exists())
        self.assertFalse(e4.exists())

        r2.write_text("go", encoding="utf-8")
        p2.join(15)
        self.assertEqual(p2.exitcode, 0)
        _wait_until(lambda: e3.exists(), message="w3 未依 FIFO 取得槽位")
        self.assertFalse(e4.exists())

        r3.write_text("go", encoding="utf-8")
        p3.join(15)
        self.assertEqual(p3.exitcode, 0)
        _wait_until(lambda: e4.exists(), message="w4 未依 FIFO 取得槽位")

        r4.write_text("go", encoding="utf-8")
        p4.join(15)
        self.assertEqual(p4.exitcode, 0)

        self.assertEqual(order_file.read_text(encoding="utf-8").splitlines(), ["w1", "w2", "w3", "w4"])
        self.assertIn("開始驗證（", l1.read_text(encoding="utf-8"))
        self.assertIn("開始驗證（", l2.read_text(encoding="utf-8"))
        self.assertEqual(list(slots.glob("slot-*.json")), [])
        self.assertEqual(list(slots.glob("wait-*.json")), [])

    def test_concurrency_two_allows_two_and_queues_third(self):
        config.save(self.root, {"check_concurrency": 2})
        slots = checkqueue.slots_dir(self.root)

        p1, e1, r1, _ = self._start_worker("c1")
        p2, e2, r2, _ = self._start_worker("c2")
        _wait_until(lambda: e1.exists() and e2.exists(), message="上限 2 時 c1/c2 應同時執行")
        self.assertEqual(len(list(slots.glob("slot-*.json"))), 2)

        p3, e3, r3, l3 = self._start_worker("c3")
        _wait_until(lambda: len(list(slots.glob("wait-*.json"))) == 1, message="c3 應排隊等待")
        self.assertFalse(e3.exists())
        self.assertEqual(len(list(slots.glob("slot-*.json"))), 2)

        r1.write_text("go", encoding="utf-8")
        p1.join(15)
        self.assertEqual(p1.exitcode, 0)
        _wait_until(lambda: e3.exists(), message="c1 釋放後 c3 應立即接手")
        self.assertEqual(len(list(slots.glob("slot-*.json"))), 2)

        r2.write_text("go", encoding="utf-8")
        r3.write_text("go", encoding="utf-8")
        p2.join(15)
        p3.join(15)
        self.assertEqual(p2.exitcode, 0)
        self.assertEqual(p3.exitcode, 0)
        self.assertIn("等待驗證中（第 1 位，前面：", l3.read_text(encoding="utf-8"))
        self.assertEqual(list(slots.glob("slot-*.json")), [])
        self.assertEqual(list(slots.glob("wait-*.json")), [])

    def test_dead_holder_slot_is_reclaimed_after_kill(self):
        config.save(self.root, {"check_concurrency": 1})
        slots = checkqueue.slots_dir(self.root)

        p1, e1, _, _ = self._start_worker("holder")
        _wait_until(lambda: e1.exists(), message="holder 未取得槽位")

        p2, e2, r2, _ = self._start_worker("waiter")
        _wait_until(lambda: len(list(slots.glob("wait-*.json"))) == 1, message="waiter 未入隊")
        self.assertFalse(e2.exists())

        p1.kill()
        p1.join(15)

        _wait_until(lambda: e2.exists(), message="holder 被 kill 後槽位未被回收給 waiter")
        r2.write_text("go", encoding="utf-8")
        p2.join(15)
        self.assertEqual(p2.exitcode, 0)
        self.assertEqual(list(slots.glob("slot-*.json")), [])
        self.assertEqual(list(slots.glob("wait-*.json")), [])


class TestCheckQueueWorktreeIntegration(unittest.TestCase):
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
        worktree.git(self.root, "add", ".")
        worktree.git(self.root, "commit", "-m", "base")
        self.thread = self.root / ".agora" / "threads" / "t1"
        self.thread.mkdir(parents=True)
        (self.thread / "control.json").write_text(json.dumps({"stop_requested": False}), encoding="utf-8")
        cli.save_state(self.thread, {"topic": "queue-test", "parties": {}, "messages": []})
        (self.thread / "transcript.md").write_text("", encoding="utf-8")
        self.ctx = multiprocessing.get_context("spawn")
        self.procs = []

    def tearDown(self):
        for p in self.procs:
            if p.is_alive():
                p.kill()
            p.join(5)

    def _prepare_task(self, dispatch_id="task1"):
        meta = worktree.prepare(self.root, dispatch_id, ["app.py"], self.thread)
        venv.EnvBuilder(with_pip=False).create(Path(meta["worktree"]) / ".venv")
        meta["environment_status"] = "ready"
        meta["commands"] = {"check": [sys.executable, "-c", "import app; assert app.VALUE == 1"]}
        worktree.save(meta)
        return meta

    def test_run_task_check_does_not_deadlock_and_uses_control_root_slots(self):
        config.save(self.root, {"check_concurrency": 1})
        meta = self._prepare_task("no-deadlock")
        result = cli.run_task_check(meta)
        self.assertEqual(result["exit_code"], 0)
        log = Path(result["log"]).read_text(encoding="utf-8")
        self.assertIn("開始驗證（", log)
        slots = checkqueue.slots_dir(self.root)
        self.assertTrue(slots.is_dir())
        self.assertEqual(list(slots.glob("slot-*.json")), [])
        self.assertEqual(list(slots.glob("wait-*.json")), [])
        # worktree 內不應各自建立獨立 check-slots
        self.assertFalse(checkqueue.slots_dir(Path(meta["worktree"])).exists())

    def test_stop_requested_aborts_waiting_cmd_check_with_distinct_message(self):
        config.save(self.root, {"check_concurrency": 1})
        meta = self._prepare_task("stop-wait")
        slots = checkqueue.slots_dir(self.root)
        flags = self.root / "flags"
        flags.mkdir()
        entered = flags / "entered-blocker"
        release = flags / "release-blocker"
        blocker_log = flags / "blocker.txt"

        blocker = self.ctx.Process(
            target=_slot_worker,
            args=(str(self.root), "blocker", str(entered), str(release), str(blocker_log)),
        )
        blocker.start()
        self.procs.append(blocker)
        _wait_until(lambda: entered.exists(), message="blocker 未取得槽位")

        out_file = flags / "check-out.json"
        env = worktree.environment(meta)
        env[cli.LOCKED_ENV] = json.dumps(meta["commands"], ensure_ascii=False)
        waiter = self.ctx.Process(target=_cmd_check_worker, args=(meta["worktree"], env, str(out_file)))
        waiter.start()
        self.procs.append(waiter)

        _wait_until(lambda: len(list(slots.glob("wait-*.json"))) == 1, message="cmd_check 未進入排隊")

        # 寫入 stop_requested，不釋放 blocker，等待中的 cmd_check 應主動結束
        (self.thread / "control.json").write_text(json.dumps({"stop_requested": True}), encoding="utf-8")
        waiter.join(15)
        self.assertEqual(waiter.exitcode, 0)  # _cmd_check_worker 正常捕獲 SystemExit 並記錄 exit code
        data = json.loads(out_file.read_text(encoding="utf-8"))
        self.assertNotEqual(data["code"], 0)
        self.assertIn("stop_requested", data["output"])
        self.assertIn("未執行測試", data["output"])
        self.assertNotIn("驗證結果：exit=", data["output"])
        self.assertEqual(list(slots.glob("wait-*.json")), [])

        release.write_text("go", encoding="utf-8")
        blocker.join(15)
        self.assertEqual(blocker.exitcode, 0)
        self.assertEqual(list(slots.glob("slot-*.json")), [])


if __name__ == "__main__":
    unittest.main()
