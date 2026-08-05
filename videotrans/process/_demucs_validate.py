# -*- coding: utf-8 -*-
"""Validate Demucs stem integrity after separation.

Demucs may exit 0 while later segments are silently zeroed when the AMD iGPU
is contended (for example by a concurrent local LLM). Existence checks alone
are not enough; compare stem energy against the source mix.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any


def _rms(samples) -> float:
    import numpy as np

    if samples is None or samples.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(samples, dtype=np.float64))))


def validate_demucs_stems(
    input_file: str | Path,
    vocal_file: str | Path,
    instr_file: str | Path,
    *,
    window_s: float = 2.0,
    stride_s: float = 5.0,
    input_active_rms: float = 1e-3,
    stem_silent_rms: float = 1e-4,
    max_bad_ratio: float = 0.25,
    min_active_windows: int = 3,
) -> tuple[bool, dict[str, Any]]:
    """Return whether vocal/instrument stems preserve source energy over time.

    A window is "active" when the source mix RMS exceeds ``input_active_rms``.
    It is "bad" when the source is active but both stems are near silence.
    The separation fails when bad active windows exceed ``max_bad_ratio``.
    """
    import soundfile as sf

    input_path = Path(input_file)
    vocal_path = Path(vocal_file)
    instr_path = Path(instr_file)
    report: dict[str, Any] = {
        "input": str(input_path),
        "vocal": str(vocal_path),
        "instrument": str(instr_path),
        "ok": False,
    }

    if not input_path.is_file() or not vocal_path.is_file() or not instr_path.is_file():
        report["reason"] = "missing_file"
        return False, report

    input_info = sf.info(str(input_path))
    vocal_info = sf.info(str(vocal_path))
    instr_info = sf.info(str(instr_path))
    duration = min(input_info.duration, vocal_info.duration, instr_info.duration)
    report["duration"] = duration
    if duration < 1.0:
        report["reason"] = "duration_too_short"
        return False, report

    if abs(vocal_info.duration - input_info.duration) > 1.0:
        report["reason"] = "duration_mismatch"
        report["input_duration"] = input_info.duration
        report["vocal_duration"] = vocal_info.duration
        return False, report

    sr = int(input_info.samplerate)
    window = max(int(window_s * sr), 1)
    stride = max(int(stride_s * sr), 1)
    active = 0
    bad = 0
    first_bad_s: float | None = None
    total = int(duration * sr)
    pos = 0
    while pos + window <= total:
        src_chunk, _ = sf.read(
            str(input_path), start=pos, stop=pos + window, dtype="float32", always_2d=True
        )
        voc_chunk, _ = sf.read(
            str(vocal_path), start=pos, stop=pos + window, dtype="float32", always_2d=True
        )
        inst_chunk, _ = sf.read(
            str(instr_path), start=pos, stop=pos + window, dtype="float32", always_2d=True
        )
        if src_chunk.shape[0] < window:
            break
        src_rms = _rms(src_chunk)
        voc_rms = _rms(voc_chunk)
        inst_rms = _rms(inst_chunk)
        if src_rms >= input_active_rms:
            active += 1
            if voc_rms < stem_silent_rms and inst_rms < stem_silent_rms:
                bad += 1
                if first_bad_s is None:
                    first_bad_s = pos / sr
        pos += stride

    report["active_windows"] = active
    report["bad_windows"] = bad
    report["first_bad_s"] = first_bad_s
    if active < min_active_windows:
        report["ok"] = True
        report["reason"] = "source_mostly_silent"
        return True, report

    bad_ratio = bad / active
    report["bad_ratio"] = bad_ratio
    if bad_ratio > max_bad_ratio:
        report["reason"] = (
            f"stem_energy_collapse bad_ratio={bad_ratio:.3f} "
            f"first_bad_s={first_bad_s}"
        )
        return False, report

    report["ok"] = True
    report["reason"] = "ok"
    return True, report
