import json
from pathlib import Path

from videotrans.process.voice_style import (
    choose_video_voice_plan,
    load_style_profile,
    parse_style_response,
)


class _Response:
    def __init__(self, content: str):
        self.payload = json.dumps({"choices": [{"message": {"content": content}}]}).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.payload


def test_profile_has_ten_complete_clone_voices():
    profile = load_style_profile()
    assert len(profile["styles"]) == 10
    root = Path(__file__).resolve().parents[1] / "assets" / "voices" / "female-styles"
    for item in profile["styles"].values():
        role_dir = root / item["female_voice"]
        assert (role_dir / "reference.spk").stat().st_size == 8192
        assert (role_dir / "reference.rvq").stat().st_size > 1000


def test_parse_style_response_accepts_fenced_json():
    style, confidence, reason = parse_style_response(
        '```json\n{"style":"news","confidence":0.92,"reason":"时事播报"}\n```',
        {"news"},
    )
    assert (style, confidence, reason) == ("news", 0.92, "时事播报")


def test_technology_plan_selects_matching_palette(monkeypatch):
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *_args, **_kwargs: _Response(
            '{"style":"technology","confidence":0.91,"reason":"数码产品评测"}'
        ),
    )
    plan = choose_video_voice_plan(
        [{"text": "这款处理器的性能和续航表现如何"}],
        [{"text": "下面测试这款新电脑"}],
        default_male="serious-male-05",
    )
    assert plan["male_voice"] == "dylan"
    assert plan["female_voice"] == "female-09"
    assert plan["classifier"] == "local_llm_serial"


def test_explicit_male_voice_stays_locked(monkeypatch):
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *_args, **_kwargs: _Response(
            '{"style":"news","confidence":0.95,"reason":"新闻"}'
        ),
    )
    plan = choose_video_voice_plan(
        [], [], default_male="serious-male-05", male_requested="serious-male-05"
    )
    assert plan["male_voice"] == "serious-male-05"
    assert plan["female_voice"] == "female-03"
    assert plan["male_voice_locked"] is True


def test_low_confidence_uses_general_fallback(monkeypatch):
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *_args, **_kwargs: _Response(
            '{"style":"cinematic","confidence":0.3,"reason":"信息不足"}'
        ),
    )
    plan = choose_video_voice_plan([], [], default_male="serious-male-05")
    assert plan["style"] == "general"
    assert plan["male_voice"] == "dylan"
    assert plan["female_voice"] == "female-01"
    assert plan["classifier"] == "low_confidence_fallback"
