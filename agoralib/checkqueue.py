"""跨分派的 check 驗證槽位（FIFO 排隊）。

槽位與排隊檔放在控制根目錄的 `.agora/check-slots/`：
- `queue.lock`：短暫持有的 OS 檔案鎖，只包住檢查／回收／入隊／升格／釋放。
- `seq`：單調遞增序號，保證嚴格 FIFO。
- `wait-<seq>-<pid>-<token>.json`：排隊中項目（記 PID 與程序建立時間）。
- `slot-<seq>-<pid>-<token>.json`：執行中槽位（記 PID 與程序建立時間）。

回收只看 `jobs.pid_alive(pid, pid_started)`，程序不在就回收；絕不依逾時刪除任何 Git `index.lock`。
"""

import json
import os
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from agoralib import config, jobs

SLOTS_DIRNAME = "check-slots"
DEFAULT_POLL_INTERVAL = 0.25
DEFAULT_REPORT_INTERVAL = 15.0


class CheckStopped(Exception):
    """等待驗證槽位期間收到 stop_requested，取消驗證（非測試失敗）。"""


def slots_dir(control_root: Path) -> Path:
    return Path(control_root) / ".agora" / SLOTS_DIRNAME


@contextmanager
def _queue_lock(directory: Path, wait: float = 60.0):
    """短暫持有的跨程序排隊鎖；程序結束時由 OS 自動釋放。"""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "queue.lock"
    with open(path, "a+b") as f:
        if f.seek(0, os.SEEK_END) == 0:
            f.write(b"0")
            f.flush()
        deadline = time.monotonic() + wait
        while True:
            try:
                f.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("驗證排隊鎖逾時，請稍後重試。")
                time.sleep(0.02)
        try:
            yield
        finally:
            try:
                f.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(f, fcntl.LOCK_UN)
            except OSError:
                pass


def _read_entry(path: Path) -> Optional[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _write_entry(path: Path, data: dict):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _next_seq(directory: Path) -> int:
    seq_file = directory / "seq"
    current = 0
    try:
        current = int(seq_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        current = 0
    for pattern in ("slot-*.json", "wait-*.json"):
        for p in directory.glob(pattern):
            entry = _read_entry(p)
            if entry and isinstance(entry.get("seq"), int) and entry["seq"] > current:
                current = entry["seq"]
    nxt = current + 1
    tmp = directory / "seq.tmp"
    tmp.write_text(str(nxt), encoding="utf-8")
    tmp.replace(seq_file)
    return nxt


def _reap_locked(directory: Path) -> Tuple[List[Tuple[Path, dict]], List[Tuple[Path, dict]]]:
    """在持有 queue.lock 時回收已結束程序的槽位與排隊檔，回傳依序排列的 (slots, waiters)。"""
    slots: List[Tuple[Path, dict]] = []
    waiters: List[Tuple[Path, dict]] = []
    for pattern, bucket in (("slot-*.json", slots), ("wait-*.json", waiters)):
        for path in sorted(directory.glob(pattern)):
            entry = _read_entry(path)
            if entry is None or not jobs.pid_alive(entry.get("pid"), entry.get("pid_started")):
                path.unlink(missing_ok=True)
                continue
            bucket.append((path, entry))
    return slots, waiters


def _format_ahead(entries: List[dict]) -> str:
    parts = []
    for entry in entries[:5]:
        pid = entry.get("pid")
        label = (entry.get("label") or "").strip()
        if label and label != f"PID {pid}":
            parts.append(f"{label} (PID {pid})")
        elif pid:
            parts.append(f"PID {pid}")
        elif label:
            parts.append(label)
    text = "、".join(parts)
    if len(entries) > 5:
        text += f"…等 {len(entries)} 個"
    return text


def _cleanup(directory: Path, *paths: Path):
    """釋放槽位與排隊檔；即使再次遭遇中斷也盡力刪除。"""
    try:
        with _queue_lock(directory, wait=5.0):
            for p in paths:
                p.unlink(missing_ok=True)
    except BaseException:
        for p in paths:
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass


def _normalize_limit(control_root: Path, limit: Optional[int]) -> int:
    if limit is None:
        return config.check_concurrency(control_root)
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        return 1
    return limit


@contextmanager
def acquire_slot(
    control_root: Path,
    limit: Optional[int] = None,
    label: str = "",
    stop_check: Optional[Callable[[], bool]] = None,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    report_interval: float = DEFAULT_REPORT_INTERVAL,
    output: Optional[Callable[[str], None]] = None,
):
    """取得跨分派 check 驗證槽位（嚴格 FIFO），離開（含例外／KeyboardInterrupt／SystemExit）時必釋放。"""
    control_root = Path(control_root).resolve()
    directory = slots_dir(control_root)
    effective_limit = _normalize_limit(control_root, limit)
    emit = output or (lambda msg: print(msg, flush=True))

    if stop_check is not None and stop_check():
        raise CheckStopped("已要求停止（stop_requested），取消等待驗證，未執行測試。")

    pid = os.getpid()
    started = jobs.pid_started(pid)
    token = uuid.uuid4().hex[:8]

    with _queue_lock(directory):
        _reap_locked(directory)
        seq = _next_seq(directory)
        wait_path = directory / f"wait-{seq:012d}-{pid}-{token}.json"
        slot_path = directory / f"slot-{seq:012d}-{pid}-{token}.json"
        entry = {
            "seq": seq,
            "pid": pid,
            "pid_started": started,
            "label": label or control_root.name,
            "enqueued_at": jobs.now_str(),
            "enqueued_ts": time.time(),
        }
        _write_entry(wait_path, entry)

    try:
        last_report: Optional[float] = None
        last_pos: Optional[int] = None
        waited = False
        started_at = ""
        while True:
            if stop_check is not None and stop_check():
                raise CheckStopped("已要求停止（stop_requested），結束等待驗證，未執行測試。")

            acquired = False
            pos = 0
            ahead_text = ""
            active_count = 0
            with _queue_lock(directory):
                slots, waiters = _reap_locked(directory)
                wait_paths = [p for p, _ in waiters]
                if wait_path not in wait_paths:
                    _write_entry(wait_path, entry)
                    slots, waiters = _reap_locked(directory)
                    wait_paths = [p for p, _ in waiters]
                idx = wait_paths.index(wait_path)
                free = effective_limit - len(slots)
                if idx < free:
                    started_at = jobs.now_str()
                    slot_entry = {**entry, "started_at": started_at, "started_ts": time.time()}
                    _write_entry(slot_path, slot_entry)
                    wait_path.unlink(missing_ok=True)
                    acquired = True
                    active_count = len(slots) + 1
                else:
                    pos = idx + 1
                    ahead = [e for _, e in slots] + [e for _, e in waiters[:idx]]
                    ahead_text = _format_ahead(ahead)

            if acquired:
                prefix = "已取得驗證槽位，開始驗證" if waited else "開始驗證"
                emit(f"{prefix}（{started_at}；槽位 {active_count}/{effective_limit}）")
                break

            waited = True
            now_mono = time.monotonic()
            if last_report is None or pos != last_pos or (now_mono - last_report) >= report_interval:
                emit(f"等待驗證中（第 {pos} 位，前面：{ahead_text or '其他程序'}）")
                last_report = now_mono
                last_pos = pos
            time.sleep(poll_interval)

        yield {
            "seq": seq,
            "pid": pid,
            "slot_path": slot_path,
            "started_at": started_at,
            "limit": effective_limit,
        }
    finally:
        _cleanup(directory, slot_path, wait_path)
