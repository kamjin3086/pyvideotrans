#!/usr/bin/env python3
"""Download, translate, dub, subtitle, and validate one supported-language video."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Iterable

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))
import job_runtime  # noqa: E402


DEFAULT_OUTPUT_ROOT = Path.home() / "Videos" / "translated-videos"
# Stay under Hermes TERMINAL_MAX_FOREGROUND_TIMEOUT (default 600s) with headroom
# for process startup and JSON printing.
DEFAULT_TICK_BUDGET_SECONDS = 480
DEFAULT_LLM_API = "http://127.0.0.1:8101/v1"
MEDIA_SUFFIXES = {".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi"}
SUPPORTED_SOURCE_LANGUAGES = {
    "auto": "自动识别",
    "en": "英语",
    "ja": "日语",
    "ko": "韩语",
    "fr": "法语",
    "de": "德语",
    "es": "西班牙语",
    "it": "意大利语",
    "pt": "葡萄牙语",
    "ru": "俄语",
}
VOICE_PROFILES = {
    "auto": "根据视频元数据和转录内容自动选择",
    "dylan": "轻松、愉快、新闻或资讯型北京男声",
    "serious-male-05": "纪录片、知识、严肃或叙事型克隆男声",
}
SERIOUS_VOICE_ROLE = "serious-male-05"
SRT_TIME_RE = re.compile(
    r"(?P<sh>\d{2}):(?P<sm>\d{2}):(?P<ss>\d{2}),(?P<sms>\d{3})\s+-->\s+"
    r"(?P<eh>\d{2}):(?P<em>\d{2}):(?P<es>\d{2}),(?P<ems>\d{3})"
)


class WorkflowError(RuntimeError):
    """A user-actionable workflow error."""


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds")


def expand(value: str | Path) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(str(value)))).resolve()


def executable(candidate: str | Path | None, fallback_name: str) -> Path | None:
    if candidate:
        path = expand(candidate)
        if path.is_file() and os.access(path, os.X_OK):
            return path
    found = shutil.which(fallback_name)
    return Path(found).resolve() if found else None


def first_file(candidates: Iterable[Path]) -> Path | None:
    for path in candidates:
        if path.is_file():
            return path.resolve()
    return None


def resolve_project(explicit: str | None) -> Path:
    candidates: list[Path] = []
    if explicit:
        candidates.append(expand(explicit))
    if os.getenv("PYVIDEOTRANS_HOME"):
        candidates.append(expand(os.environ["PYVIDEOTRANS_HOME"]))
    # Repository-hosted installation: <repo>/skills/<skill>/scripts/this-file.
    candidates.append(Path(__file__).resolve().parents[3])
    candidates.append(expand("~/projects/pyVideoTrans"))
    for candidate in candidates:
        if (candidate / "run_cli_local.sh").is_file() and (candidate / "cli.py").is_file():
            return candidate.resolve()
    rendered = "\n  - ".join(str(item) for item in candidates)
    raise WorkflowError(
        "找不到 pyVideoTrans 项目。检查过：\n  - "
        f"{rendered}\n请先安装当前仓库，或设置 PYVIDEOTRANS_HOME=/绝对路径。"
    )


def resolve_faster_whisper_model(project: Path) -> Path | None:
    env_path = os.getenv("PYVIDEOTRANS_WHISPER_MODEL_DIR")
    candidates = [
        expand(env_path) if env_path else project / "__missing__",
        project / "models" / "models--Systran--faster-whisper-small",
        expand("~/.cache/huggingface/hub/models--Systran--faster-whisper-small"),
    ]
    for candidate in candidates:
        if candidate.exists() and any(candidate.rglob("model.bin")):
            return candidate.resolve()
    return None


def resolve_qwen_assets(project: Path) -> tuple[Path | None, Path | None, Path | None, Path | None]:
    roots: list[Path] = []
    if os.getenv("HERMES_TTS_HOME"):
        roots.append(expand(os.environ["HERMES_TTS_HOME"]))
    roots.extend([project.parent / "hermes-tts-lab", expand("~/projects/hermes-tts-lab")])

    binary = executable(os.getenv("PYVIDEOTRANS_QWENTTS_BIN"), "qwen-tts")
    model = first_file([expand(os.environ["PYVIDEOTRANS_QWENTTS_MODEL"])]) if os.getenv(
        "PYVIDEOTRANS_QWENTTS_MODEL"
    ) else None
    codec = first_file([expand(os.environ["PYVIDEOTRANS_QWENTTS_CODEC"])]) if os.getenv(
        "PYVIDEOTRANS_QWENTTS_CODEC"
    ) else None
    base_model = first_file([expand(os.environ["PYVIDEOTRANS_QWENTTS_BASE_MODEL"])]) if os.getenv(
        "PYVIDEOTRANS_QWENTTS_BASE_MODEL"
    ) else None

    for root in roots:
        if not binary:
            binary = executable(root / "src" / "qwentts.cpp" / "build" / "qwen-tts", "qwen-tts")
        if not model:
            model = first_file(
                [
                    root / "models" / "qwen-talker-1.7b-customvoice-Q8_0.gguf",
                    root / "models" / "qwen-talker-1.7b-customvoice-F16.gguf",
                ]
            )
        if not codec:
            codec = first_file(
                [
                    root / "models" / "qwen-tokenizer-12hz-Q8_0.gguf",
                    root / "models" / "qwen-tokenizer-12hz-F16.gguf",
                ]
            )
        if not base_model:
            base_model = first_file(
                [
                    root / "models" / "qwen-talker-1.7b-base-Q8_0.gguf",
                    root / "models" / "qwen-talker-1.7b-base-F16.gguf",
                ]
            )
    return binary, model, codec, base_model


def check_llm(api_base: str) -> None:
    url = api_base.rstrip("/") + "/models"
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=6) as response:
            if response.status >= 400:
                raise WorkflowError(f"本地 LLM 接口返回 HTTP {response.status}: {url}")
    except (urllib.error.URLError, TimeoutError) as exc:
        raise WorkflowError(
            f"无法连接本地翻译模型接口 {url}。请先确保 localhost:8101/v1 的 Qwen3 服务可用。详情：{exc}"
        ) from exc


def preflight(project_arg: str | None) -> dict[str, Path | str]:
    project = resolve_project(project_arg)
    missing: list[str] = []

    python = project / ".venv" / "bin" / "python"
    run_cli = project / "run_cli_local.sh"
    yt_dlp = executable(os.getenv("YTDLP_BIN"), "yt-dlp")
    ffmpeg = executable(os.getenv("FFMPEG_BIN"), "ffmpeg")
    ffprobe = executable(os.getenv("FFPROBE_BIN"), "ffprobe")
    demucs = executable(os.getenv("PYVIDEOTRANS_DEMUCS_BIN"), "demucs")
    qwen_bin, qwen_model, qwen_codec, qwen_base_model = resolve_qwen_assets(project)
    qwen_codec_bin = qwen_bin.with_name("qwen-codec") if qwen_bin else None
    whisper_model = resolve_faster_whisper_model(project)
    clone_dir = project / "assets" / "voices" / SERIOUS_VOICE_ROLE
    clone_spk = clone_dir / "reference.spk"
    clone_rvq = clone_dir / "reference.rvq"
    clone_text = clone_dir / "reference.txt"
    router_dir = project / "assets" / "voices" / "gender-router"
    male_prototype = router_dir / "male.spk"
    female_prototype = router_dir / "female.spk"
    female_styles = project / "assets" / "voices" / "female-styles"
    voice_style_profile = female_styles / "profile.json"
    female_reference_text = female_styles / "reference.txt"

    if not python.is_file():
        missing.append(f"项目虚拟环境：{python}（需按仓库 LOCAL_SETUP.md 安装依赖）")
    if not run_cli.is_file():
        missing.append(f"工作流入口：{run_cli}")
    if not yt_dlp:
        missing.append("yt-dlp 命令（Python 包通常约数 MB）")
    if not ffmpeg:
        missing.append("ffmpeg 命令（系统依赖，体积因发行版而异）")
    if not ffprobe:
        missing.append("ffprobe 命令（通常随 ffmpeg 一起安装）")
    if not demucs:
        missing.append("demucs 命令及其 Python/PyTorch 环境（可能需要数 GB）")
    if not whisper_model:
        missing.append("faster-whisper small 模型（约 464 MB）")
    if not qwen_bin:
        missing.append("Qwen TTS CLI 可执行文件 qwen-tts")
    if not qwen_codec_bin or not qwen_codec_bin.is_file():
        missing.append("Qwen speaker encoder CLI 可执行文件 qwen-codec")
    if not qwen_model:
        missing.append("Qwen CustomVoice 1.7B GGUF 模型（Q8 约 2 GB）")
    if not qwen_codec:
        missing.append("Qwen 12Hz tokenizer/codec GGUF 模型（Q8 约 278 MB）")
    if not qwen_base_model:
        missing.append("Qwen Base 1.7B GGUF 克隆模型（Q8 约 2 GB）")
    for label, path in (("SPK", clone_spk), ("RVQ", clone_rvq), ("参考文本", clone_text)):
        if not path.is_file() or path.stat().st_size == 0:
            missing.append(f"{SERIOUS_VOICE_ROLE} 音色资产 {label}：{path}")
    for label, path in (("男声", male_prototype), ("女声", female_prototype)):
        if not path.is_file() or path.stat().st_size != 8192:
            missing.append(f"自动声线路由 {label}原型：{path}")
    if not voice_style_profile.is_file() or not female_reference_text.is_file():
        missing.append(f"视频风格音色配置：{voice_style_profile}")
    for number in range(1, 11):
        role = f"female-{number:02d}"
        role_dir = female_styles / role
        spk = role_dir / "reference.spk"
        rvq = role_dir / "reference.rvq"
        if not spk.is_file() or spk.stat().st_size != 8192 or not rvq.is_file() or rvq.stat().st_size == 0:
            missing.append(f"{role} 固定音色资产：{role_dir}")
    if missing:
        raise WorkflowError(
            "预检发现缺失项，未下载视频、未安装依赖：\n  - "
            + "\n  - ".join(missing)
            + "\n请先告知用户并取得许可，再安装或下载大文件。"
        )

    llm_api = os.getenv("PYVIDEOTRANS_LLM_API", DEFAULT_LLM_API)
    check_llm(llm_api)
    return {
        "project": project,
        "python": python.resolve(),
        "run_cli": run_cli.resolve(),
        "yt_dlp": yt_dlp,
        "ffmpeg": ffmpeg,
        "ffprobe": ffprobe,
        "demucs": demucs,
        "qwen_bin": qwen_bin,
        "qwen_codec_bin": qwen_codec_bin.resolve(),
        "qwen_model": qwen_model,
        "qwen_codec": qwen_codec,
        "qwen_base_model": qwen_base_model,
        "clone_spk": clone_spk.resolve(),
        "clone_rvq": clone_rvq.resolve(),
        "clone_text": clone_text.resolve(),
        "male_prototype": male_prototype.resolve(),
        "female_prototype": female_prototype.resolve(),
        "female_styles": female_styles.resolve(),
        "voice_style_profile": voice_style_profile.resolve(),
        "whisper_model": whisper_model,
        "llm_api": llm_api,
    }


def print_preflight(config: dict[str, Path | str]) -> None:
    print("[preflight] 所需程序、模型和本地 LLM 均已就绪：")
    for key in (
        "project",
        "yt_dlp",
        "ffmpeg",
        "demucs",
        "whisper_model",
        "qwen_bin",
        "qwen_codec_bin",
        "qwen_model",
        "qwen_codec",
        "qwen_base_model",
        "clone_spk",
        "clone_rvq",
        "male_prototype",
        "female_prototype",
        "female_styles",
        "voice_style_profile",
        "llm_api",
    ):
        print(f"  {key}: {config[key]}")


def run_capture(command: list[str], cwd: Path | None = None) -> str:
    process = subprocess.run(command, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if process.returncode:
        detail = process.stderr.strip() or process.stdout.strip()
        raise WorkflowError(f"命令执行失败（{process.returncode}）：{' '.join(command)}\n{detail[-4000:]}")
    return process.stdout


def run_logged(command: list[str], log_path: Path, cwd: Path, env: dict[str, str] | None = None) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"[run] {' '.join(command)}")
    with log_path.open("a", encoding="utf-8") as log:
        log.write(f"\n[{now_iso()}] $ {' '.join(command)}\n")
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
        )
        assert process.stdout is not None
        for line in process.stdout:
            print(line, end="", flush=True)
            log.write(line)
            log.flush()
        return_code = process.wait()
    if return_code:
        raise WorkflowError(f"命令执行失败，退出码 {return_code}。日志：{log_path}")


def cookie_args(browser: str | None) -> list[str]:
    return ["--cookies-from-browser", browser] if browser else []


def load_remote_metadata(yt_dlp: Path, source: str, browser: str | None) -> dict[str, Any]:
    command = [str(yt_dlp), "--no-playlist", "--dump-single-json", "--skip-download"]
    command.extend(cookie_args(browser))
    command.append(source)
    raw = run_capture(command)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise WorkflowError("yt-dlp 未返回可解析的视频元数据。") from exc


def safe_identifier(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-.")
    return cleaned[:100] or "video"


def local_identifier(path: Path) -> str:
    digest = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:10]
    return safe_identifier(f"{path.stem}-{digest}")


def locate_download(source_dir: Path) -> Path:
    candidates = [
        item
        for item in source_dir.iterdir()
        if item.is_file() and item.suffix.lower() in MEDIA_SUFFIXES and not item.name.endswith(".part")
    ]
    if not candidates:
        raise WorkflowError(f"下载完成但未在 {source_dir} 找到视频文件。")
    return max(candidates, key=lambda item: item.stat().st_size).resolve()


def download_video(
    config: dict[str, Path | str],
    source: str,
    source_dir: Path,
    log_path: Path,
    max_height: int,
    browser: str | None,
) -> Path:
    source_dir.mkdir(parents=True, exist_ok=True)
    existing = [item for item in source_dir.iterdir() if item.suffix.lower() in MEDIA_SUFFIXES]
    if existing:
        selected = locate_download(source_dir)
        print(f"[download] 复用已下载源视频：{selected}")
        return selected

    command = [
        str(config["yt_dlp"]),
        "--no-playlist",
        "--continue",
        "--no-overwrites",
        "--newline",
        "-f",
        f"bv*[height<={max_height}]+ba/b[height<={max_height}]/b",
        "--merge-output-format",
        "mp4",
        "-o",
        str(source_dir / "source.%(ext)s"),
    ]
    command.extend(cookie_args(browser))
    command.append(source)
    run_logged(command, log_path, Path(config["project"]))
    return locate_download(source_dir)


def probe_json(ffprobe: Path, media: Path) -> dict[str, Any]:
    raw = run_capture(
        [
            str(ffprobe),
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(media),
        ]
    )
    return json.loads(raw)


def parse_srt_intervals(path: Path) -> list[tuple[float, float]]:
    def seconds(groups: dict[str, str], prefix: str) -> float:
        return (
            int(groups[f"{prefix}h"]) * 3600
            + int(groups[f"{prefix}m"]) * 60
            + int(groups[f"{prefix}s"])
            + int(groups[f"{prefix}ms"]) / 1000
        )

    intervals: list[tuple[float, float]] = []
    for match in SRT_TIME_RE.finditer(path.read_text(encoding="utf-8", errors="replace")):
        groups = match.groupdict()
        intervals.append((seconds(groups, "s"), seconds(groups, "e")))
    return intervals


def silent_gaps(intervals: list[tuple[float, float]], duration: float) -> list[tuple[float, float]]:
    gaps: list[tuple[float, float]] = []
    cursor = 0.0
    for start, end in sorted(intervals):
        if start - cursor >= 1.5:
            gaps.append((cursor, start))
        cursor = max(cursor, end)
    if duration - cursor >= 1.5:
        gaps.append((cursor, duration))
    return sorted(gaps, key=lambda item: item[1] - item[0], reverse=True)


def mean_volume(ffmpeg: Path, media: Path, start: float, length: float) -> float | None:
    command = [
        str(ffmpeg),
        "-hide_banner",
        "-nostats",
        "-ss",
        f"{start:.3f}",
        "-t",
        f"{length:.3f}",
        "-i",
        str(media),
        "-vn",
        "-af",
        "volumedetect",
        "-f",
        "null",
        "-",
    ]
    process = subprocess.run(command, text=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    match = re.search(r"mean_volume:\s*(-?(?:inf|\d+(?:\.\d+)?))\s*dB", process.stderr)
    if not match or match.group(1) == "-inf":
        return None
    return float(match.group(1))


def validate_result(final_video: Path, config: dict[str, Path | str]) -> dict[str, Any]:
    if not final_video.is_file() or final_video.stat().st_size < 100_000:
        raise WorkflowError(f"成片不存在或文件过小：{final_video}")

    data = probe_json(Path(config["ffprobe"]), final_video)
    streams = data.get("streams", [])
    video_streams = [item for item in streams if item.get("codec_type") == "video"]
    audio_streams = [item for item in streams if item.get("codec_type") == "audio"]
    duration = float(data.get("format", {}).get("duration") or 0)
    if not video_streams or not audio_streams or duration <= 1:
        raise WorkflowError("成片校验失败：缺少视频流、音频流，或时长异常。")

    result_dir = final_video.parent
    artifacts = {
        "instrument": result_dir / "instrument.wav",
        "vocal": result_dir / "vocal.wav",
        "subtitles": result_dir / "zh-cn.srt",
    }
    if os.getenv("PYVIDEOTRANS_AUTO_VOICE_GENDER", "1") == "1":
        artifacts["voice_routing"] = result_dir / "voice-routing.json"
    if os.getenv("PYVIDEOTRANS_AUTO_VOICE_STYLE", "1") == "1":
        artifacts["voice_style_plan"] = result_dir / "voice-style-plan.json"
    absent = [f"{name}: {path}" for name, path in artifacts.items() if not path.is_file() or path.stat().st_size < 100]
    if absent:
        raise WorkflowError("成片存在，但缺少关键工作流产物：\n  - " + "\n  - ".join(absent))

    intervals = parse_srt_intervals(artifacts["subtitles"])
    if not intervals:
        raise WorkflowError(f"中文字幕为空或时间轴不可解析：{artifacts['subtitles']}")

    background_samples: list[dict[str, float]] = []
    for gap_start, gap_end in silent_gaps(intervals, duration)[:5]:
        start = gap_start + 0.2
        length = min(4.0, gap_end - gap_start - 0.4)
        if length < 0.8:
            continue
        instrument_db = mean_volume(Path(config["ffmpeg"]), artifacts["instrument"], start, length)
        final_db = mean_volume(Path(config["ffmpeg"]), final_video, start, length)
        if instrument_db is None or instrument_db < -45 or final_db is None:
            continue
        sample = {
            "start_seconds": round(start, 3),
            "duration_seconds": round(length, 3),
            "instrument_mean_db": instrument_db,
            "final_mean_db": final_db,
        }
        background_samples.append(sample)
        if final_db < instrument_db - 12:
            raise WorkflowError(
                "背景音校验失败：在无中文配音片段中，成片音量比分离出的背景轨低超过 12 dB。"
                f" 样本：{sample}"
            )
        if len(background_samples) >= 3:
            break

    result: dict[str, Any] = {
        "ok": True,
        "duration_seconds": round(duration, 3),
        "video_codec": video_streams[0].get("codec_name"),
        "audio_codec": audio_streams[0].get("codec_name"),
        "subtitle_cues": len(intervals),
        "artifacts": {name: str(path.resolve()) for name, path in artifacts.items()},
        "background_samples": background_samples,
    }
    routing_path = artifacts.get("voice_routing")
    if routing_path:
        try:
            routing = json.loads(routing_path.read_text(encoding="utf-8"))
            routing_counts = routing["counts"]
            if sum(int(value) for value in routing_counts.values()) != len(intervals):
                raise ValueError("路由行数与中文字幕条数不一致")
            result["voice_routing_counts"] = routing_counts
            result["female_voice"] = routing.get("female_voice")
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise WorkflowError(f"自动声线路由报告无效：{routing_path}: {exc}") from exc
    style_path = artifacts.get("voice_style_plan")
    if style_path:
        try:
            voice_plan = json.loads(style_path.read_text(encoding="utf-8"))
            if voice_plan.get("style") not in {
                "general", "documentary", "news", "knowledge", "lifestyle",
                "professional", "human_story", "culture", "technology", "cinematic",
            }:
                raise ValueError("未知的视频风格")
            if not voice_plan.get("male_voice") or not voice_plan.get("female_voice"):
                raise ValueError("缺少男女音色选择")
            result["voice_style_plan"] = voice_plan
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise WorkflowError(f"视频风格音色报告无效：{style_path}: {exc}") from exc
    if not background_samples:
        result["background_validation_note"] = "未找到同时满足长度和响度阈值的纯背景抽样区间；关键背景轨文件已确认存在。"
    return result


def translation_environment(
    config: dict[str, Path | str],
    voice_profile_requested: str,
    metadata: dict[str, Any],
) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "PYVIDEOTRANS_HOME": str(config["project"]),
            "PYVIDEOTRANS_DEMUCS_BIN": str(config["demucs"]),
            "PYVIDEOTRANS_DEMUCS_MODEL": os.getenv("PYVIDEOTRANS_DEMUCS_MODEL", "htdemucs"),
            "PYVIDEOTRANS_DEMUCS_DEVICE": os.getenv("PYVIDEOTRANS_DEMUCS_DEVICE", "cuda"),
            "PYVIDEOTRANS_QWENTTS_BIN": str(config["qwen_bin"]),
            "PYVIDEOTRANS_QWENTTS_MODEL": str(config["qwen_model"]),
            "PYVIDEOTRANS_QWENTTS_BASE_MODEL": str(config["qwen_base_model"]),
            "PYVIDEOTRANS_QWENTTS_CODEC": str(config["qwen_codec"]),
            "PYVIDEOTRANS_QWENTTS_CLONE_ROLE": SERIOUS_VOICE_ROLE,
            "PYVIDEOTRANS_QWENTTS_CLONE_SPK": str(config["clone_spk"]),
            "PYVIDEOTRANS_QWENTTS_CLONE_RVQ": str(config["clone_rvq"]),
            "PYVIDEOTRANS_QWENTTS_CLONE_TEXT": str(config["clone_text"]),
            "PYVIDEOTRANS_QWENTTS_CLONE_ROOT": str(config["female_styles"]),
            "PYVIDEOTRANS_AUTO_VOICE_STYLE": os.getenv("PYVIDEOTRANS_AUTO_VOICE_STYLE", "1"),
            "PYVIDEOTRANS_VOICE_STYLE_PROFILE": str(config["voice_style_profile"]),
            "PYVIDEOTRANS_VOICE_PROFILE_REQUESTED": voice_profile_requested,
            "PYVIDEOTRANS_FEMALE_VOICE_PROFILE": os.getenv(
                "PYVIDEOTRANS_FEMALE_VOICE_PROFILE", "auto"
            ),
            "PYVIDEOTRANS_VIDEO_STYLE_CONTEXT": json.dumps({
                "title": metadata.get("title"),
                "uploader": metadata.get("uploader") or metadata.get("channel"),
                "categories": metadata.get("categories"),
                "tags": (metadata.get("tags") or [])[:30],
                "description": str(metadata.get("description") or "")[:3000],
            }, ensure_ascii=False),
            "PYVIDEOTRANS_AUTO_VOICE_GENDER": os.getenv("PYVIDEOTRANS_AUTO_VOICE_GENDER", "1"),
            "PYVIDEOTRANS_QWENTTS_FEMALE_VOICE": os.getenv(
                "PYVIDEOTRANS_QWENTTS_FEMALE_VOICE", "female-01"
            ),
            "PYVIDEOTRANS_QWEN_CODEC_BIN": str(config["qwen_codec_bin"]),
            "PYVIDEOTRANS_VOICE_MALE_PROTOTYPE": str(config["male_prototype"]),
            "PYVIDEOTRANS_VOICE_FEMALE_PROTOTYPE": str(config["female_prototype"]),
            "PYVIDEOTRANS_LLM_API": str(config["llm_api"]),
            "PYVIDEOTRANS_LLM_MODEL": os.getenv(
                "PYVIDEOTRANS_LLM_MODEL", "Qwen3.6-35B-A3B-instruct"
            ),
        }
    )
    return env


def run_translation(
    config: dict[str, Path | str],
    source_video: Path,
    result_dir: Path,
    log_path: Path,
    manifest_path: Path,
    source_language: str,
    voice_role: str,
    voice_profile_requested: str,
    metadata: dict[str, Any],
    force: bool,
) -> Path:
    expected = result_dir / f"{source_video.stem}.mp4"
    previous_language = None
    previous_voice = None
    previous_auto_voice_routing = None
    previous_auto_voice_style = None
    previous_voice_requested = None
    if manifest_path.is_file():
        try:
            previous = json.loads(manifest_path.read_text(encoding="utf-8"))
            previous_language = previous.get("settings", {}).get("source_language")
            previous_voice = previous.get("settings", {}).get("voice")
            previous_voice_requested = previous.get("settings", {}).get("voice_profile_requested")
            previous_auto_voice_routing = previous.get("settings", {}).get(
                "automatic_acoustic_voice_routing"
            )
            previous_auto_voice_style = previous.get("settings", {}).get(
                "automatic_video_style_routing"
            )
        except (OSError, json.JSONDecodeError, AttributeError):
            previous_language = None
            previous_voice = None
            previous_voice_requested = None
            previous_auto_voice_routing = None
            previous_auto_voice_style = None

    if (
        expected.is_file()
        and not force
        and previous_language == source_language
        and previous_voice_requested == voice_profile_requested
        and previous_auto_voice_routing == (
            os.getenv("PYVIDEOTRANS_AUTO_VOICE_GENDER", "1") == "1"
        )
        and previous_auto_voice_style == (
            os.getenv("PYVIDEOTRANS_AUTO_VOICE_STYLE", "1") == "1"
        )
    ):
        print(f"[translate] 复用已存在成片并重新校验：{expected}")
        return expected.resolve()
    if expected.is_file() and not force:
        print(
            "[translate] 已有成片的源语言或音色配置不匹配，重新执行："
            f"language={previous_language or 'unknown'}->{source_language}, "
            f"voice_request={previous_voice_requested or previous_voice or 'unknown'}->{voice_profile_requested}, "
            f"auto_gender={previous_auto_voice_routing}->{os.getenv('PYVIDEOTRANS_AUTO_VOICE_GENDER', '1')}, "
            f"auto_style={previous_auto_voice_style}->{os.getenv('PYVIDEOTRANS_AUTO_VOICE_STYLE', '1')}"
        )

    result_dir.mkdir(parents=True, exist_ok=True)
    command = [
        str(config["run_cli"]),
        "--task",
        "vtv",
        "--name",
        str(source_video),
        "--output-dir",
        str(result_dir),
        "--recogn_type",
        "0",
        "--model_name",
        "small",
        "--detect_language",
        source_language,
        "--translate_type",
        "9",
        "--source_language_code",
        source_language,
        "--target_language_code",
        "zh-cn",
        "--tts_type",
        "20",
        "--voice_role",
        voice_role,
        "--subtitle_type",
        "1",
        "--voice_autorate",
        "--align_sub_audio",
        "--is_separate",
    ]
    # CLI defaults clear_cache=True (wipes target+cache at start). Resume needs
    # the opposite; only --force may wipe.
    command.append("--clear_cache" if force else "--no-clear-cache")
    run_logged(
        command,
        log_path,
        Path(config["project"]),
        translation_environment(config, voice_profile_requested, metadata),
    )
    if expected.is_file():
        return expected.resolve()
    candidates = list(result_dir.glob("*.mp4"))
    if not candidates:
        raise WorkflowError(f"翻译流程结束，但 {result_dir} 内没有生成 MP4 成片。")
    return max(candidates, key=lambda item: item.stat().st_size).resolve()


def write_manifest(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def emit_tick_payload(payload: dict[str, Any]) -> None:
    """Print one machine-readable checkpoint for Hermes to loop on."""
    print("[tick]")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def resolve_voice_role(voice_profile: str) -> str:
    if voice_profile == "auto":
        print("[voice] 男女音色将在转录后按全片风格自动选择。")
        return SERIOUS_VOICE_ROLE
    return voice_profile


def prepare_job_paths(
    config: dict[str, Path | str],
    source: str,
    output_root: str,
    max_height: int,
    cookies_browser: str | None,
    source_language: str,
    voice_profile: str,
) -> dict[str, Any]:
    local_source = expand(source) if not re.match(r"^https?://", source, flags=re.I) else None
    if local_source:
        if not local_source.is_file():
            raise WorkflowError(f"本地视频不存在：{local_source}")
        metadata: dict[str, Any] = {
            "id": local_identifier(local_source),
            "title": local_source.stem,
            "webpage_url": None,
        }
    else:
        print("[metadata] 正在读取远程视频信息……")
        metadata = load_remote_metadata(Path(config["yt_dlp"]), source, cookies_browser)

    voice_role = resolve_voice_role(voice_profile)
    video_id = safe_identifier(str(metadata.get("id") or hashlib.sha256(source.encode()).hexdigest()[:12]))
    job_dir = expand(output_root) / video_id
    source_dir = job_dir / "source"
    result_dir = job_dir / "result"
    log_path = job_dir / "workflow.log"
    manifest_path = job_dir / "job.json"
    job_dir.mkdir(parents=True, exist_ok=True)

    if local_source:
        source_video = local_source
        print(f"[source] 使用本地视频：{source_video}")
    else:
        source_video = download_video(
            config,
            source,
            source_dir,
            log_path,
            max_height,
            cookies_browser,
        )

    settings = {
        "source_language": source_language,
        "target_language": "zh-cn",
        "voice_profile_requested": voice_profile,
        "voice_role": voice_role,
        "automatic_video_style_routing": os.getenv("PYVIDEOTRANS_AUTO_VOICE_STYLE", "1") == "1",
        "automatic_acoustic_voice_routing": os.getenv("PYVIDEOTRANS_AUTO_VOICE_GENDER", "1") == "1",
        "llm_api": str(config["llm_api"]),
        "separation": "demucs two-stems vocals",
        "subtitles": "hard-burned",
        "max_download_height": max_height,
        "cookies_from_browser": cookies_browser,
    }
    seed_manifest = {
        "status": "prepared",
        "source_request": source,
        "source_video": str(source_video),
        "video_id": video_id,
        "title": metadata.get("title"),
        "metadata": {
            "title": metadata.get("title"),
            "uploader": metadata.get("uploader") or metadata.get("channel"),
            "categories": metadata.get("categories"),
            "tags": (metadata.get("tags") or [])[:30],
            "description": str(metadata.get("description") or "")[:3000],
        },
        "job_directory": str(job_dir),
        "settings": settings,
        "started_at": now_iso(),
        "log": str(log_path),
    }
    write_manifest(manifest_path, seed_manifest)
    return {
        "job_dir": job_dir,
        "result_dir": result_dir,
        "log_path": log_path,
        "manifest_path": manifest_path,
        "source_video": source_video,
        "video_id": video_id,
        "metadata": metadata,
        "voice_role": voice_role,
        "settings": settings,
        "seed_manifest": seed_manifest,
    }


def completed_payload(
    *,
    final_video: Path,
    job_dir: Path,
    manifest_path: Path,
    validation: dict[str, Any],
) -> dict[str, Any]:
    return {
        "status": "completed",
        "phase": "done",
        "final_video": str(final_video),
        "job_directory": str(job_dir),
        "manifest": str(manifest_path),
        "validation_ok": True,
        "next_action": "report_success_to_user",
        "message": "video translation completed",
        "voice_style": validation.get("voice_style_plan", {}).get("style"),
        "female_voice": validation.get("voice_style_plan", {}).get("female_voice"),
    }


def finish_job(
    config: dict[str, Path | str],
    *,
    source_video: Path,
    result_dir: Path,
    log_path: Path,
    manifest_path: Path,
    job_dir: Path,
    video_id: str,
    source_request: str,
    metadata: dict[str, Any],
    source_language: str,
    voice_role: str,
    voice_profile: str,
    force: bool,
    started_at: str,
) -> tuple[Path, dict[str, Any]]:
    print(
        "[translate] 开始转译。Demucs 会使用本机 GPU；worker 期间请避免并发占用同一 GPU 的本地 LLM 长请求。"
    )
    job_runtime.write_runtime(
        job_dir,
        {
            "status": "running",
            "phase": "translate",
            "worker_pid": os.getpid(),
            "message": "running pyVideoTrans CLI",
            "heartbeat_at": now_iso(),
        },
    )
    final_video = run_translation(
        config,
        source_video,
        result_dir,
        log_path,
        manifest_path,
        source_language,
        voice_role,
        voice_profile,
        metadata,
        force,
    )
    job_runtime.write_runtime(
        job_dir,
        {
            "status": "running",
            "phase": "validate",
            "worker_pid": os.getpid(),
            "message": f"validating {final_video}",
            "heartbeat_at": now_iso(),
        },
    )
    print(f"[validate] 正在校验成片和背景声：{final_video}")
    validation = validate_result(final_video, config)
    manifest: dict[str, Any] = {
        "status": "completed",
        "source_request": source_request,
        "source_video": str(source_video),
        "video_id": video_id,
        "title": metadata.get("title"),
        "final_video": str(final_video),
        "job_directory": str(job_dir),
        "settings": {
            "source_language": source_language,
            "target_language": "zh-cn",
            "voice_profile_requested": voice_profile,
            "voice": validation.get("voice_style_plan", {}).get("male_voice", voice_role),
            "automatic_video_style_routing": os.getenv("PYVIDEOTRANS_AUTO_VOICE_STYLE", "1") == "1",
            "video_style": validation.get("voice_style_plan", {}).get("style"),
            "automatic_acoustic_voice_routing": os.getenv("PYVIDEOTRANS_AUTO_VOICE_GENDER", "1") == "1",
            "female_voice": validation.get("voice_style_plan", {}).get(
                "female_voice",
                os.getenv("PYVIDEOTRANS_QWENTTS_FEMALE_VOICE", "female-01"),
            ),
            "llm_api": config["llm_api"],
            "separation": "demucs two-stems vocals",
            "subtitles": "hard-burned",
        },
        "validation": validation,
        "started_at": started_at,
        "completed_at": now_iso(),
        "log": str(log_path),
    }
    write_manifest(manifest_path, manifest)
    return final_video, validation


def run_worker(job_dir: Path, project_dir: str | None) -> int:
    job_dir = expand(str(job_dir))
    manifest_path = job_dir / "job.json"
    if not manifest_path.is_file():
        raise WorkflowError(f"worker 缺少 job.json：{manifest_path}")
    try:
        job = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkflowError(f"无法读取 job.json：{exc}") from exc

    config = preflight(project_dir)
    settings = job.get("settings") or {}
    source_video = expand(job["source_video"])
    result_dir = job_dir / "result"
    log_path = expand(job.get("log") or str(job_dir / "workflow.log"))
    metadata = dict(job.get("metadata") or {})
    if not metadata.get("title"):
        metadata["title"] = job.get("title")
    source_language = str(settings.get("source_language") or "auto")
    voice_profile = str(settings.get("voice_profile_requested") or "auto")
    voice_role = str(settings.get("voice_role") or resolve_voice_role(voice_profile))
    force = bool(job.get("force"))
    started_at = str(job.get("started_at") or now_iso())
    if force:
        # Consume one-shot wipe flag so a later reconnect cannot wipe again.
        job["force"] = False
        write_manifest(manifest_path, job)

    try:
        job_runtime.write_runtime(
            job_dir,
            {
                "status": "running",
                "phase": "worker_running",
                "worker_pid": os.getpid(),
                "started_at": started_at,
                "heartbeat_at": now_iso(),
                "message": "worker process active",
            },
        )
        final_video, validation = finish_job(
            config,
            source_video=source_video,
            result_dir=result_dir,
            log_path=log_path,
            manifest_path=manifest_path,
            job_dir=job_dir,
            video_id=str(job.get("video_id") or job_dir.name),
            source_request=str(job.get("source_request") or source_video),
            metadata=metadata,
            source_language=source_language,
            voice_role=voice_role,
            voice_profile=voice_profile,
            force=force,
            started_at=started_at,
        )
        payload = completed_payload(
            final_video=final_video,
            job_dir=job_dir,
            manifest_path=manifest_path,
            validation=validation,
        )
        job_runtime.write_runtime(
            job_dir,
            {
                "status": "completed",
                "phase": "done",
                "worker_pid": os.getpid(),
                "final_video": str(final_video),
                "message": "completed",
                "heartbeat_at": now_iso(),
            },
        )
        print("[complete] 视频翻译完成。")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        job_runtime.write_runtime(
            job_dir,
            {
                "status": "failed",
                "phase": "error",
                "worker_pid": os.getpid(),
                "message": str(exc),
                "error": str(exc),
                "heartbeat_at": now_iso(),
            },
        )
        raise


def ensure_worker(
    config: dict[str, Path | str],
    prepared: dict[str, Any],
    *,
    force: bool,
    script_path: Path,
) -> dict[str, Any]:
    job_dir: Path = prepared["job_dir"]
    manifest_path: Path = prepared["manifest_path"]
    runtime = job_runtime.read_runtime(job_dir)
    pid = job_runtime.read_worker_pid(job_dir)
    alive = bool(pid and job_runtime.pid_is_alive(pid))

    if force:
        print("[tick] --force：停止旧 worker 并清理后重启")
        job_runtime.stop_worker(job_dir)
        job = json.loads(manifest_path.read_text(encoding="utf-8"))
        job["force"] = True
        job["status"] = "prepared"
        job["started_at"] = now_iso()
        write_manifest(manifest_path, job)
        alive = False
        runtime = {}

    if runtime.get("status") == "completed" and not force:
        return runtime
    if runtime.get("status") == "failed" and not force and not alive:
        return runtime
    if alive:
        print(f"[tick] 复用运行中的 worker pid={pid}")
        return runtime
    if runtime.get("status") == "running" and pid and not alive:
        print("[tick] worker 异常退出且未写完状态，将以 resume（--no-clear-cache）重启")
        runtime = {}
    elif runtime and not alive and runtime.get("status") not in {"completed", "failed", None, ""}:
        # Stale non-terminal status without a live process.
        print(f"[tick] 清理陈旧 runtime status={runtime.get('status')!r} 并 resume 重启")
        runtime = {}

    worker_cmd = [
        sys.executable,
        str(script_path),
        "--worker",
        "--job-dir",
        str(job_dir),
    ]
    if config.get("project"):
        worker_cmd.extend(["--project-dir", str(config["project"])])
    print(f"[tick] 启动分离 worker：{' '.join(worker_cmd)}")
    job_runtime.start_detached_worker(
        worker_cmd,
        job_dir,
        Path(config["project"]),
        os.environ.copy(),
    )
    return job_runtime.read_runtime(job_dir)


def run_tick(
    config: dict[str, Path | str],
    *,
    source: str | None,
    job_dir_arg: str | None,
    output_root: str,
    max_height: int,
    cookies_browser: str | None,
    source_language: str,
    voice_profile: str,
    force: bool,
    budget_seconds: float,
) -> int:
    script_path = Path(__file__).resolve()
    if job_dir_arg:
        job_dir = expand(job_dir_arg)
        manifest_path = job_dir / "job.json"
        if not manifest_path.is_file():
            raise WorkflowError(f"--job-dir 缺少 job.json：{manifest_path}")
        job = json.loads(manifest_path.read_text(encoding="utf-8"))
        prepared = {
            "job_dir": job_dir,
            "result_dir": job_dir / "result",
            "log_path": expand(job.get("log") or str(job_dir / "workflow.log")),
            "manifest_path": manifest_path,
            "source_video": expand(job["source_video"]),
            "video_id": job.get("video_id") or job_dir.name,
            "metadata": job.get("metadata") or {"title": job.get("title")},
            "voice_role": (job.get("settings") or {}).get("voice_role") or SERIOUS_VOICE_ROLE,
            "settings": job.get("settings") or {},
            "seed_manifest": job,
        }
    else:
        if not source:
            raise WorkflowError("缺少视频 URL / 本地路径，或 --job-dir。")
        prepared = prepare_job_paths(
            config,
            source,
            output_root,
            max_height,
            cookies_browser,
            source_language,
            voice_profile,
        )

    job_dir = prepared["job_dir"]
    runtime = ensure_worker(config, prepared, force=force, script_path=script_path)
    if runtime.get("status") == "completed":
        job = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
        final_path = expand(job.get("final_video") or "")
        if final_path.is_file():
            payload = completed_payload(
                final_video=final_path,
                job_dir=job_dir,
                manifest_path=job_dir / "job.json",
                validation=job.get("validation") or {},
            )
            emit_tick_payload(payload)
            return 0
        raise WorkflowError("runtime 标记 completed，但找不到 final_video。")

    if runtime.get("status") == "failed":
        payload = {
            "status": "failed",
            "phase": runtime.get("phase") or "error",
            "job_directory": str(job_dir),
            "message": runtime.get("message") or runtime.get("error") or "worker failed",
            "next_action": "report_failure_to_user",
            "runtime": runtime,
            "log_tail": job_runtime.tail_text(job_runtime.worker_log_path(job_dir)),
        }
        emit_tick_payload(payload)
        return 2

    print(f"[tick] 等待 worker，预算 {budget_seconds:.0f}s（Hermes 前台硬顶约 600s）……")
    waited = job_runtime.wait_for_runtime(job_dir, budget_seconds=budget_seconds)
    status = str(waited.get("status") or "running")
    if status == "completed":
        job = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
        final_path = expand(job["final_video"])
        payload = completed_payload(
            final_video=final_path,
            job_dir=job_dir,
            manifest_path=job_dir / "job.json",
            validation=job.get("validation") or {},
        )
        emit_tick_payload(payload)
        return 0
    if status == "failed":
        payload = {
            "status": "failed",
            "phase": waited.get("phase") or "error",
            "job_directory": str(job_dir),
            "message": waited.get("message") or waited.get("error") or "worker failed",
            "next_action": "report_failure_to_user",
            "runtime": waited,
            "log_tail": job_runtime.tail_text(job_runtime.worker_log_path(job_dir)),
        }
        emit_tick_payload(payload)
        return 2

    payload = {
        "status": "in_progress",
        "phase": waited.get("phase") or "worker_running",
        "job_directory": str(job_dir),
        "worker_alive": bool(waited.get("worker_alive")),
        "worker_pid": job_runtime.read_worker_pid(job_dir),
        "message": waited.get("message")
        or "tick budget exhausted; worker still running",
        "next_action": "immediately_call_tick_again",
        "tick_command": (
            f'python3 "{script_path}" --tick --job-dir "{job_dir}" '
            f"--budget-seconds {int(budget_seconds)}"
        ),
        "runtime": waited,
        "log_tail": job_runtime.tail_text(job_runtime.worker_log_path(job_dir)),
    }
    emit_tick_payload(payload)
    # Exit 0 so Hermes does not treat an in-progress checkpoint as a tool failure.
    return 0


def run_full_sync(
    config: dict[str, Path | str],
    *,
    source: str,
    output_root: str,
    max_height: int,
    cookies_browser: str | None,
    source_language: str,
    voice_profile: str,
    force: bool,
) -> int:
    started_at = now_iso()
    prepared = prepare_job_paths(
        config,
        source,
        output_root,
        max_height,
        cookies_browser,
        source_language,
        voice_profile,
    )
    if force:
        job = json.loads(prepared["manifest_path"].read_text(encoding="utf-8"))
        job["force"] = True
        write_manifest(prepared["manifest_path"], job)

    final_video, validation = finish_job(
        config,
        source_video=prepared["source_video"],
        result_dir=prepared["result_dir"],
        log_path=prepared["log_path"],
        manifest_path=prepared["manifest_path"],
        job_dir=prepared["job_dir"],
        video_id=prepared["video_id"],
        source_request=source,
        metadata=prepared["metadata"],
        source_language=source_language,
        voice_role=prepared["voice_role"],
        voice_profile=voice_profile,
        force=force,
        started_at=started_at,
    )
    summary = completed_payload(
        final_video=final_video,
        job_dir=prepared["job_dir"],
        manifest_path=prepared["manifest_path"],
        validation=validation,
    )
    print("[complete] 视频翻译完成。")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "下载常用语言视频，生成保留背景声、自动匹配男女音色的中文配音和硬字幕成片。"
            " Hermes 应使用 --tick 循环；长任务在分离的 worker 中运行。"
        )
    )
    parser.add_argument("source", nargs="?", help="视频 URL 或本地视频文件")
    parser.add_argument("--output-root", default=os.getenv("PYVIDEOTRANS_OUTPUT_ROOT", str(DEFAULT_OUTPUT_ROOT)))
    parser.add_argument("--project-dir", default=os.getenv("PYVIDEOTRANS_HOME"))
    parser.add_argument(
        "--source-language",
        choices=tuple(SUPPORTED_SOURCE_LANGUAGES),
        default=os.getenv("PYVIDEOTRANS_SOURCE_LANGUAGE", "auto"),
        help="源视频语言；默认 auto。支持：" + "、".join(
            f"{code}={name}" for code, name in SUPPORTED_SOURCE_LANGUAGES.items()
        ),
    )
    parser.add_argument("--max-height", type=int, default=1080)
    parser.add_argument(
        "--voice-profile",
        choices=tuple(VOICE_PROFILES),
        default=os.getenv("PYVIDEOTRANS_VOICE_PROFILE", "auto"),
        help="中文男声音色；默认 auto。支持：" + "、".join(
            f"{code}={name}" for code, name in VOICE_PROFILES.items()
        ),
    )
    parser.add_argument("--cookies-from-browser", help="仅在用户明确授权时使用，例如 chrome 或 firefox")
    parser.add_argument("--force", action="store_true", help="停止旧 worker，清理项目任务缓存并重新执行翻译")
    parser.add_argument("--preflight-only", action="store_true", help="只检查依赖、模型和本地 LLM")
    parser.add_argument("--validate-result", type=str, help="只校验一个已有成片，不运行翻译")
    parser.add_argument(
        "--tick",
        action="store_true",
        help="Hermes 安全模式：启动/复用分离 worker，阻塞最多 --budget-seconds，返回可续跑 JSON",
    )
    parser.add_argument(
        "--worker",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--job-dir",
        help="已有任务目录（含 job.json）；--tick / --worker 续跑时使用",
    )
    parser.add_argument(
        "--budget-seconds",
        type=float,
        default=float(os.getenv("PYVIDEOTRANS_TICK_BUDGET", DEFAULT_TICK_BUDGET_SECONDS)),
        help=f"单次 --tick 最多等待秒数（默认 {DEFAULT_TICK_BUDGET_SECONDS}，需低于 Hermes 前台硬顶）",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="在当前进程同步跑完全流程（本机调试用；Hermes 请用 --tick）",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.source_language not in SUPPORTED_SOURCE_LANGUAGES:
            raise WorkflowError(
                f"不支持源语言代码 {args.source_language!r}。当前只支持："
                + ", ".join(SUPPORTED_SOURCE_LANGUAGES)
            )
        if args.voice_profile not in VOICE_PROFILES:
            raise WorkflowError(
                f"不支持音色配置 {args.voice_profile!r}。当前只支持："
                + ", ".join(VOICE_PROFILES)
            )

        if args.worker:
            if not args.job_dir:
                raise WorkflowError("--worker 需要 --job-dir")
            return run_worker(expand(args.job_dir), args.project_dir)

        config = preflight(args.project_dir)
        print_preflight(config)
        if args.preflight_only:
            return 0

        if args.validate_result:
            final_video = expand(args.validate_result)
            validation = validate_result(final_video, config)
            print(json.dumps({"final_video": str(final_video), "validation": validation}, ensure_ascii=False, indent=2))
            return 0

        # Default (and Hermes skill path): checkpointed --tick. Use --full only for
        # local debugging that can block far beyond Hermes' ~600s hard cap.
        if args.full and not args.tick:
            if not args.source:
                raise WorkflowError("缺少视频 URL 或本地视频路径。")
            return run_full_sync(
                config,
                source=args.source,
                output_root=args.output_root,
                max_height=args.max_height,
                cookies_browser=args.cookies_from_browser,
                source_language=args.source_language,
                voice_profile=args.voice_profile,
                force=args.force,
            )

        return run_tick(
            config,
            source=args.source,
            job_dir_arg=args.job_dir,
            output_root=args.output_root,
            max_height=args.max_height,
            cookies_browser=args.cookies_from_browser,
            source_language=args.source_language,
            voice_profile=args.voice_profile,
            force=args.force,
            budget_seconds=args.budget_seconds,
        )
    except WorkflowError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("[error] 用户中止工作流；已保留下载和任务目录，可稍后重试。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
