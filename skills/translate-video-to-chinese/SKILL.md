---
name: translate-video-to-chinese
description: Download a common-language YouTube or other yt-dlp-compatible video, translate its speech and subtitles into Simplified Chinese with pyVideoTrans, replace only the original vocal track with automatically matched Chinese voices while preserving background audio through Demucs, burn Chinese subtitles into the video, validate the result, and return the final path. Supports automatic detection plus English, Japanese, Korean, French, German, Spanish, Italian, Portuguese, and Russian. Use when the user asks to translate, dub, localize, or download an online video as a Chinese version, especially prompts such as “帮我使用 skill 翻译这个视频：[URL]”, “把这个日语视频做成中文配音版”, or “将这个法语视频翻译成中文”.
---

# Translate Video to Chinese

Use the repository's tested local workflow. Keep translation calls serial, use Qwen TTS through its CLI, preserve the Demucs `no_vocals` background track, replace only vocals, and hard-burn Simplified Chinese subtitles. After STT, the default video-level router makes one serial local-Qwen call over metadata and transcript samples, then keeps one coherent male/female palette for the whole video. It maps ten dominant content styles to `dylan` or `serious-male-05` plus one of ten repository female clone voices. After Demucs separation, the acoustic router reuses `qwen-codec` and two tiny speaker prototypes only to choose male versus female per subtitle cue; ambiguous cues keep the selected male default.

## Run the workflow (tick loop — do not change Hermes timeouts)

Hermes foreground terminals are hard-capped around **600 seconds**. Long videos cannot finish inside one tool call, and raising Hermes config is the wrong fix. This skill therefore uses a **detached worker + short tick** design owned by the project:

1. Each `--tick` may start or reuse a project-owned worker that outlives the Hermes tool call.
2. The tick process only waits up to `--budget-seconds` (default **480**), then returns a JSON checkpoint.
3. Hermes immediately calls `--tick` again with the same `--job-dir` until `status` is `completed` or `failed`.
4. Default resume uses `--no-clear-cache`. Never pass `--force` unless the user asked for a clean rerun.

### Steps

1. Extract exactly one video URL or local video path from the user's request.
2. Ask for a URL or path only if none was provided.
3. Infer an explicitly stated source language and map it to `en`, `ja`, `ko`, `fr`, `de`, `es`, `it`, `pt`, or `ru`. Otherwise use `auto`.
4. Run the first tick through Hermes' absolute skill-directory template variable:

   ```bash
   python3 "${HERMES_SKILL_DIR}/scripts/translate_video.py" --tick "<URL-or-local-video>" --source-language <code-or-auto>
   ```

5. Read the printed `[tick]` JSON:
   - `status=completed` → stop; report `final_video`, `job_directory`, and `manifest`.
   - `status=failed` → stop; report `message` / `log_tail`.
   - `status=in_progress` → **immediately** run the `tick_command` from the JSON (or the equivalent `--tick --job-dir …`). Do not chat, search, browse, or start other tools between ticks.
6. Repeat step 5 until completed or failed. Process one video at a time.

### Required Hermes tool shape

```text
terminal(
  command='python3 "${HERMES_SKILL_DIR}/scripts/translate_video.py" --tick "<URL>" --source-language <code-or-auto>',
  background=false
)
```

Later ticks:

```text
terminal(
  command='python3 "${HERMES_SKILL_DIR}/scripts/translate_video.py" --tick --job-dir "<job_directory_from_json>"',
  background=false
)
```

Rules:

- Prefer **omitting** `timeout`, or pass a value **≤ 600** (for example `560`). Never pass `timeout=3600` / `6000` — Hermes will reject or force background mode.
- Do **not** set `background=true`.
- Do **not** use `process(action='poll'|'wait'|'log')` around this job.
- Do **not** ask the user to raise `TERMINAL_MAX_FOREGROUND_TIMEOUT` or edit Hermes config for this skill.
- Do **not** invent alternate Python one-liners or call `run_cli_local.sh` directly; always use this script's `--tick` loop.
- Between `in_progress` ticks, re-enter the next terminal call immediately so Demucs (early phase) stays free of concurrent local-LLM chat traffic.

The heavy pipeline runs in a detached worker (`worker.log`, `runtime.json`, `worker.pid` under the job directory). Killing or timing out a tick only ends the waiter; the worker keeps going and the next tick reconnects.

`--force` stops any old worker and passes CLI `--clear_cache` (wipes that job's generated outputs). Use only when the user explicitly requests a clean rerun.

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
  python3 "${HERMES_SKILL_DIR}/scripts/translate_video.py" --tick "<URL>" --cookies-from-browser chrome
  ```

- Reuse an already completed and valid result. Pass `--force` only when the user explicitly requests a clean rerun.
- Preserve the original downloaded source under the job directory so a failed or timed-out tick can resume via `--tick --job-dir …` without wiping progress.
- Do not claim success merely because a tick exited with code zero. Success requires `[tick]` JSON `status=completed` plus a validated `final_video`.
- Local debugging outside Hermes may use `--full` for a single long blocking run. Hermes must not use `--full`.

## Expected deliverables

Each job directory contains:

- `source/`: downloaded or referenced source video
- `result/`: final MP4 plus workflow artifacts such as `instrument.wav`, `vocal.wav`, and `zh-cn.srt`
- `workflow.log`: download and translation logs from the waiter / prep phase
- `worker.log`: detached worker stdout/stderr
- `runtime.json`: worker checkpoint (`running` / `completed` / `failed`)
- `worker.pid`: detached worker pid while alive
- `job.json`: source, settings, output paths, timestamps, and validation results
- `result/voice-style-plan.json`: video-wide style, confidence, reason, and selected male/female palette
- `result/voice-routing.json`: per-subtitle acoustic label, confidence margin, selected voice, and routing summary

Treat the `final_video` value in a `status=completed` tick JSON as authoritative.
