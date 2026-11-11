"""程序終止測試：不呼叫 AI CLI、不連網，涵蓋 taskkill 失敗與繼承的 pipe。"""

import io
import os
import signal
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agoralib import jobs, parties  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]
DENIED = subprocess.CompletedProcess(["taskkill"], 1, "", "Access is denied (sandbox probe)")


class TestTermination(unittest.TestCase):
    def test_taskkill_success_does_not_use_fallback(self):
        with patch.object(sys, "platform", "win32"), \
                patch.object(jobs.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run, \
                patch.object(jobs, "_kill_windows_fallback") as fallback:
            self.assertEqual(jobs.kill_tree(123), "")
        fallback.assert_not_called()
        self.assertEqual(run.call_args.args[0], ["taskkill", "/pid", "123", "/T", "/F"])
        self.assertEqual(run.call_args.kwargs["timeout"], 2)

    def test_taskkill_errors_are_preserved_and_fallback_is_called(self):
        for failure in (DENIED, FileNotFoundError("taskkill missing"), subprocess.TimeoutExpired("taskkill", 2)):
            with self.subTest(failure=failure):
                kwargs = {"side_effect": failure} if isinstance(failure, Exception) else {"return_value": failure}
                proc = Mock()
                with patch.object(sys, "platform", "win32"), \
                        patch.object(jobs.subprocess, "run", **kwargs), \
                        patch.object(jobs, "_kill_windows_fallback", return_value=[]) as fallback, \
                        patch("sys.stderr", io.StringIO()) as err:
                    detail = jobs.kill_tree(123, proc)
                fallback.assert_called_once_with(123, proc)
                self.assertIn("終止失敗：taskkill", detail)
                self.assertIn("備援終止", detail)
                self.assertEqual(err.getvalue().strip(), detail)
                if failure is DENIED:
                    self.assertIn("exit=1", detail)
                    self.assertIn("Access is denied", detail)

    def test_fallback_failure_is_visible(self):
        with patch.object(sys, "platform", "win32"), \
                patch.object(jobs.subprocess, "run", return_value=DENIED), \
                patch.object(jobs, "_kill_windows_fallback", return_value=["TerminateProcess: denied"]), \
                patch("sys.stderr", io.StringIO()):
            detail = jobs.kill_tree(123)
        self.assertIn("終止失敗：備援終止：TerminateProcess: denied", detail)

    @unittest.skipUnless(sys.platform.startswith("win"), "Win32 備援的真實程序測試")
    def test_failed_taskkill_really_calls_proc_kill_and_preserves_reason(self):
        popen = subprocess.Popen
        processes = []

        def start(*args, **kwargs):
            proc = popen(*args, **kwargs)
            processes.append(proc)
            proc.kill = Mock(wraps=proc.kill)
            return proc

        begin = time.monotonic()
        with patch.object(parties.subprocess, "Popen", side_effect=start), \
                patch.object(jobs.subprocess, "run", return_value=DENIED), patch("sys.stderr", io.StringIO()):
            with self.assertRaises(subprocess.TimeoutExpired) as caught:
                parties._run([sys.executable, "-c", "import time; time.sleep(30)"], ROOT, "", -59.5)
        self.assertLess(time.monotonic() - begin, 8)
        self.assertIsNotNone(processes[0].poll())
        processes[0].kill.assert_called_once()
        self.assertIn("終止失敗", str(caught.exception))
        self.assertIn("Access is denied", str(caught.exception))
        self.assertIn("Access is denied", caught.exception.stderr)

    @unittest.skipUnless(sys.platform.startswith("win"), "Win32 程序樹的真實測試")
    def test_fallback_kills_descendant_holding_pipes(self):
        seen = []
        script = ("import subprocess,sys,time,json\n"
                  "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
                  "print(json.dumps({'child': child.pid}), flush=True)\n"
                  "time.sleep(30)\n")
        begin = time.monotonic()
        try:
            with patch.object(jobs.subprocess, "run", return_value=DENIED), patch("sys.stderr", io.StringIO()):
                with self.assertRaises(subprocess.TimeoutExpired) as caught:
                    parties._run([sys.executable, "-c", script], ROOT, "", -59, seen.append)
            self.assertLess(time.monotonic() - begin, 8)
            self.assertEqual(len(seen), 1)
            self.assertFalse(jobs.pid_alive(seen[0]["child"]), str(caught.exception))
            self.assertNotIn("仍被程序占用", str(caught.exception))
        finally:
            for event in seen:
                if jobs.pid_alive(event["child"]):
                    os.kill(event["child"], signal.SIGTERM)

    def test_timeout_returns_even_when_reader_never_gets_eof(self):
        # 不啟動長命孤兒：reader 模擬孫程序持有 stdout 的 read lock。
        release = threading.Event()
        closed = threading.Event()

        class HeldPipe:
            def __iter__(self):
                release.wait(10)
                return iter(())

            def close(self):
                closed.set()

        proc = Mock(pid=123, stdin=io.StringIO(), stdout=HeldPipe(), stderr=io.StringIO("stderr before timeout\n"))
        proc.poll.return_value = None
        proc.wait.side_effect = subprocess.TimeoutExpired("probe", 0)
        begin = time.monotonic()
        try:
            with patch.object(parties.subprocess, "Popen", return_value=proc), \
                    patch.object(jobs, "kill_tree", return_value="終止失敗：probe denied"):
                with self.assertRaises(subprocess.TimeoutExpired) as caught:
                    parties._run(["probe"], ROOT, "", -59.9)
            self.assertLess(time.monotonic() - begin, 4)
            self.assertFalse(closed.is_set(), "不可在 reader 卡住時從主執行緒 close")
            self.assertIn("probe denied", str(caught.exception))
            self.assertIn("仍被程序占用", str(caught.exception))
            self.assertIn("根程序仍未結束", str(caught.exception))
            self.assertIn("stderr before timeout", caught.exception.stderr)
        finally:
            release.set()
            self.assertTrue(closed.wait(2))

    def test_stdout_eof_does_not_disable_process_timeout(self):
        # CLI 關掉 stdout 後仍在執行，wait() 也必須受同一個截止時間保護。
        begin = time.monotonic()
        with patch("sys.stderr", io.StringIO()):
            with self.assertRaises(subprocess.TimeoutExpired):
                parties._run([sys.executable, "-c", "import os,time; os.close(1); time.sleep(30)"],
                             ROOT, "", -59.5)
        self.assertLess(time.monotonic() - begin, 8)


if __name__ == "__main__":
    unittest.main()


class TestProcessIdentity(unittest.TestCase):
    """找後代時持有 handle 並重新快照確認父子關係：快照後 PID 被無關程序重用時，不能把新程序當成後代"""

    def kernel(self, processes):
        """processes：{PID: (建立時間, 父 PID)}，可在測試中途替換，模擬 PID 被重複使用；handle 綁定開啟當下的程序"""
        class Handle:
            def __init__(self, pid, created):
                self.pid, self.created = pid, created

        class K:
            closed = []

            def OpenProcess(self, access, inherit, pid):
                return Handle(pid, processes[pid][0]) if pid in processes else None

            def GetProcessTimes(self, handle, creation, *rest):
                ticks = int((handle.created + 11644473600) * 1e7)
                creation._obj.dwLowDateTime = ticks & 0xFFFFFFFF
                creation._obj.dwHighDateTime = ticks >> 32
                return 1

            def CloseHandle(self, handle):
                K.closed.append(handle)
        return K()

    @unittest.skipUnless(sys.platform.startswith("win"), "ctypes wintypes 只在 Windows")
    def test_real_descendants_are_kept(self):
        from agoralib import jobs
        procs = {1: (100.0, 0), 10: (150.0, 1), 11: (160.0, 10)}
        parents = {p: v[1] for p, v in procs.items()}
        targets, root = jobs._collect_targets(self.kernel(procs), 1, parents, lambda: dict(parents))
        self.assertEqual([p for p, _ in targets], [11, 10])   # 由深到淺
        self.assertEqual(root.pid, 1)

    @unittest.skipUnless(sys.platform.startswith("win"), "ctypes wintypes 只在 Windows")
    def test_pid_reused_between_snapshot_and_open_is_skipped(self):
        from agoralib import jobs
        procs = {1: (100.0, 0), 10: (150.0, 1), 11: (160.0, 10)}
        parents = {p: v[1] for p, v in procs.items()}         # 第一次快照：10 是 1 的子程序
        procs[10] = (200.0, 99)                                # 開啟前 10 已結束，PID 被無關的新程序（父 99）重用
        targets, _ = jobs._collect_targets(self.kernel(procs), 1, parents,
                                           lambda: {p: v[1] for p, v in procs.items()})
        self.assertEqual(targets, [])                          # 新程序與原本的孫程序 11 都不終止

    @unittest.skipUnless(sys.platform.startswith("win"), "ctypes wintypes 只在 Windows")
    def test_older_process_with_stale_parent_pid_is_skipped(self):
        from agoralib import jobs
        procs = {1: (100.0, 0), 10: (50.0, 1)}                 # 10 比父程序更早建立：記錄的父 PID 是舊程序留下的
        parents = {p: v[1] for p, v in procs.items()}
        targets, _ = jobs._collect_targets(self.kernel(procs), 1, parents, lambda: dict(parents))
        self.assertEqual(targets, [])
