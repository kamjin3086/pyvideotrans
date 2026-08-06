# -*- coding: utf-8 -*-
"""Translate-stage repair: reconcile ``zh-cn.srt`` with ``auto.srt``.

The local translation LLM occasionally violates the 1:1 SRT contract:

* it drops an entire block from a batch, or
* it returns a block whose text is empty.

The pipeline's timestamp alignment then leaves that cue untranslated, the
dubbing stage skips empty cues, and the final ``zh-cn.srt`` has fewer cues than
``auto.srt``.  Validate then fails with "路由行数与中文字幕条数不一致" because
the acoustic voice routing is keyed to the source cues.

This module fills the missing translations through the same local LLM and
rewrites ``zh-cn.srt`` with a strict 1:1 cue layout (source order + source
timestamps + 1..N numbering), so no content is silently lost.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_LLM_API = "http://127.0.0.1:8101/v1"
DEFAULT_LLM_MODEL = "Qwen3.6-35B-A3B-instruct"
MAX_ATTEMPTS = 3
CHUNK_SIZE = 8

_PROMPT = """# ROLE
You are an expert SRT subtitle translator. Translate ONLY the text of each SRT block into Simplified Chinese.

# RULES
- Output block count MUST exactly equal input block count.
- Do NOT change index numbers or timestamps.
- Keep the translation concise for TTS dubbing; never merge or shift blocks.
- If the source block is an incomplete fragment, keep the translation an incomplete fragment too.
- Output ONLY the valid SRT content inside <TRANSLATE_TEXT> tags, no markdown fences.

