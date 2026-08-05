from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from videotrans.process._demucs_validate import validate_demucs_stems


def _write_wav(path: Path, data: np.ndarray, sr: int = 16000) -> None:
    sf.write(str(path), data, sr)


def test_validate_demucs_stems_accepts_reconstructable_energy(tmp_path: Path):
    sr = 16000
    duration = 30
    t = np.linspace(0, duration, sr * duration, endpoint=False)
    vocal = (0.2 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
    instr = (0.1 * np.sin(2 * np.pi * 110 * t)).astype(np.float32)
    mix = vocal + instr
    mix_path = tmp_path / "mix.wav"
    vocal_path = tmp_path / "vocals.wav"
    instr_path = tmp_path / "no_vocals.wav"
    _write_wav(mix_path, mix, sr)
    _write_wav(vocal_path, vocal, sr)
    _write_wav(instr_path, instr, sr)

    ok, report = validate_demucs_stems(mix_path, vocal_path, instr_path, stride_s=2.0)
    assert ok is True
    assert report["reason"] == "ok"
    assert report["bad_windows"] == 0


def test_validate_demucs_stems_rejects_post_segment_silence(tmp_path: Path):
    sr = 16000
    duration = 40
    samples = sr * duration
    mix = (0.25 * np.sin(2 * np.pi * 180 * np.linspace(0, duration, samples, endpoint=False))).astype(
        np.float32
    )
    vocal = np.zeros(samples, dtype=np.float32)
    instr = np.zeros(samples, dtype=np.float32)
    # Only the first ~8 seconds resemble a successful Demucs segment.
    alive = sr * 8
    vocal[:alive] = mix[:alive] * 0.7
    instr[:alive] = mix[:alive] * 0.3

    mix_path = tmp_path / "mix.wav"
    vocal_path = tmp_path / "vocals.wav"
    instr_path = tmp_path / "no_vocals.wav"
    _write_wav(mix_path, mix, sr)
    _write_wav(vocal_path, vocal, sr)
    _write_wav(instr_path, instr, sr)

    ok, report = validate_demucs_stems(mix_path, vocal_path, instr_path, stride_s=2.0)
    assert ok is False
    assert "stem_energy_collapse" in report["reason"]
    assert report["first_bad_s"] is not None
    assert report["first_bad_s"] >= 8.0
    assert report["bad_ratio"] > 0.25
