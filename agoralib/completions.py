"""Workspace completion reports shared by watch and conversation hooks.

Terminal results are archived before a thread can be reused. An OS lock protects
delivery and acknowledgements; it is released even if a watcher is terminated.
These reports are context for the calling CLI, not push messages to another UI.
"""

import hashlib
import json
import os
import time
import uuid
from contextlib import contextmanager
from datetime import datetime

from agoralib import jobs

RELAY_ACTIVE = ("starting", "running", "waiting", "checking")
TERMINAL = {"done", "failed", "stopped", "interrupted", "blocked", "needs_upgrade", "max_rounds"}


def read_object(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"完成回報紀錄格式錯誤：{path}")
    return value


def atomic_write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def event(thread, kind, record, topic=""):
    if record.get("status") not in TERMINAL:
        return None
    identity = (record.get("dispatch_id") if kind == "relay" else record.get("job_id"))
    identity = identity or record.get("started_ts") or record.get("started") or record.get("pid") or "legacy"
    key = hashlib.sha256(json.dumps([thread.name, kind, identity], ensure_ascii=False).encode()).hexdigest()
    return {"id": key, "thread": thread.name, "topic": topic, "type": kind,
            **{k: record.get(k) for k in ("dispatch_id", "job_id", "worker", "kind", "status", "branch",
                                         "commit_sha", "commit_status", "error", "blocked_reason")}}


def archive(thread, kind, record, topic=""):
    """Only controller thread paths are eligible; never write beside arbitrary inputs."""
    item = event(thread, kind, record, topic)
    if item and thread.parent.name == "threads" and thread.parent.parent.name == ".agora":
        atomic_write(thread.parent.parent / "completions" / f"{item['id']}.json", item)


def snapshot(root):
    """Read current and archived results, detecting dead workers without editing them."""
    home = root / ".agora"
    completed, active = {}, False
    for path in sorted((home / "completions").glob("*.json")):
        item = read_object(path)
        if item.get("id"):
            completed[item["id"]] = item
    for thread in sorted((home / "threads").glob("*")):
        records = []
        for filename in ("state.json", "job.json"):
            try:
                records.append(read_object(thread / filename))
            except (OSError, ValueError):
                records.append({})  # damage to one record must not hide the other
        state, job = records
        for kind, record in (("relay", state.get("relay") or {}), ("discussion", job)):
            if not isinstance(record, dict):
                continue
            running = RELAY_ACTIVE if kind == "relay" else jobs.RUNNING
            if record.get("status") in running:
                started = record.get("started_ts") or 0
                if not started and record.get("started"):
                    try:
                        started = datetime.strptime(record["started"], "%Y-%m-%d %H:%M:%S").timestamp()
                    except ValueError:
                        pass
                grace = record["status"] == "starting" and time.time() - started < 60
                if grace or jobs.alive(record):
                    active = True
                    continue
                record = {**record, "status": "interrupted"}
            item = event(thread, kind, record, state.get("topic", ""))
            if item:
                completed[item["id"]] = item
    return list(completed.values()), active


@contextmanager
def report_lock(home, wait):
    home.mkdir(parents=True, exist_ok=True)
    with open(home / "completion-report.lock", "a+b") as handle:
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"0")
            handle.flush()
        deadline = time.monotonic() + wait
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("其他 watch 或 hook 正在回報完成項目，請稍後再試。")
                time.sleep(0.05)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def deliver(root, emit, wait=0.2):
    """Emit unreported results then acknowledge them, under one cross-process lock.

    Returns (number delivered, whether any work is active). A failed emit leaves
    results unreported. Both watch and hooks use this same workspace-wide ledger.
    """
    if not (root / ".agora").is_dir():
        emit([])
        return 0, False
    with report_lock(root / ".agora", wait):
        ledger_path = root / ".agora" / "completion-reported.json"
        ledger = read_object(ledger_path)
        reported = ledger.get("reported", {})
        if not isinstance(reported, dict):
            raise ValueError(f"完成回報紀錄格式錯誤：{ledger_path}")
        items, active = snapshot(root)
        pending = [item for item in items if item["id"] not in reported]
        emit(pending)
        if pending:
            reported.update({item["id"]: time.time() for item in pending})
            atomic_write(ledger_path, {"reported": reported})
        return len(pending), active


def one_line(value):
    return " ".join(str(value or "").split())


def describe(item):
    kind = "分派" if item["type"] == "relay" else "討論"
    identity = item.get("dispatch_id") or item.get("job_id") or item["id"][:12]
    status = item["status"]
    if status != "done":
        next_step = "查看 progress.md 與錯誤，處理原因後重新分派" if kind == "分派" else "查看逐字稿與 job.err，處理原因後再討論"
        if status == "needs_upgrade":
            next_step = "以較高等級重新分派"
        elif status == "blocked":
            next_step = "處理帳務或權限問題後重新分派"
    elif kind == "討論":
        next_step = "閱讀逐字稿，回報結論與下一步"
    elif item.get("commit_status") == "committed" and item.get("commit_sha"):
        next_step = "審查 diff 與測試，再決定是否整合分支"
    else:
        next_step = "查看 progress.md，確認變更與驗證結果後決定下一步"
    details = (f"branch={item.get('branch') or '無'}，commit={item.get('commit_sha') or '無'}"
               f"（{item.get('commit_status') or '未提交'}）")
    error = item.get("blocked_reason") or item.get("error")
    return one_line(f"{kind} {item['thread']} / {identity}｜{item.get('topic') or ''}｜{status}｜{details}"
                    + (f"｜{error}" if error else "") + f"｜下一步：{next_step}")
