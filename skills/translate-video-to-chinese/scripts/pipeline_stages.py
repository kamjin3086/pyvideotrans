# -*- coding: utf-8 -*-
"""Agent-facing pipeline stages for Hermes video translation.

Agent orchestrates these discrete stages (not one opaque worker). Long stages
may return ``in_progress`` so Hermes can tick within the ~600s hard cap.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


# Agent-visible order after preflight.
STAGE_ORDER = (
    "prepare",     # yt-dlp / local source → job.json
    "separate",    # Demucs + demux (CLI vtv-stage=prepare)
    "recognize",   # STT
    "translate",   # LLM translation
    "dub",         # TTS + align + mux (CLI vtv-stage=dub)
    "validate",    # skill-side AV checks
)

# Stages whose wall time often exceeds Hermes foreground budget (~600s).
LONG_STAGES = frozenset({"separate", "recognize", "translate", "dub"})

# Map skill stage → CLI --vtv-stage (None = skill-local work).
CLI_STAGE_MAP = {
    "separate": "prepare",
    "recognize": "recognize",
    "translate": "translate",
    "dub": "dub",
}


@dataclass(frozen=True)
class StageSpec:
    name: str
    user_hint_done: str
    next_stage: str | None
    tickable: bool


STAGE_SPECS: dict[str, StageSpec] = {
    "prepare": StageSpec(
        "prepare",
        "源视频已就绪，下一步：人声/背景分离。",
        "separate",
        False,
    ),
    "separate": StageSpec(
        "separate",
        "人声分离完成，下一步：语音识别。",
        "recognize",
        True,
    ),
    "recognize": StageSpec(
        "recognize",
        "识别完成，下一步：翻译字幕。",
        "translate",
        True,
    ),
    "translate": StageSpec(
        "translate",
        "翻译完成，下一步：中文配音与成片合成（可能较久）。",
        "dub",
        True,
    ),
    "dub": StageSpec(
        "dub",
        "配音与合成完成，下一步：成片校验。",
        "validate",
        True,
    ),
    "validate": StageSpec(
        "validate",
        "校验通过，中文成片已就绪。",
        None,
        False,
    ),
}


def next_after(stage: str) -> str | None:
    spec = STAGE_SPECS.get(stage)
    return spec.next_stage if spec else None


def source_srt_candidates(result_dir: Path, source_language: str) -> list[Path]:
    names = []
    if source_language and source_language != "zh-cn":
        names.append(f"{source_language}.srt")
    names.extend(["auto.srt", "en.srt"])
    seen: set[str] = set()
    out: list[Path] = []
    for name in names:
        if name in seen:
            continue
        seen.add(name)
        out.append(result_dir / name)
    return out


def stage_artifacts_ready(
    stage: str,
    *,
    job_dir: Path,
    result_dir: Path,
    source_video: Path | None,
    source_language: str,
    force: bool = False,
) -> tuple[bool, dict[str, str]]:
    """Return (ready, artifact_paths). ready=True means stage can be skipped."""
    if force:
        return False, {}
    artifacts: dict[str, str] = {}

    def ok_file(path: Path, min_size: int = 100) -> bool:
        return path.is_file() and path.stat().st_size >= min_size

    if stage == "prepare":
        if source_video and ok_file(source_video, 1000):
            artifacts["source_video"] = str(source_video)
            return True, artifacts
        return False, {}

    if stage == "separate":
        vocal = result_dir / "vocal.wav"
        instrument = result_dir / "instrument.wav"
        if ok_file(vocal, 1000) and ok_file(instrument, 1000):
            artifacts["vocal"] = str(vocal)
            artifacts["instrument"] = str(instrument)
            return True, artifacts
        return False, {}

    if stage == "recognize":
        for path in source_srt_candidates(result_dir, source_language):
            if ok_file(path):
                artifacts["source_subtitles"] = str(path)
                return True, artifacts
        return False, {}

    if stage == "translate":
        path = result_dir / "zh-cn.srt"
        if ok_file(path):
            artifacts["zh_subtitles"] = str(path)
            return True, artifacts
        return False, {}

    if stage == "dub":
        # Prefer stem.mp4 from source, else largest mp4 that is not a mid product.
        if source_video:
            expected = result_dir / f"{source_video.stem}.mp4"
            if ok_file(expected, 100_000):
                artifacts["final_video"] = str(expected)
                return True, artifacts
        candidates = [
            p
            for p in result_dir.glob("*.mp4")
            if p.is_file() and p.stat().st_size >= 100_000 and "output_hardsub" not in p.name
        ]
        if candidates:
            best = max(candidates, key=lambda p: p.stat().st_size)
            artifacts["final_video"] = str(best)
            return True, artifacts
        return False, {}

    if stage == "validate":
        # validate always re-checks; never skip unless job already completed with ok.
        return False, {}

    return False, {}


def mark_stage(job: dict[str, Any], stage: str, *, status: str, artifacts: dict[str, str] | None = None, message: str = "") -> None:
    import datetime as dt

    pipeline = job.setdefault("pipeline", {})
    stages = pipeline.setdefault("stages", {})
    stages[stage] = {
        "status": status,
        "updated_at": dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds"),
        "artifacts": artifacts or {},
        "message": message,
    }
    pipeline["current_stage"] = stage
    pipeline["status"] = status


def stage_payload(
    *,
    status: str,
    stage: str,
    job_dir: Path,
    message: str,
    artifacts: dict[str, str] | None = None,
    next_stage: str | None = None,
    tick_command: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    spec = STAGE_SPECS.get(stage)
    nxt = next_stage if next_stage is not None else (spec.next_stage if spec and status == "completed" else None)
    payload: dict[str, Any] = {
        "status": status,
        "stage": stage,
        "job_directory": str(job_dir),
        "message": message,
        "artifacts": artifacts or {},
        "user_hint": "",
        "next_stage": nxt,
        "next_action": "",
    }
    if status == "completed":
        payload["user_hint"] = (spec.user_hint_done if spec else message)
        if nxt:
            payload["next_action"] = "report_user_hint_then_run_next_stage"
            payload["next_command"] = (
                f'python3 "${{HERMES_SKILL_DIR}}/scripts/translate_video.py" '
                f'--stage {nxt} --job-dir "{job_dir}"'
            )
        else:
            payload["next_action"] = "report_success_to_user"
    elif status == "in_progress":
        payload["user_hint"] = ""  # do not chat mid-stage / mid-Demucs
        payload["next_action"] = "immediately_call_same_stage_again"
        payload["tick_command"] = tick_command or (
            f'python3 "${{HERMES_SKILL_DIR}}/scripts/translate_video.py" '
            f'--stage {stage} --job-dir "{job_dir}"'
        )
    elif status == "failed":
        payload["next_action"] = "report_failure_to_user"
        payload["user_hint"] = message
    if extra:
        payload.update(extra)
    return payload
