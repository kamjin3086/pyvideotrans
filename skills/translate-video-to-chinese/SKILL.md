---
name: translate-video-to-chinese
description: Download a common-language YouTube or other yt-dlp-compatible video, translate its speech and subtitles into Simplified Chinese with pyVideoTrans, replace only the original vocal track with automatically matched Chinese voices while preserving background audio through Demucs, burn Chinese subtitles into the video, validate the result, and return the final path. Supports automatic detection plus English, Japanese, Korean, French, German, Spanish, Italian, Portuguese, and Russian. Use when the user asks to translate, dub, localize, or download an online video as a Chinese version, especially prompts such as “帮我使用 skill 翻译这个视频：[URL]”, “把这个日语视频做成中文配音版”, or “将这个法语视频翻译成中文”.
---

# Translate Video to Chinese

Orchestrate the repository workflow as **discrete stages**. Do not wrap the whole pipeline in one opaque command. Keep translation serial, use Qwen TTS CLI, preserve Demucs `no_vocals`, replace vocals only, and hard-burn Simplified Chinese subtitles.

## Why stages (not one mega-script)

Hermes foreground terminals are hard-capped near **600s**. A single worker also leaves the user staring at a spinner with no idea whether download, Demucs, STT, translate, or dub is running. The agent therefore:

1. Runs **one `--stage` at a time**.
2. On `status=completed`, briefly tells the user the `user_hint` (one short line), then starts the next stage.
3. On `status=in_progress`, **immediately** re-runs the **same** stage (no chat, no search) — long stages tick under the 600s cap.
4. Never raises Hermes `TERMINAL_MAX_FOREGROUND_TIMEOUT` and never uses `--full` from Hermes.

## Stage order

| Stage | Purpose | May return `in_progress`? |
|---|---|---|
| `preflight` | deps / models / LLM | no |
| `prepare` | metadata + download → `job.json` | no |
| `separate` | Demucs + demux | **yes** (GPU quiet — no mid-stage chat) |
| `recognize` | STT | yes |
| `translate` | Chinese subtitles | yes |
| `dub` | TTS + align + mux | yes |
| `validate` | AV / background checks | no |

`dub` includes assemble in one process (TTS queue state). Report it as “配音与合成”.

## Required agent loop

1. Extract one URL or local path; ask if missing.
2. Map an explicit language to `en|ja|ko|fr|de|es|it|pt|ru`, else `auto`.
3. Optional opening line once: 「开始分阶段转译；人声分离和翻译/配音可能较久，阶段之间会简短汇报。」
4. Run stages in order. Prefer `job_directory` / `next_command` from JSON.

```bash
python3 "${HERMES_SKILL_DIR}/scripts/translate_video.py" --stage preflight
python3 "${HERMES_SKILL_DIR}/scripts/translate_video.py" --stage prepare "<URL>" --source-language <code-or-auto>
python3 "${HERMES_SKILL_DIR}/scripts/translate_video.py" --stage separate --job-dir "<job_directory>"
# … recognize → translate → dub → validate
```

5. After each terminal call, read `[stage]` JSON:
   - `completed` → quote `user_hint` to the user (one line), then run `next_command` / `next_stage`.
   - `in_progress` → run `tick_command` / same `--stage --job-dir` immediately; **do not** chat.
   - `failed` → read `message` / `log_tail`. For transient stage failures, re-run the **same** `--stage --job-dir` once (soft resume). Use `--force` only if the user asked for a clean wipe. If it fails again, stop and report.
6. On final `validate` completed, report `final_video`, `job_directory`, and manifest.

### Hermes tool shape

```text
terminal(command='python3 "${HERMES_SKILL_DIR}/scripts/translate_video.py" --stage <name> ...', background=false)
```

- Omit `timeout`, or pass **≤ 600** (e.g. 560). Never pass 3600/6000.
- Do **not** use `background=true` or `process(poll|wait|log)`.
- Do **not** call `run_cli_local.sh` directly or invent alternate scripts.
- Do **not** pass `--force` unless the user asked for a clean rerun.
- Between `in_progress` ticks of `separate`, stay silent so Demucs keeps the GPU.

## Reporting rules (stability first)

- **Do** report at stage boundaries using `user_hint`.
- **Do not** narrate every tick or every subtitle batch.
- **Do not** send progress chats during `separate` / other `in_progress` waits.
- One short opening summary is enough before `prepare`.

## Dependencies

Preflight is read-only: no auto-install, no large downloads, no restarting the shared TTS server on `18081`.

```bash
python3 "${HERMES_SKILL_DIR}/scripts/translate_video.py" --stage preflight
# equivalent:
python3 "${HERMES_SKILL_DIR}/scripts/translate_video.py" --preflight-only
```

If preflight fails, quote missing paths/sizes and ask permission before installing.

## Common cases

- One URL at a time; playlist URLs → that single video.
- Supported languages only: `auto,en,ja,ko,fr,de,es,it,pt,ru`.
- Cookies only after explicit user browser choice: `--cookies-from-browser chrome`.
- Resume is default (`--no-clear-cache`). Existing stage artifacts are skipped.
- Success requires `validate` JSON `status=completed` and `final_video`, not merely exit code 0.
- Local debugging may use `--full`. Hermes must not.

## Deliverables

Job dir `~/Videos/translated-videos/<video-id>/` (override with `--output-root`):

- `source/`, `result/` (`vocal.wav`, `instrument.wav`, `zh-cn.srt`, final MP4, voice plans)
- `job.json` (includes `pipeline.stages` checkpoints)
- `workflow.log`, `worker.log`, `runtime.json`, `worker.pid`
- `workcache/`: stable per-job pyVideoTrans cache (shared across stage workers; do not delete mid-run)

Treat `final_video` from a completed `validate` stage as authoritative.
