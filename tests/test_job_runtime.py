# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "skills" / "translate-video-to-chinese" / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))
import job_runtime  # noqa: E402


def test_wait_for_runtime_completes(tmp_path: Path) -> None:
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    job_runtime.write_runtime(job_dir, {"status": "running", "phase": "work"})

    def finish() -> None:
        time.sleep(0.3)
        job_runtime.write_runtime(job_dir, {"status": "completed", "phase": "done"})

    import threading

    threading.Thread(target=finish, daemon=True).start()
    result = job_runtime.wait_for_runtime(job_dir, budget_seconds=2.0, poll_seconds=0.1)
    assert result["status"] == "completed"


def test_wait_for_runtime_budget(tmp_path: Path) -> None:
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    job_runtime.write_runtime(job_dir, {"status": "running", "phase": "work", "worker_pid": os.getpid()})
    job_runtime.worker_pid_path(job_dir).write_text(str(os.getpid()) + "\n", encoding="utf-8")
    started = time.time()
    result = job_runtime.wait_for_runtime(job_dir, budget_seconds=0.6, poll_seconds=0.1)
    elapsed = time.time() - started
    assert result["status"] == "running"
    assert result.get("tick_exhausted") is True
    assert 0.5 <= elapsed < 1.5


def test_detached_worker_survives_parent(tmp_path: Path) -> None:
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    marker = job_dir / "done.txt"
    command = [
        sys.executable,
        "-c",
        (
            "import time, pathlib;"
            f"p=pathlib.Path({str(marker)!r});"
            "time.sleep(0.8);"
            "p.write_text('ok', encoding='utf-8')"
        ),
    ]
    pid = job_runtime.start_detached_worker(command, job_dir, tmp_path, os.environ.copy())
    assert job_runtime.pid_is_alive(pid)
    # Simulate Hermes tick ending quickly while worker continues.
    time.sleep(0.2)
    assert job_runtime.pid_is_alive(pid)
    deadline = time.time() + 3
    while time.time() < deadline and not marker.is_file():
        time.sleep(0.1)
    assert marker.read_text(encoding="utf-8") == "ok"
    runtime = job_runtime.read_runtime(job_dir)
    assert runtime.get("worker_pid") == pid
