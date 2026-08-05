---
name: translate-video-to-chinese
description: Download a common-language YouTube or other yt-dlp-compatible video, translate its speech and subtitles into Simplified Chinese with pyVideoTrans, replace only the original vocal track with automatically matched Chinese voices while preserving background audio through Demucs, burn Chinese subtitles into the video, validate the result, and return the final path. Supports automatic detection plus English, Japanese, Korean, French, German, Spanish, Italian, Portuguese, and Russian. Use when the user asks to translate, dub, localize, or download an online video as a Chinese version, especially prompts such as “帮我使用 skill 翻译这个视频：[URL]”, “把这个日语视频做成中文配音版”, or “将这个法语视频翻译成中文”.
---

# Translate Video to Chinese

Use the repository's tested local workflow. Keep translation calls serial, use Qwen TTS through its CLI, preserve the Demucs `no_vocals` background track, replace only vocals, and hard-burn Simplified Chinese subtitles. After STT, the default video-level router makes one serial local-Qwen call over metadata and transcript samples, then keeps one coherent male/female palette for the whole video. It maps ten dominant content styles to `dylan` or `serious-male-05` plus one of ten repository female clone voices. After Demucs separation, the acoustic router reuses `qwen-codec` and two tiny speaker prototypes only to choose male versus female per subtitle cue; ambiguous cues keep the selected male default.

## Run the workflow

1. Extract exactly one video URL or local video path from the user's request.
2. Ask for a URL or path only if none was provided.
3. Infer an explicitly stated source language and map it to `en`, `ja`, `ko`, `fr`, `de`, `es`, `it`, `pt`, or `ru`. Otherwise use `auto`.
4. Run the bundled [workflow script](scripts/translate_video.py) through Hermes' absolute skill-directory template variable **as one foreground blocking terminal call**:

   ```bash
   python3 "${HERMES_SKILL_DIR}/scripts/translate_video.py" "<URL-or-local-video>" --source-language <code-or-auto>
   ```

5. Wait until that single command exits. Do not start a second translation of the same video concurrently.
6. Report the absolute final video path, job directory, validation status, and manifest path printed by the script.

### Critical: keep Demucs and the local LLM off the GPU at the same time

Demucs uses the same AMD ROCm GPU as the local Qwen endpoint on port `8101`. If the agent backgrounds the workflow and keeps making chat/tool turns while Demucs runs, stem segments after the first ~8 seconds can be silently zeroed even though Demucs exits 0. Do **not** unload or restart the LLM. Avoid contention by **blocking the Hermes tool call** until the script exits.

#### Required Hermes tool shape (blocks; no mid-wait LLM)

Use a single foreground `terminal` call. While that tool is running, the agent loop waits for the tool result and does **not** issue another LLM completion.

```text
terminal(
  command='python3 "${HERMES_SKILL_DIR}/scripts/translate_video.py" "<URL-or-local-video>" --source-language <code-or-auto>',
  background=false
)
```

Rules:

- Prefer **omitting** `timeout` so Hermes uses the configured `terminal.timeout` (this host is already `6000` seconds ≈ 100 minutes). That is enough for typical ≤15 minute videos.
- If you must pass `timeout`, it must be ≤ `TERMINAL_MAX_FOREGROUND_TIMEOUT` (Hermes default hard cap is **600** unless the gateway env raises it). Passing e.g. `timeout=3600` with the default cap makes Hermes **reject** foreground mode and nudge you to `background=true` — do not do that for this skill.
- Do **not** set `background=true`.
- Do **not** use `process(action='poll'|'wait'|'log')` loops around this job.
- Do **not** call `web_search`, browser, or other tools, and do not send interim progress chats, until the foreground `terminal` returns.
- After it returns, read the printed JSON/`workflow.log` and only then reply to the user.

`background=true` + `notify_on_complete=true` can also avoid polling, but it is weaker for this GPU case (other user messages can still start LLM turns mid-job). Prefer foreground blocking.

The script itself validates Demucs stem energy and retries once with `--shifts 1` if collapse is detected. Still treat GPU-quiet blocking as mandatory.

If a longer video needs more than ~100 minutes, ask the user to raise gateway `TERMINAL_MAX_FOREGROUND_TIMEOUT` (and keep `terminal.timeout` ≥ that), then re-run with one foreground call — still do not background.

The default output directory is `~/Videos/translated-videos/<video-id>/`. Honor a user-requested destination with `--output-root /absolute/path`.

Keep `--voice-profile auto` unless the user explicitly requests a voice. Use `--voice-profile dylan` for an explicit light Beijing male voice, or `--voice-profile serious-male-05` for the extracted calm narrative male voice. Automatic style routing runs after transcription and is one short serial call to the configured local Qwen endpoint; it never runs concurrently with subtitle translation. The selected style and palette are recorded in `voice-style-plan.json`.

## Respect dependency boundaries

The script performs a read-only preflight before downloading or translating. It must not automatically install packages, download large models, modify another project, or restart the shared TTS service.

If preflight fails:

- Quote the missing dependency or model paths reported by the script.
- Tell the user the reported approximate download size when one is available.
- Ask permission before installing or downloading anything large.
- Keep any Python dependencies inside the pyVideoTrans `.venv`.
- Use the local Qwen TTS executable directly; do not use or stop the TTS server on port `18081`.
- The serious clone and ten female clones use the already-installed Qwen Base 1.7B model plus small repository SPK/RVQ/text files. Do not download a replacement model automatically.
- Automatic male/female voice presentation routing must not install pyannote, PyTorch, or a separate gender model. It uses the existing Qwen Base speaker encoder through `qwen-codec`, repository prototypes, and a conservative pitch fallback. Treat ambiguous, short, overlapping, or noisy cues as the default voice. This classifies acoustic presentation, not gender identity.

Run preflight alone when diagnosing installation:

```bash
python3 "${HERMES_SKILL_DIR}/scripts/translate_video.py" --preflight-only
```

## Handle common cases

- Process one URL at a time. YouTube playlist URLs default to the single selected video.
- Prefer an explicit source language when the user names one. Use automatic detection when the user does not know or mention it.
- Support only `auto`, `en`, `ja`, `ko`, `fr`, `de`, `es`, `it`, `pt`, and `ru`. If the user requests another language, explain that the streamlined local workflow does not support it rather than installing another model or attempting an untested workaround.
- Warn that automatic detection is intended for videos with one dominant spoken language; ask for an explicit supported language if detection produces poor transcription.
- For a login-gated video, retry only after the user explicitly chooses a browser cookie source:

  ```bash
  python3 "${HERMES_SKILL_DIR}/scripts/translate_video.py" "<URL>" --cookies-from-browser chrome
  ```

- Reuse an already completed and valid result. Pass `--force` only when the user explicitly requests a clean rerun.
- Preserve the original downloaded source under the job directory so a failed translation can be resumed.
- Do not claim success merely because the translator exited with code zero. The script must validate the final audio/video streams, duration, workflow artifacts, and background-audio preservation samples.

## Expected deliverables

Each job directory contains:

- `source/`: downloaded or referenced source video
- `result/`: final MP4 plus workflow artifacts such as `instrument.wav`, `vocal.wav`, and `zh-cn.srt`
- `workflow.log`: download and translation logs
- `job.json`: source, settings, output paths, timestamps, and validation results
- `result/voice-style-plan.json`: video-wide style, confidence, reason, and selected male/female palette
- `result/voice-routing.json`: per-subtitle acoustic label, confidence margin, selected voice, and routing summary

Treat the `final_video` value in the script's final JSON output as authoritative.
