# -*- coding: utf-8 -*-
"""Detached worker + timed tick helpers for Hermes-safe orchestration.

Hermes foreground terminal calls are hard-capped (~600s). Long video jobs cannot
reliably finish inside one tool call. This module keeps the heavy pipeline in a
project-owned detached worker, while each Hermes ``--tick`` only waits up to a
safe budget and returns a checkpoint JSON the agent can loop on.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any


RUNTIME_NAME = "runtime.json"
WORKER_LOG_NAME = "worker.log"
WORKER_PID_NAME = "worker.pid"


def now_iso() -> str:
    import datetime as dt

    return dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds")


def runtime_path(job_dir: Path) -> Path:
    return job_dir / RUNTIME_NAME


def worker_log_path(job_dir: Path) -> Path:
    return job_dir / WORKER_LOG_NAME


def worker_pid_path(job_dir: Path) -> Path:
    return job_dir / WORKER_PID_NAME


def read_runtime(job_dir: Path) -> dict[str, Any]:
    path = runtime_path(job_dir)
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def write_runtime(job_dir: Path, payload: dict[str, Any]) -> None:
    job_dir.mkdir(parents=True, exist_ok=True)
    path = runtime_path(job_dir)
    tmp = path.with_suffix(".tmp")
    data = dict(payload)
    data["updated_at"] = now_iso()
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    else:
        return True


def read_worker_pid(job_dir: Path) -> int | None:
    path = worker_pid_path(job_dir)
    if not path.is_file():
        runtime = read_runtime(job_dir)
        pid = runtime.get("worker_pid")
        return int(pid) if isinstance(pid, int) else None
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def stop_worker(job_dir: Path, *, grace_seconds: float = 5.0) -> None:
    pid = read_worker_pid(job_dir)
    if pid and pid_is_alive(pid):
        try:
            os.killpg(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                pass
        deadline = time.time() + grace_seconds
        while time.time() < deadline and pid_is_alive(pid):
            time.sleep(0.2)
        if pid_is_alive(pid):
            try:
                os.killpg(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    os.kill(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    pass
    for path in (worker_pid_path(job_dir),):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def start_detached_worker(command: list[str], job_dir: Path, cwd: Path, env: dict[str, str]) -> int:
    """Start a session-leader worker that outlives the Hermes tick process."""
    job_dir.mkdir(parents=True, exist_ok=True)
    log_path = worker_log_path(job_dir)
    log_handle = open(log_path, "a", encoding="utf-8")
    log_handle.write(f"\n[{now_iso()}] worker start: {' '.join(command)}\n")
    log_handle.flush()
    process = subprocess.Popen(
        command,
        cwd=str(cwd),
        env=env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
    worker_pid_path(job_dir).write_text(str(process.pid) + "\n", encoding="utf-8")
    write_runtime(
        job_dir,
        {
            "status": "running",
            "phase": "worker_started",
            "worker_pid": process.pid,
            "started_at": now_iso(),
            "heartbeat_at": now_iso(),
            "message": "detached worker started",
            "worker_log": str(log_path),
        },
    )
    # Parent no longer needs the log fd; child keeps it.
    log_handle.close()
    return process.pid


def wait_for_runtime(
    job_dir: Path,
    *,
    budget_seconds: float,
    poll_seconds: float = 2.0,
    terminal_statuses: set[str] | None = None,
) -> dict[str, Any]:
    """Block up to budget_seconds watching runtime.json / worker liveness."""
    terminal_statuses = terminal_statuses or {"completed", "failed"}
    deadline = time.time() + max(1.0, budget_seconds)
    last: dict[str, Any] = {}
    while time.time() < deadline:
        last = read_runtime(job_dir)
        status = str(last.get("status") or "")
        pid = read_worker_pid(job_dir)
        alive = bool(pid and pid_is_alive(pid))
        last["worker_alive"] = alive
        if status in terminal_statuses:
            return last
        if pid and not alive and status not in terminal_statuses:
            # Worker vanished without a terminal status — mark failed for the tick.
            last = {
                **last,
                "status": "failed",
                "phase": last.get("phase") or "worker_dead",
                "message": "worker process exited without writing a terminal status",
                "worker_alive": False,
            }
            write_runtime(job_dir, last)
            return last
        time.sleep(poll_seconds)
    last = read_runtime(job_dir)
    pid = read_worker_pid(job_dir)
    last["worker_alive"] = bool(pid and pid_is_alive(pid))
    last["status"] = last.get("status") or "running"
    last["tick_exhausted"] = True
    last["message"] = last.get("message") or (
        "tick budget exhausted; worker still running — call --tick again"
    )
    return last


def tail_text(path: Path, max_chars: int = 4000) -> str:
    if not path.is_file():
        return ""
    try:
        data = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return data[-max_chars:]
