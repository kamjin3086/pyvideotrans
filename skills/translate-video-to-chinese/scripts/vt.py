#!/usr/bin/env python3
"""Agent-facing façade — prefer this over hand-assembled --stage flags.

Examples:
  vt.py preflight
  vt.py prepare "https://youtu.be/xxxx"
  vt.py prepare "https://youtu.be/xxxx" --lang en
  vt.py continue /home/.../translated-videos/VIDEO_ID
  vt.py status /home/.../translated-videos/VIDEO_ID
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import job_runtime  # noqa: E402
import pipeline_stages as stages  # noqa: E402

TRANSLATE = _SCRIPT_DIR / "translate_video.py"


def _py() -> str:
    return sys.executable


def vt_cmd(*parts: str) -> str:
    """Shell-ready command using HERMES_SKILL_DIR when present."""
    # Agents should copy this string as-is from JSON when possible.
    skill = os.environ.get("HERMES_SKILL_DIR", str(_SCRIPT_DIR.parent))
    base = f'python3 "{skill}/scripts/vt.py"'
    return " ".join([base, *parts])


def invoke_translate(extra: list[str]) -> int:
    cmd = [_py(), str(TRANSLATE), *extra]
    return subprocess.call(cmd)


def load_job(job_dir: Path) -> dict:
    path = job_dir / "job.json"
    if not path.is_file():
        raise SystemExit(f"[error] missing job.json in {job_dir}")
    return json.loads(path.read_text(encoding="utf-8"))


def decide_stage(job_dir: Path) -> str:
    """Pick which --stage to run for ``continue`` (tick or advance)."""
    job = load_job(job_dir)
    runtime = job_runtime.read_runtime(job_dir)
    pid = job_runtime.read_worker_pid(job_dir)
    alive = bool(pid and job_runtime.pid_is_alive(pid))
    rt_stage = str(runtime.get("stage") or "")
    rt_status = str(runtime.get("status") or "")

    if rt_stage and rt_status in {"running"} and alive:
        return rt_stage
    if rt_stage and rt_status in {"running", "failed"}:
        # Dead worker or failed stage → soft retry same stage.
        return rt_stage

    settings = job.get("settings") or {}
    source_language = str(settings.get("source_language") or "auto")
    source_video = Path(job.get("source_video") or "")
    result_dir = job_dir / "result"
    pipeline = job.get("pipeline") or {}
    recorded = pipeline.get("stages") or {}

    for stage in stages.STAGE_ORDER:
        rec = recorded.get(stage) or {}
        if rec.get("status") == "completed":
            continue
        ready, _ = stages.stage_artifacts_ready(
            stage,
            job_dir=job_dir,
            result_dir=result_dir,
            source_video=source_video if source_video.is_file() else None,
            source_language=source_language,
            force=False,
        )
        if ready and stage != "validate":
            continue
        return stage
    return "validate"


def cmd_preflight(args: argparse.Namespace) -> int:
    return invoke_translate(["--stage", "preflight", "--source-language", args.lang])


def cmd_prepare(args: argparse.Namespace) -> int:
    extra = [
        "--stage",
        "prepare",
        args.source,
        "--source-language",
        args.lang,
        "--voice-profile",
        args.voice_profile,
    ]
    if args.cookies:
        extra.extend(["--cookies-from-browser", args.cookies])
    if args.output_root:
        extra.extend(["--output-root", args.output_root])
    if args.force:
        extra.append("--force")
    return invoke_translate(extra)


def cmd_continue(args: argparse.Namespace) -> int:
    job_dir = Path(os.path.expanduser(args.job_dir)).resolve()
    stage = decide_stage(job_dir)
    print(f"[vt] continue → stage={stage} job_dir={job_dir}", flush=True)
    extra = ["--stage", stage, "--job-dir", str(job_dir)]
    if args.force:
        extra.append("--force")
    if args.budget_seconds is not None:
        extra.extend(["--budget-seconds", str(args.budget_seconds)])
    return invoke_translate(extra)


def cmd_status(args: argparse.Namespace) -> int:
    job_dir = Path(os.path.expanduser(args.job_dir)).resolve()
    job = load_job(job_dir) if (job_dir / "job.json").is_file() else {}
    runtime = job_runtime.read_runtime(job_dir)
    next_stage = decide_stage(job_dir) if job else None
    payload = {
        "job_directory": str(job_dir),
        "job_status": job.get("status"),
        "final_video": job.get("final_video"),
        "pipeline": job.get("pipeline"),
        "runtime": runtime,
        "suggested_stage": next_stage,
        "continue_command": vt_cmd("continue", f'"{job_dir}"') if job else None,
    }
    print("[vt-status]")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vt.py",
        description="Hermes-friendly wrapper for translate-video-to-chinese stages.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_pre = sub.add_parser("preflight", help="Check deps / models / local LLM")
    p_pre.add_argument("--lang", default="auto")
    p_pre.set_defaults(func=cmd_preflight)

    p_prep = sub.add_parser("prepare", help="Download / register source video")
    p_prep.add_argument("source", help="URL or local video path")
    p_prep.add_argument("--lang", default="auto", help="Source language code (default auto)")
    p_prep.add_argument("--voice-profile", default="auto")
    p_prep.add_argument("--cookies", help="Browser name for yt-dlp cookies, e.g. chrome")
    p_prep.add_argument("--output-root", default=None)
    p_prep.add_argument("--force", action="store_true")
    p_prep.set_defaults(func=cmd_prepare)

    p_c = sub.add_parser(
        "continue",
        help="Tick current long stage or advance to the next incomplete stage",
    )
    p_c.add_argument("job_dir", help="Job directory containing job.json")
    p_c.add_argument("--force", action="store_true")
    p_c.add_argument("--budget-seconds", type=float, default=None)
    p_c.set_defaults(func=cmd_continue)

    p_s = sub.add_parser("status", help="Show job/runtime checkpoint JSON")
    p_s.add_argument("job_dir")
    p_s.set_defaults(func=cmd_status)

    return parser


def main() -> int:
    args = build_parser().parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
