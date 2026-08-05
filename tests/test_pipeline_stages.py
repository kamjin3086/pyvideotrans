# -*- coding: utf-8 -*-
from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "skills" / "translate-video-to-chinese" / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))
import pipeline_stages  # noqa: E402


def test_stage_order_and_hints() -> None:
    assert pipeline_stages.STAGE_ORDER[0] == "prepare"
    assert pipeline_stages.STAGE_ORDER[-1] == "validate"
    assert "separate" in pipeline_stages.LONG_STAGES
    assert pipeline_stages.CLI_STAGE_MAP["separate"] == "prepare"
    assert pipeline_stages.CLI_STAGE_MAP["dub"] == "dub"
    payload = pipeline_stages.stage_payload(
        status="completed",
        stage="separate",
        job_dir=Path("/tmp/job"),
        message="ok",
        artifacts={"vocal": "/tmp/v.wav"},
    )
    assert payload["next_stage"] == "recognize"
    assert "人声分离" in payload["user_hint"]
    assert payload["next_action"] == "report_user_hint_then_run_next_stage"


def test_artifact_skip_detect(tmp_path: Path) -> None:
    result = tmp_path / "result"
    result.mkdir()
    vocal = result / "vocal.wav"
    instrument = result / "instrument.wav"
    vocal.write_bytes(b"0" * 2000)
    instrument.write_bytes(b"0" * 2000)
    ready, arts = pipeline_stages.stage_artifacts_ready(
        "separate",
        job_dir=tmp_path,
        result_dir=result,
        source_video=None,
        source_language="auto",
    )
    assert ready is True
    assert "vocal" in arts
