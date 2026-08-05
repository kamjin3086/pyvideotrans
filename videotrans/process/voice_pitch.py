"""Dependency-light acoustic voice routing for translated-video dubbing.

This module classifies vocal presentation, not a person's gender identity.  It
reuses the installed Qwen speaker encoder with two tiny repository prototypes;
a conservative YIN-style pitch estimate is only a fallback. Ambiguous or
undersampled subtitle cues stay on the job's default voice.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import subprocess
import tempfile
from typing import Any

import numpy as np


@dataclass(frozen=True)
class PitchResult:
    label: str
    median_hz: float | None
    voiced_frames: int
    confidence: float


def classify_speaker_embedding(
    embedding: np.ndarray,
    male_prototype: np.ndarray,
    female_prototype: np.ndarray,
    *,
    minimum_margin: float = 0.006,
) -> tuple[str, float, float, float]:
    """Classify a Qwen speaker embedding against compact voice prototypes."""
    def normalized(value: np.ndarray) -> np.ndarray:
        value = np.asarray(value, dtype=np.float32)
        return value / (np.linalg.norm(value) + 1e-9)

    embedding = normalized(embedding)
    male_score = float(embedding @ normalized(male_prototype))
    female_score = float(embedding @ normalized(female_prototype))
    margin = female_score - male_score
    if margin >= minimum_margin:
        label = "female"
    elif margin <= -minimum_margin:
        label = "male"
    else:
        label = "uncertain"
    return label, round(male_score, 4), round(female_score, 4), round(margin, 4)


def _extract_qwen_embedding(
    samples: np.ndarray,
    sample_rate: int,
    *,
    qwen_codec_bin: str | Path,
    codec_model: str | Path,
    base_model: str | Path,
    work_dir: Path,
    stem: str,
) -> np.ndarray:
    import soundfile as sf

    clip = _resample_linear(samples, sample_rate, 24000)
    wav_path = work_dir / f"{stem}.wav"
    sf.write(wav_path, clip, 24000, subtype="PCM_16")
    process = subprocess.run(
        [
            str(qwen_codec_bin),
            "--model", str(codec_model),
            "--talker", str(base_model),
            "-i", str(wav_path),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
        timeout=180,
    )
    spk_path = wav_path.with_suffix(".spk")
    if process.returncode or not spk_path.is_file():
        raise RuntimeError(f"qwen-codec speaker extraction failed: {process.stderr[-1000:]}")
    embedding = np.fromfile(spk_path, dtype="<f4")
    if len(embedding) != 2048:
        raise RuntimeError(f"invalid Qwen speaker embedding length: {len(embedding)}")
    return embedding


def _resample_linear(samples: np.ndarray, source_rate: int, target_rate: int = 16000) -> np.ndarray:
    if source_rate == target_rate:
        return samples.astype(np.float32, copy=False)
    output_length = max(1, int(round(len(samples) * target_rate / source_rate)))
    old_positions = np.arange(len(samples), dtype=np.float64) / source_rate
    new_positions = np.arange(output_length, dtype=np.float64) / target_rate
    return np.interp(new_positions, old_positions, samples).astype(np.float32)


def analyze_pitch(
    samples: np.ndarray,
    sample_rate: int,
    *,
    male_max_hz: float = 155.0,
    female_min_hz: float = 185.0,
) -> PitchResult:
    """Return a conservative male/female/uncertain acoustic label."""
    if samples.ndim > 1:
        samples = samples.mean(axis=1)
    samples = _resample_linear(np.asarray(samples, dtype=np.float32), sample_rate)
    sample_rate = 16000
    frame_length = int(0.05 * sample_rate)
    frame_hop = int(0.02 * sample_rate)
    if len(samples) < frame_length:
        return PitchResult("uncertain", None, 0, 0.0)

    frames = [samples[pos:pos + frame_length] for pos in range(0, len(samples) - frame_length + 1, frame_hop)]
    rms_values = np.asarray([np.sqrt(np.mean(frame * frame)) for frame in frames])
    rms_floor = max(0.003, float(np.percentile(rms_values, 30)) * 0.8)
    min_lag = int(sample_rate / 350.0)
    max_lag = int(sample_rate / 70.0)
    pitches: list[float] = []
    confidences: list[float] = []
    window = np.hanning(frame_length).astype(np.float32)

    for frame, rms in zip(frames, rms_values):
        if rms < rms_floor:
            continue
        centered = (frame - frame.mean()) * window
        differences = np.asarray(
            [np.sum((centered[:-lag] - centered[lag:]) ** 2) for lag in range(1, max_lag + 1)],
            dtype=np.float32,
        )
        cumulative = np.ones_like(differences)
        cumulative[1:] = (
            differences[1:]
            * np.arange(2, len(differences) + 1, dtype=np.float32)
            / (np.cumsum(differences)[1:] + 1e-9)
        )
        below_threshold = np.flatnonzero(cumulative[min_lag - 1:] < 0.18)
        if len(below_threshold):
            lag = int(below_threshold[0] + min_lag)
            while lag < max_lag and cumulative[lag] < cumulative[lag - 1]:
                lag += 1
        else:
            lag = int(np.argmin(cumulative[min_lag - 1:]) + min_lag)
        confidence = 1.0 - float(cumulative[lag - 1])
        if confidence >= 0.72:
            pitches.append(sample_rate / lag)
            confidences.append(confidence)

    if len(pitches) < 8:
        return PitchResult("uncertain", None, len(pitches), 0.0)

    values = np.asarray(pitches)
    low, high = np.percentile(values, [10, 90])
    trimmed = values[(values >= low) & (values <= high)]
    median_hz = float(np.median(trimmed if len(trimmed) else values))
    confidence = float(np.median(confidences))
    if median_hz <= male_max_hz:
        label = "male"
    elif median_hz >= female_min_hz:
        label = "female"
    else:
        label = "uncertain"
    return PitchResult(label, round(median_hz, 2), len(pitches), round(confidence, 3))


def route_subtitle_voices(
    vocal_path: str | Path,
    subtitles: list[dict[str, Any]],
    *,
    default_voice: str,
    female_voice: str = "serena",
    qwen_codec_bin: str | Path | None = None,
    codec_model: str | Path | None = None,
    base_model: str | Path | None = None,
    male_prototype_path: str | Path | None = None,
    female_prototype_path: str | Path | None = None,
) -> tuple[dict[str, str], dict[str, Any]]:
    """Map subtitle lines with Qwen speaker embeddings plus pitch fallback."""
    import soundfile as sf

    audio, sample_rate = sf.read(str(vocal_path), dtype="float32", always_2d=True)
    mono = audio.mean(axis=1)
    roles: dict[str, str] = {}
    lines: list[dict[str, Any]] = []
    counts = {"male": 0, "female": 0, "uncertain": 0}
    embedding_assets = [
        qwen_codec_bin,
        codec_model,
        base_model,
        male_prototype_path,
        female_prototype_path,
    ]
    use_embeddings = all(value and Path(value).is_file() for value in embedding_assets)
    male_prototype = np.fromfile(male_prototype_path, dtype="<f4") if use_embeddings else None
    female_prototype = np.fromfile(female_prototype_path, dtype="<f4") if use_embeddings else None

    with tempfile.TemporaryDirectory(prefix="pyvideotrans-voice-router-") as temp_name:
        temp_dir = Path(temp_name)
        for subtitle in subtitles:
            start_ms = int(subtitle.get("start_time", 0))
            end_ms = int(subtitle.get("end_time", start_ms))
            start = max(0, int((start_ms / 1000.0 - 0.04) * sample_rate))
            end = min(len(mono), int((end_ms / 1000.0 + 0.04) * sample_rate))
            clip = mono[start:end]
            pitch = analyze_pitch(clip, sample_rate)
            label = "uncertain"
            male_score = female_score = margin = None
            method = "pitch_fallback"
            # The Qwen speaker encoder is already required by the cloned voice.
            # Avoid it only for clips too short to produce a stable embedding.
            if use_embeddings and (end_ms - start_ms) >= 700:
                try:
                    embedding = _extract_qwen_embedding(
                        clip,
                        sample_rate,
                        qwen_codec_bin=qwen_codec_bin,
                        codec_model=codec_model,
                        base_model=base_model,
                        work_dir=temp_dir,
                        stem=f"line-{subtitle.get('line', 0)}",
                    )
                    label, male_score, female_score, margin = classify_speaker_embedding(
                        embedding, male_prototype, female_prototype
                    )
                    method = "qwen_speaker_embedding"
                except Exception:
                    label = "uncertain"
            elif pitch.label == "male" and pitch.confidence >= 0.78:
                # Low-pitched presentation is a safe fallback; high-pitched
                # male voices such as Dylan must not be routed by pitch alone.
                label = "male"

            line = str(subtitle.get("line"))
            voice = female_voice if label == "female" else default_voice
            roles[line] = voice
            counts[label] += 1
            lines.append({
                "line": int(subtitle.get("line", 0)),
                "start_ms": start_ms,
                "end_ms": end_ms,
                "voice": voice,
                "label": label,
                "method": method,
                "male_score": male_score,
                "female_score": female_score,
                "score_margin": margin,
                "pitch": asdict(pitch),
            })
    return roles, {
        "method": "qwen_speaker_embedding_prototypes_v1_with_pitch_fallback",
        "meaning": "acoustic voice presentation, not gender identity",
        "default_voice": default_voice,
        "female_voice": female_voice,
        "embedding_margin": 0.006,
        "pitch_fallback_hz": {"male_max": 155.0, "female_min": 185.0},
        "counts": counts,
        "lines": lines,
    }
