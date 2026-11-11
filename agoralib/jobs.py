"""
背景執行：討論（send / reply / auto）與分派工作都在獨立的背景程序執行，
呼叫者（使用者或某個 AI 的終端指令）只負責顯示進度。呼叫者被終止（例如關掉 VSCode 視窗）時，
背景程序不受影響，結果照樣寫進討論串。

Windows 上關閉視窗會以「整棵程序樹」終止呼叫者，直接啟動的子程序（即使 DETACHED）也會被一併終止；
所以用兩段式啟動：中間程序啟動背景程序後立即結束，背景程序就不再是呼叫者的後代。

每個討論串同時只有一個討論工作，狀態在 <討論串>/job.json，輸出在 job.log / job.err：
  {"kind": "send", "pid": 1234, "status": "running", "current": "codex", "since": ..., "exit": null}
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

JOB_ENV = "AGORA_JOB"          # 背景程序內設為討論串路徑
RUNNING = ("starting", "running")
FLAGS = 0
if sys.platform.startswith("win"):
    # 用隱藏的主控台（CREATE_NO_WINDOW）而不是完全沒有主控台（DETACHED_PROCESS）：
    # 背景程序再呼叫 agy／codex／claude／git 時會沿用這個隱藏主控台；
    # 若完全沒有主控台，Windows 會替每個子程序開新視窗（Windows Terminal 會多出一個分頁）
    FLAGS = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
BREAKAWAY = 0x01000000          # CREATE_BREAKAWAY_FROM_JOB：呼叫者若在 Windows job 內，也一併脫離


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _win_process(pid: int):
    """Windows：(是否存在, 建立時間 epoch 秒或 None)；直接查 Win32 API，比 tasklist 快很多"""
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    handle = kernel32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ctypes.get_last_error() == 5, None       # 拒絕存取表示存在（其他使用者的程序），其餘表示不存在
    try:
        code = wintypes.DWORD()
        if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)) and code.value != 259:   # STILL_ACTIVE
            return False, None
        times = [wintypes.FILETIME() for _ in range(4)]
        if not kernel32.GetProcessTimes(handle, *[ctypes.byref(t) for t in times]):
            return True, None
        ticks = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime   # 1601 年起的 100 奈秒
        return True, ticks / 1e7 - 11644473600
    finally:
        kernel32.CloseHandle(handle)


def pid_started(pid: Optional[int]) -> Optional[float]:
    """程序的建立時間（用來分辨 PID 被重複使用）；取不到時為 None（目前只支援 Windows 與 Linux）"""
    if not pid:
        return None
    if sys.platform.startswith("win"):
        try:
            return _win_process(pid)[1]
        except (OSError, AttributeError):
            return None
    try:
        return os.stat(f"/proc/{pid}").st_ctime
    except OSError:
        return None


def pid_alive(pid: Optional[int], started: Optional[float] = None) -> bool:
    """程序是否還在；有記錄建立時間時一併比對，PID 被別的程序重複使用（Windows 很常見）視為已結束"""
    if not pid:
        return False
    if sys.platform.startswith("win"):
        try:
            exists, created = _win_process(pid)
        except (OSError, AttributeError):
            from agoralib.quota import NO_WINDOW   # 以腳本執行 launch 時用不到，不在開頭匯入
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", **NO_WINDOW).stdout
            exists, created = str(pid) in out.split(), None
    else:
        try:
            os.kill(pid, 0)
            exists = True
        except OSError:
            exists = False
        created = pid_started(pid) if exists else None
    if exists and started and created:
        return abs(created - started) < 2
    return exists


def alive(record: dict) -> bool:
    """job.json 或 relay 紀錄裡的背景程序（pid、pid_started）是否還在"""
    return pid_alive(record.get("pid"), record.get("pid_started"))


def _handle_created(k, handle) -> Optional[float]:
    """用這個 handle 讀程序的建立時間（epoch 秒）；讀不到回傳 None"""
    import ctypes
    from ctypes import wintypes
    times = [wintypes.FILETIME() for _ in range(4)]
    if not k.GetProcessTimes(handle, *[ctypes.byref(t) for t in times]):
        return None
    ticks = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime   # 1601 年起的 100 奈秒
    return ticks / 1e7 - 11644473600


def _kill_windows_fallback(pid: int, proc=None) -> list:
    """taskkill 不可用時，用 Win32 快照終止後代，根程序優先用既有 Popen handle。"""
    import ctypes
    from ctypes import wintypes

    class Entry(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("usage", wintypes.DWORD), ("pid", wintypes.DWORD),
                    ("heap", ctypes.c_size_t), ("module", wintypes.DWORD), ("threads", wintypes.DWORD),
                    ("parent", wintypes.DWORD), ("priority", wintypes.LONG), ("flags", wintypes.DWORD),
                    ("exe", wintypes.WCHAR * 260)]

    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k.Process32FirstW.argtypes = k.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.OpenProcess.restype = wintypes.HANDLE
    k.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    k.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    k.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    failures = []

    def snapshot_parents() -> dict:
        """目前所有程序的 {PID: 父 PID}；快照失敗時回傳空的（不會找到後代，寧可漏殺也不誤殺）"""
        parents = {}
        snapshot = k.CreateToolhelp32Snapshot(2, 0)   # TH32CS_SNAPPROCESS
        if snapshot == ctypes.c_void_p(-1).value:
            failures.append(f"程序樹快照失敗：{ctypes.WinError(ctypes.get_last_error())}")
            return parents
        try:
            entry = Entry()
            entry.size = ctypes.sizeof(entry)
            found = k.Process32FirstW(snapshot, ctypes.byref(entry))
            while found:
                parents[entry.pid] = entry.parent
                found = k.Process32NextW(snapshot, ctypes.byref(entry))
            if ctypes.get_last_error() != 18:   # ERROR_NO_MORE_FILES
                failures.append(f"程序樹列舉失敗：{ctypes.WinError(ctypes.get_last_error())}")
        finally:
            k.CloseHandle(snapshot)
        return parents

    descendants, root_handle = _collect_targets(k, pid, snapshot_parents(), snapshot_parents)
    deadline = time.monotonic() + 2
    try:
        for target, handle in descendants:   # 由深到淺，用持有的 handle 終止（不重新開啟，PID 不會被換掉）
            _terminate_handle(k, handle, target, deadline, failures)
        if proc is not None:
            try:
                proc.kill()
                proc.wait(timeout=max(0, deadline - time.monotonic()))
            except (OSError, subprocess.TimeoutExpired) as e:
                failures.append(f"proc.kill PID {pid}：{e}")
        elif root_handle:
            _terminate_handle(k, root_handle, pid, deadline, failures)
        else:
            failures.append(f"OpenProcess PID {pid}：{ctypes.WinError(ctypes.get_last_error())}")
    finally:
        for _, handle in descendants:
            k.CloseHandle(handle)
        if root_handle:
            k.CloseHandle(root_handle)
    return failures


PROCESS_ACCESS = 0x100001 | 0x1000   # SYNCHRONIZE | PROCESS_TERMINATE | PROCESS_QUERY_LIMITED_INFORMATION


def _collect_targets(k, pid: int, parents: dict, resnapshot) -> tuple:
    """找出 pid 的後代並持有它們的 handle：回傳 ([(PID, handle)] 由深到淺, 根程序 handle 或 None)，handle 由呼叫者關閉。
    - 先開啟並持有每個候選的 handle（持有期間這個 PID 不會被系統重複使用），在同一個 handle 上讀建立時間；
      比父程序更早建立的不算後代（父程序結束後子程序記錄的 parent PID 不會清除，PID 可能被重複使用）。
    - 全部開啟後重新快照：候選的父 PID 和第一次快照不同（開啟前原程序已結束、PID 被無關程序重用），
      或它的父程序已被放棄時，就放棄它與它的後代。任何一步確認不了都放棄，寧可漏殺也不誤殺"""
    root = k.OpenProcess(PROCESS_ACCESS, False, pid)
    births = {pid: _handle_created(k, root) if root else None}
    held, order, pending, seen = {}, [], [pid], {pid}
    while pending:
        parent = pending.pop(0)
        born = births.get(parent)
        if born is None:
            continue
        for child, owner in list(parents.items()):
            if owner != parent or child in seen:
                continue
            seen.add(child)
            handle = k.OpenProcess(PROCESS_ACCESS, False, child)
            if not handle:
                continue
            created = _handle_created(k, handle)
            if created is None or created < born:
                k.CloseHandle(handle)
                continue
            held[child], births[child] = handle, created
            order.append(child)
            pending.append(child)
    after = resnapshot()
    valid, kept = {pid}, []
    for child in order:   # 廣度優先：父程序一定先判斷
        if after.get(child) == parents.get(child) and parents.get(child) in valid:
            valid.add(child)
            kept.append((child, held[child]))
        else:
            k.CloseHandle(held[child])
    return list(reversed(kept)), root


def _terminate_handle(k, handle, target: int, deadline: float, failures: list):
    import ctypes
    from ctypes import wintypes
    code = wintypes.DWORD()
    if k.GetExitCodeProcess(handle, ctypes.byref(code)) and code.value != 259:   # 已結束
        return
    if not k.TerminateProcess(handle, 1):
        failures.append(f"TerminateProcess PID {target}：{ctypes.WinError(ctypes.get_last_error())}")
    elif k.WaitForSingleObject(handle, max(0, int((deadline - time.monotonic()) * 1000))) != 0:
        failures.append(f"等待 PID {target} 結束逾時或失敗")


def kill_tree(pid: int, proc=None) -> str:
    """終止程序樹；傳回並顯示失敗診斷，taskkill 失敗時仍嘗試 Win32 / proc.kill。"""
    diagnostics = []
    if sys.platform.startswith("win"):
        from agoralib.quota import NO_WINDOW
        try:
            result = subprocess.run(["taskkill", "/pid", str(pid), "/T", "/F"], capture_output=True,
                                    text=True, errors="replace", timeout=2, **NO_WINDOW)
            if result.returncode:
                diagnostics.append(f"終止失敗：taskkill PID {pid} exit={result.returncode}："
                                   f"{(result.stderr or result.stdout).strip()}")
        except (OSError, subprocess.TimeoutExpired) as e:
            diagnostics.append(f"終止失敗：taskkill PID {pid}：{e}")
        if diagnostics:
            try:
                failures = _kill_windows_fallback(pid, proc)
            except (OSError, AttributeError) as e:
                failures = [f"Win32 備援：{e}"]
                if proc is not None:
                    try:
                        proc.kill()
                    except OSError as kill_error:
                        failures.append(f"proc.kill：{kill_error}")
            diagnostics.append("終止失敗：備援終止：" + "；".join(failures) if failures else "已使用備援終止程序樹")
    else:
        try:
            if proc is not None:
                proc.kill()
            else:
                os.killpg(pid, 15)
        except OSError as e:
            diagnostics.append(f"終止失敗：PID {pid}：{e}")
    detail = "\n".join(diagnostics)
    if detail:
        print(detail, file=sys.stderr, flush=True)
    return detail


# ───────────────────────── 兩段式啟動 ─────────────────────────

def spawn(argv: list, cwd: Path, stdout: Path, stderr: Path, append: bool = False, env: Optional[dict] = None) -> int:
    """在背景啟動 argv，回傳 PID；背景程序不屬於呼叫者的程序樹"""
    pid_file = stdout.with_name(f".spawn-{os.getpid()}-{time.time_ns()}.pid")
    spec = {"argv": argv, "cwd": str(cwd), "stdout": str(stdout), "stderr": str(stderr), "append": append,
            "env": env or {}, "pid_file": str(pid_file)}
    extra = {"creationflags": FLAGS} if sys.platform.startswith("win") else {}
    subprocess.run([sys.executable, str(Path(__file__).resolve()), "launch", json.dumps(spec)],
                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, timeout=60, check=True, **extra)
    pid = int(pid_file.read_text(encoding="utf-8"))
    pid_file.unlink(missing_ok=True)
    return pid


def _launch(spec: dict):
    mode = "a" if spec["append"] else "w"
    out = open(spec["stdout"], mode, encoding="utf-8")
    err = out if spec["stderr"] == spec["stdout"] else open(spec["stderr"], mode, encoding="utf-8")
    kwargs = dict(cwd=spec["cwd"], stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                  env={**os.environ, "PYTHONUNBUFFERED": "1", **spec["env"]})
    if not sys.platform.startswith("win"):
        proc = subprocess.Popen(spec["argv"], start_new_session=True, **kwargs)
    else:
        try:
            proc = subprocess.Popen(spec["argv"], creationflags=FLAGS | BREAKAWAY, **kwargs)
        except OSError:   # 所在的 job 不允許脫離
            proc = subprocess.Popen(spec["argv"], creationflags=FLAGS, **kwargs)
    Path(spec["pid_file"]).write_text(str(proc.pid), encoding="utf-8")


# ───────────────────────── 討論工作 ─────────────────────────

def job_path(thread: Path) -> Path:
    return thread / "job.json"


def read(thread: Path) -> dict:
    try:
        return json.loads(job_path(thread).read_text(encoding="utf-8"))
    except Exception:
        return {}


def update(thread: Path, **fields) -> dict:
    job = {**read(thread), **fields, "updated": now_str()}
    tmp = thread / "job.json.tmp"
    tmp.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(job_path(thread))
    return job


def active(thread: Path) -> Optional[dict]:
    """進行中的討論工作；程序已不存在但狀態還是 running 的（被強制終止）改記為 interrupted"""
    job = read(thread)
    if job.get("status") not in RUNNING:
        return None
    if job.get("status") == "starting" and time.time() - job.get("started_ts", 0) < 60:
        return job
    if alive(job):
        return job
    update(thread, status="interrupted", current=None, finished=now_str())
    return None


def start(thread: Path, kind: str, argv: list, cwd: Path) -> dict:
    update(thread, kind=kind, status="starting", pid=None, current=None, since=None, exit=None,
           started=now_str(), started_ts=time.time(), finished=None)
    try:   # PID 由背景程序自己寫入（run），避免和它同時改 job.json
        spawn(argv, cwd, thread / "job.log", thread / "job.err", env={JOB_ENV: str(thread)})
    except Exception as e:
        update(thread, status="failed", error=f"無法啟動背景程序：{e}", finished=now_str())
        raise
    return read(thread)


def run(thread: Path, func: Callable[[], None]):
    """在背景程序內執行討論，結束時寫入狀態與結束碼"""
    update(thread, status="running", pid=os.getpid(), pid_started=pid_started(os.getpid()))
    code = 0
    try:
        func()
    except SystemExit as e:
        if isinstance(e.code, str):
            print(e.code, file=sys.stderr)
            code = 1
        else:
            code = e.code or 0
    except BaseException:
        import traceback
        traceback.print_exc()
        code = 1
    finally:
        update(thread, status="done" if code == 0 else "failed", exit=code, current=None, finished=now_str())
    sys.exit(code)


def set_current(party: Optional[str]):
    """背景程序內：記下目前正在等誰回覆"""
    thread = os.getenv(JOB_ENV)
    if thread:
        update(Path(thread), current=party, since=time.time() if party else None)


def follow(thread: Path, from_start: bool = True, poll: float = 0.5) -> int:
    """把背景討論的輸出轉到目前的終端，直到它結束；回傳它的結束碼"""
    streams = [(thread / "job.log", sys.stdout), (thread / "job.err", sys.stderr)]
    offsets = {}
    for path, _ in streams:
        offsets[path] = 0 if from_start or not path.exists() else path.stat().st_size

    def pump():
        for path, out in streams:
            if not path.exists():
                continue
            with open(path, "rb") as f:
                f.seek(offsets[path])
                data = f.read()
            if data:
                offsets[path] += len(data)
                out.write(data.decode("utf-8", errors="replace"))
                out.flush()

    checks = 0
    while True:
        pump()
        if read(thread).get("status") not in RUNNING:
            break
        checks += 1
        if checks % 10 == 0 and not active(thread):   # 偶爾確認程序還在（被強制結束時狀態不會更新）
            break
        time.sleep(poll)
    pump()
    job = read(thread)
    if job.get("status") == "interrupted":
        print("⚠️ 背景討論程序已中斷（可能被強制結束），回覆前的內容都已寫入逐字稿。", file=sys.stderr)
        return 1
    if job.get("status") == "stopped":
        print("已停止。", file=sys.stderr)
        return 1
    return int(job.get("exit") or 0)


if __name__ == "__main__" and len(sys.argv) == 3 and sys.argv[1] == "launch":
    _launch(json.loads(sys.argv[2]))
