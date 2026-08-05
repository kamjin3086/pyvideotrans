"""Choose one coherent male/female dubbing palette for an entire video."""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any
import urllib.error
import urllib.request


DEFAULT_PROFILE = (
    Path(__file__).resolve().parents[2]
    / "assets"
    / "voices"
    / "female-styles"
    / "profile.json"
)


def load_style_profile(path: str | Path | None = None) -> dict[str, Any]:
    profile_path = Path(path) if path else DEFAULT_PROFILE
    data = json.loads(profile_path.read_text(encoding="utf-8"))
    styles = data.get("styles")
    fallback = data.get("fallback_style")
    if not isinstance(styles, dict) or fallback not in styles:
        raise ValueError(f"invalid voice style profile: {profile_path}")
    for name, item in styles.items():
        if not isinstance(item, dict) or not item.get("male_voice") or not item.get("female_voice"):
            raise ValueError(f"invalid voice style entry {name!r}: {profile_path}")
    return data


def _sample_subtitles(subtitles: list[dict[str, Any]], limit: int = 48) -> str:
    usable = [str(item.get("text") or "").strip() for item in subtitles]
    usable = [text[:240] for text in usable if text]
    if len(usable) > limit:
        positions = [round(index * (len(usable) - 1) / (limit - 1)) for index in range(limit)]
        usable = [usable[position] for position in positions]
    return "\n".join(usable)[:7000]


def parse_style_response(content: str, allowed_styles: set[str]) -> tuple[str, float, str]:
    """Parse the local LLM's constrained JSON response."""
    match = re.search(r"\{.*\}", content, flags=re.S)
    if not match:
        raise ValueError(f"style response has no JSON object: {content[:200]!r}")
    data = json.loads(match.group(0))
    style = str(data.get("style") or "").strip()
    if style not in allowed_styles:
        raise ValueError(f"unsupported style from classifier: {style!r}")
    confidence = max(0.0, min(1.0, float(data.get("confidence", 0.0))))
    reason = str(data.get("reason") or "").strip()[:300]
    return style, confidence, reason


def _make_plan(
    profile: dict[str, Any],
    style: str,
    confidence: float,
    reason: str,
    *,
    default_male: str,
    male_requested: str,
    female_requested: str,
    classifier: str,
) -> dict[str, Any]:
    selected = profile["styles"][style]
    male_voice = selected["male_voice"] if male_requested == "auto" else default_male
    female_voice = selected["female_voice"] if female_requested == "auto" else female_requested
    return {
        "version": 1,
        "style": style,
        "style_label": selected.get("label", style),
        "description": selected.get("description", ""),
        "confidence": round(confidence, 3),
        "reason": reason,
        "male_voice": male_voice,
        "female_voice": female_voice,
        "male_voice_locked": male_requested != "auto",
        "female_voice_locked": female_requested != "auto",
        "classifier": classifier,
    }


def choose_video_voice_plan(
    source_subtitles: list[dict[str, Any]],
    target_subtitles: list[dict[str, Any]],
    *,
    default_male: str,
    male_requested: str = "auto",
    female_requested: str = "auto",
    api_base: str = "http://127.0.0.1:8101/v1",
    model: str = "Qwen3.6-35B-A3B-instruct",
    profile_path: str | Path | None = None,
    metadata_context: str = "",
    timeout: int = 120,
) -> dict[str, Any]:
    """Use one serial local-LLM call to select a video-wide voice palette."""
    profile = load_style_profile(profile_path)
    fallback = str(profile["fallback_style"])
    minimum_confidence = float(profile.get("minimum_confidence", 0.55))

    if male_requested != "auto" and female_requested != "auto":
        return _make_plan(
            profile, fallback, 1.0, "男女音色均由用户明确指定", default_male=default_male,
            male_requested=male_requested, female_requested=female_requested, classifier="manual",
        )

    categories = {
        name: item.get("label", name)
        for name, item in profile["styles"].items()
    }
    prompt = (
        "判断这个视频的主要内容风格，以便为整段中文配音选择一套一致的男女音色。"
        "只能从给定 style 名称中选一个；按全片主导风格判断，不要因个别句子改变。"
        "只输出 JSON：{\"style\":\"...\",\"confidence\":0到1,\"reason\":\"简短中文原因\"}。\n"
        f"可选风格：{json.dumps(categories, ensure_ascii=False)}\n"
        f"视频元数据：{metadata_context[:3500] or '未提供'}\n"
        f"源语言转录抽样：\n{_sample_subtitles(source_subtitles)}\n"
        f"中文字幕抽样：\n{_sample_subtitles(target_subtitles)}"
    )
    request = urllib.request.Request(
        api_base.rstrip("/") + "/chat/completions",
        data=json.dumps({
            "model": model,
            "messages": [
                {"role": "system", "content": "你是视频配音风格分类器，严格输出一个 JSON 对象。"},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.0,
            "max_tokens": 160,
        }, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
        content = str(body["choices"][0]["message"]["content"])
        style, confidence, reason = parse_style_response(content, set(profile["styles"]))
        if confidence < minimum_confidence:
            return _make_plan(
                profile, fallback, confidence,
                f"分类置信度不足，使用通用方案；原判断 {style}: {reason}",
                default_male=default_male, male_requested=male_requested,
                female_requested=female_requested, classifier="low_confidence_fallback",
            )
        return _make_plan(
            profile, style, confidence, reason, default_male=default_male,
            male_requested=male_requested, female_requested=female_requested,
            classifier="local_llm_serial",
        )
    except (OSError, TimeoutError, urllib.error.URLError, KeyError, IndexError,
            TypeError, ValueError, json.JSONDecodeError) as exc:
        return _make_plan(
            profile, fallback, 0.0, f"风格分类失败，使用通用方案：{exc}",
            default_male=default_male, male_requested=male_requested,
            female_requested=female_requested, classifier="error_fallback",
        )
