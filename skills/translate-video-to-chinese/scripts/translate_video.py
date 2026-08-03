#!/usr/bin/env python3
"""Download, translate, dub, subtitle, and validate one English video."""

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


DEFAULT_OUTPUT_ROOT = Path.home() / "Videos" / "translated-videos"
DEFAULT_LLM_API = "http://127.0.0.1:8101/v1"
MEDIA_SUFFIXES = {".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi"}
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


def resolve_qwen_assets(project: Path) -> tuple[Path | None, Path | None, Path | None]:
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
    return binary, model, codec


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
    qwen_bin, qwen_model, qwen_codec = resolve_qwen_assets(project)
    whisper_model = resolve_faster_whisper_model(project)

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
    if not qwen_model:
        missing.append("Qwen CustomVoice 1.7B GGUF 模型（Q8 约 2 GB）")
    if not qwen_codec:
        missing.append("Qwen 12Hz tokenizer/codec GGUF 模型（Q8 约 278 MB）")
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
        "qwen_model": qwen_model,
        "qwen_codec": qwen_codec,
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
        "qwen_model",
        "qwen_codec",
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
    if not background_samples:
        result["background_validation_note"] = "未找到同时满足长度和响度阈值的纯背景抽样区间；关键背景轨文件已确认存在。"
    return result


def translation_environment(config: dict[str, Path | str]) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "PYVIDEOTRANS_HOME": str(config["project"]),
            "PYVIDEOTRANS_DEMUCS_BIN": str(config["demucs"]),
            "PYVIDEOTRANS_QWENTTS_BIN": str(config["qwen_bin"]),
            "PYVIDEOTRANS_QWENTTS_MODEL": str(config["qwen_model"]),
            "PYVIDEOTRANS_QWENTTS_CODEC": str(config["qwen_codec"]),
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
    force: bool,
) -> Path:
    expected = result_dir / f"{source_video.stem}.mp4"
    if expected.is_file() and not force:
        print(f"[translate] 复用已存在成片并重新校验：{expected}")
        return expected.resolve()

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
        "en",
        "--translate_type",
        "9",
        "--source_language_code",
        "en",
        "--target_language_code",
        "zh-cn",
        "--tts_type",
        "20",
        "--voice_role",
        "dylan",
        "--subtitle_type",
        "1",
        "--voice_autorate",
        "--align_sub_audio",
        "--is_separate",
        "--clear_cache",
    ]
    run_logged(command, log_path, Path(config["project"]), translation_environment(config))
    if expected.is_file():
        return expected.resolve()
    candidates = list(result_dir.glob("*.mp4"))
    if not candidates:
        raise WorkflowError(f"翻译流程结束，但 {result_dir} 内没有生成 MP4 成片。")
    return max(candidates, key=lambda item: item.stat().st_size).resolve()


def write_manifest(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="下载英文视频，生成保留背景声的中文男声配音和硬字幕成片。"
    )
    parser.add_argument("source", nargs="?", help="视频 URL 或本地视频文件")
    parser.add_argument("--output-root", default=os.getenv("PYVIDEOTRANS_OUTPUT_ROOT", str(DEFAULT_OUTPUT_ROOT)))
    parser.add_argument("--project-dir", default=os.getenv("PYVIDEOTRANS_HOME"))
    parser.add_argument("--max-height", type=int, default=1080)
    parser.add_argument("--cookies-from-browser", help="仅在用户明确授权时使用，例如 chrome 或 firefox")
    parser.add_argument("--force", action="store_true", help="清理项目任务缓存并重新执行翻译")
    parser.add_argument("--preflight-only", action="store_true", help="只检查依赖、模型和本地 LLM")
    parser.add_argument("--validate-result", type=str, help="只校验一个已有成片，不运行翻译")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        config = preflight(args.project_dir)
        print_preflight(config)
        if args.preflight_only:
            return 0

        if args.validate_result:
            final_video = expand(args.validate_result)
            validation = validate_result(final_video, config)
            print(json.dumps({"final_video": str(final_video), "validation": validation}, ensure_ascii=False, indent=2))
            return 0

        if not args.source:
            raise WorkflowError("缺少视频 URL 或本地视频路径。")

        started_at = now_iso()
        local_source = expand(args.source) if not re.match(r"^https?://", args.source, flags=re.I) else None
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
            metadata = load_remote_metadata(Path(config["yt_dlp"]), args.source, args.cookies_from_browser)

        video_id = safe_identifier(str(metadata.get("id") or hashlib.sha256(args.source.encode()).hexdigest()[:12]))
        job_dir = expand(args.output_root) / video_id
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
                args.source,
                source_dir,
                log_path,
                args.max_height,
                args.cookies_from_browser,
            )

        final_video = run_translation(config, source_video, result_dir, log_path, args.force)
        print(f"[validate] 正在校验成片和背景声：{final_video}")
        validation = validate_result(final_video, config)

        manifest: dict[str, Any] = {
            "status": "completed",
            "source_request": args.source,
            "source_video": str(source_video),
            "video_id": video_id,
            "title": metadata.get("title"),
            "final_video": str(final_video),
            "job_directory": str(job_dir),
            "settings": {
                "source_language": "en",
                "target_language": "zh-cn",
                "voice": "dylan",
                "llm_api": config["llm_api"],
                "separation": "demucs two-stems vocals",
                "subtitles": "hard-burned",
                "max_download_height": args.max_height,
            },
            "validation": validation,
            "started_at": started_at,
            "completed_at": now_iso(),
            "log": str(log_path),
        }
        write_manifest(manifest_path, manifest)
        summary = {
            "status": "completed",
            "final_video": str(final_video),
            "job_directory": str(job_dir),
            "manifest": str(manifest_path),
            "validation_ok": True,
        }
        print("[complete] 视频翻译完成。")
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    except WorkflowError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("[error] 用户中止工作流；已保留下载和任务目录，可稍后重试。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