<INPUT>
{batch_input}
</INPUT>"""


class RepairError(RuntimeError):
    """Raised when a subtitle cannot be repaired after retries."""


def normalize_time(value: str) -> str:
    return re.sub(r"\.", ",", value.strip())


def parse_srt(text: str) -> list[dict[str, Any]]:
    """Parse SRT text into [{index,time,text}] preserving order.

    Tolerant of missing index numbers and empty texts; blocks without a
    timestamp line are skipped.
    """
    time_re = re.compile(
        r"\d{1,2}:\d{2}:\d{2}[,.]\d{1,3}\s*-->\s*\d{1,2}:\d{2}:\d{2}[,.]\d{1,3}"
    )
    cues: list[dict[str, Any]] = []
    for block in re.split(r"\n\s*\n", text):
        block = block.strip()
        if not block:
            continue
        lines = block.splitlines()
        time_line = None
        for line in lines[:2]:
            if time_re.search(line):
                time_line = line
                break
        if time_line is None:
            continue
        time_idx = lines.index(time_line)
        index = len(cues) + 1
        if time_idx > 0:
            m = re.match(r"^\s*(\d+)", lines[0])
            if m:
                index = int(m.group(1))
        time_str = normalize_time(time_re.search(time_line).group(0))
        text = " ".join(lines[time_idx + 1 :]).strip()
        cues.append({"index": index, "time": time_str, "text": text})
    return cues


def _srt_batch(cues: list[dict[str, Any]]) -> str:
    return "\n\n".join(f"{i + 1}\n{c['time']}\n{c['text']}" for i, c in enumerate(cues))


def llm_translate(api_base: str, model: str, batch_input: str, *, timeout: float = 300.0) -> str:
    url = api_base.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "temperature": 0.1,
        "max_tokens": 4096,
        "messages": [
            {"role": "system", "content": "You are a top-tier Subtitle Translation Engine."},
            {"role": "user", "content": _PROMPT.replace("{batch_input}", batch_input)},
        ],
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise RepairError(f"补译 LLM 请求失败：{api_base}: {exc}") from exc
    try:
        choices = body["choices"]
        content = choices[0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RepairError(f"补译 LLM 响应格式异常：{api_base}: {body}") from exc
    if choices[0].get("finish_reason") == "length":
        raise RepairError("补译 LLM 响应被 max_tokens 截断，请检查本地 LLM 配置。")
    match = re.search(r"<TRANSLATE_TEXT>(.*?)</TRANSLATE_TEXT>", content, re.S | re.I)
    return (match.group(1) if match else content).strip()


def _fill_from_response(
    pending: list[dict[str, Any]],
    response: str,
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    """Map response cues onto pending cues; returns (by_time_text, still_missing)."""
    parsed = parse_srt(response)
    by_time: dict[str, str] = {}
    for cue in parsed:
        if cue["text"].strip():
            by_time.setdefault(cue["time"], cue["text"].strip())
    filled: dict[str, str] = {}
    still: list[dict[str, Any]] = []
    for cue in pending:
        text = by_time.get(cue["time"], "")
        if text:
            filled[cue["time"]] = text
        else:
            still.append(cue)
    # Positional fallback for responses whose timestamps drifted slightly.
    if still and parsed:
        idx = 0
        for cue in pending:
            if cue["time"] in filled:
                continue
            while idx < len(parsed) and not parsed[idx]["text"].strip():
                idx += 1
            if idx >= len(parsed):
                break
            filled[cue["time"]] = parsed[idx]["text"].strip()
            idx += 1
    still = [cue for cue in pending if cue["time"] not in filled]
    return filled, still


def repair_translated_subtitles(
    result_dir: Path | str,
    llm_api: str | None = None,
    llm_model: str | None = None,
) -> dict[str, Any]:
    """Repair missing/empty target cues; rewrite zh-cn.srt 1:1 with auto.srt."""
    result_dir = Path(result_dir)
    source_path = result_dir / "auto.srt"
    target_path = result_dir / "zh-cn.srt"
    if not source_path.is_file() or not target_path.is_file():
        return {"repaired": 0, "skipped": "缺少 auto.srt 或 zh-cn.srt"}

    source = parse_srt(source_path.read_text(encoding="utf-8", errors="ignore"))
    target = parse_srt(target_path.read_text(encoding="utf-8", errors="ignore"))
    if not source:
        return {"repaired": 0, "skipped": "auto.srt 为空或无法解析"}

    target_by_time: dict[str, str] = {}
    for cue in target:
        if cue["text"].strip():
            target_by_time.setdefault(cue["time"], cue["text"].strip())

    missing = [cue for cue in source if not target_by_time.get(cue["time"], "")]
    if missing and len(missing) > max(20, len(source) // 5):
        raise RepairError(
            f"zh-cn.srt 时间轴与 auto.srt 失配过多（{len(missing)}/{len(source)} 条），"
            "疑似已被配音阶段重排时间轴。请重新执行 translate 阶段"
            "（--force 会先清除旧 zh-cn.srt）后再继续。"
        )
    if not missing:
        return {
            "repaired": 0,
            "checked": len(source),
            "target_cues": len(target),
            "message": "zh-cn.srt 与 auto.srt 条数一致，无需补译",
        }

    api = llm_api or os.environ.get("PYVIDEOTRANS_LLM_API", DEFAULT_LLM_API)
    model = llm_model or os.environ.get("PYVIDEOTRANS_LLM_MODEL", DEFAULT_LLM_MODEL)
    print(
        f"[repair] 检测到 {len(missing)} 条缺失/空字幕（auto.srt {len(source)} 条 vs "
        f"zh-cn.srt {len(target)} 条），行号：{[c['index'] for c in missing]}"
    )

    filled: dict[str, str] = {}
    pending = list(missing)
    for attempt in range(1, MAX_ATTEMPTS + 1):
        if not pending:
            break
        print(f"[repair] 补译第 {attempt} 轮，剩余 {len(pending)} 条")
        for start in range(0, len(pending), CHUNK_SIZE):
            chunk = pending[start : start + CHUNK_SIZE]
            batch_input = _srt_batch(chunk)
            response = llm_translate(api, model, batch_input)
            filled_now, _ = _fill_from_response(chunk, response)
            filled.update(filled_now)
        pending = [cue for cue in pending if cue["time"] not in filled]

    if pending:
        lines = ", ".join(str(c["index"]) for c in pending)
        raise RepairError(
            f"补译失败：仍有 {len(pending)} 条字幕无法翻译（行号 {lines}）。"
            f"请检查本地 LLM（{api}，模型 {model}）后重试。"
        )

    out_lines: list[str] = []
    for i, cue in enumerate(source, start=1):
        text = target_by_time.get(cue["time"], "") or filled.get(cue["time"], "")
        out_lines.append(f"{i}\n{cue['time']}\n{text}")
    target_path.write_text("\n\n".join(out_lines) + "\n", encoding="utf-8")

    for cue in missing:
        print(
            f"[repair] 已补译：原行 {cue['index']} {cue['time']} → {filled[cue['time']]}"
        )
    return {
        "repaired": len(missing),
        "checked": len(source),
        "target_cues_before": len(target),
        "target_cues_after": len(source),
    }
