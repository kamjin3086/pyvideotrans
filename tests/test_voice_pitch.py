from pathlib import Path

import numpy as np

from videotrans.process.voice_pitch import analyze_pitch, classify_speaker_embedding


def _sine(frequency: float, seconds: float = 2.0, sample_rate: int = 16000) -> np.ndarray:
    time = np.arange(int(seconds * sample_rate), dtype=np.float32) / sample_rate
    # A little second harmonic is closer to voiced speech than a pure sine.
    return (0.25 * np.sin(2 * np.pi * frequency * time)
            + 0.05 * np.sin(4 * np.pi * frequency * time)).astype(np.float32)


def test_low_pitch_routes_to_male_voice():
    result = analyze_pitch(_sine(125), 16000)
    assert result.label == "male"
    assert 120 <= result.median_hz <= 130


def test_high_pitch_routes_to_female_voice():
    result = analyze_pitch(_sine(235), 16000)
    assert result.label == "female"
    assert 225 <= result.median_hz <= 245


def test_overlap_range_stays_uncertain():
    result = analyze_pitch(_sine(170), 16000)
    assert result.label == "uncertain"


def test_short_audio_stays_uncertain():
    result = analyze_pitch(np.zeros(100, dtype=np.float32), 16000)
    assert result.label == "uncertain"
    assert result.voiced_frames == 0


def test_bundled_qwen_voice_prototypes_are_separable():
    root = Path(__file__).resolve().parents[1] / "assets" / "voices" / "gender-router"
    male = np.fromfile(root / "male.spk", dtype="<f4")
    female = np.fromfile(root / "female.spk", dtype="<f4")
    assert classify_speaker_embedding(male, male, female)[0] == "male"
    assert classify_speaker_embedding(female, male, female)[0] == "female"


def test_small_embedding_margin_stays_uncertain():
    male = np.array([1.0, 0.0], dtype=np.float32)
    female = np.array([0.0, 1.0], dtype=np.float32)
    middle = np.array([1.0, 1.0], dtype=np.float32)
    assert classify_speaker_embedding(middle, male, female)[0] == "uncertain"
