---
name: translate-video-to-chinese
description: Download a common-language YouTube or other yt-dlp-compatible video, translate its speech and subtitles into Simplified Chinese with pyVideoTrans, replace only the original vocal track with automatically matched Chinese voices while preserving background audio through Demucs, burn Chinese subtitles into the video, validate the result, and return the final path. Supports automatic detection plus English, Japanese, Korean, French, German, Spanish, Italian, Portuguese, and Russian. Use when the user asks to translate, dub, localize, or download an online video as a Chinese version, especially prompts such as “帮我使用 skill 翻译这个视频：[URL]”, “把这个日语视频做成中文配音版”, or “将这个法语视频翻译成中文”.
---

# Translate Video to Chinese

Use the **`vt.py` façade** only. It hides `--stage` / `--job-dir` / `--budget-seconds` / cache flags. Run **one terminal call at a time**, read `[stage]` JSON, then either tell the user one short `user_hint` or immediately run the printed `next_command` / `continue`.

**New machine / first bring-up:** see **[ENVIRONMENT.md](ENVIRONMENT.md)** (checklist, reference stack, env vars). Every run starts with read-only `vt.py preflight`; it reports gaps but does **not** auto-install. Get permission before large downloads.

Do not hand-assemble `translate_video.py --stage …` unless debugging. Do not call `run_cli_local.sh` directly. Do not raise Hermes timeouts.

## Canonical commands

```bash
VT="${HERMES_SKILL_DIR}/scripts/vt.py"

python3 "$VT" preflight
python3 "$VT" prepare "<URL-or-local-path>" --lang auto
python3 "$VT" continue "<job_directory>"
```

Optional: `prepare … --lang en|ja|ko|fr|de|es|it|pt|ru` when the user names a language. Cookies only after explicit consent: `prepare URL --cookies chrome`.

## Agent loop (copy this)

1. Extract one URL/path; ask if missing.
2. Optional once: 「开始分阶段转译；人声分离和翻译/配音可能较久，阶段之间会简短汇报。」
3. `vt.py preflight` → on completed, quote `user_hint`, substitute the URL into `next_command` / run `prepare`.
4. After `prepare` completes, keep the `job_directory` from JSON.
5. Loop **`vt.py continue "<job_directory>"`** until JSON has `stage=validate` and `status=completed` (or a second consecutive `failed` for the same stage).
6. After each call:
   - `completed` + `next_command` → one-line `user_hint` to user, then run that command.
   - `in_progress` → **immediately** run `next_command` / `continue` again; **no chat**.
   - `failed` → run `continue` **once** more; if still failed, stop and report `message` / `log_tail`.
7. Final success: report only the **final video path** and `job_directory` — one short line.
   Do not report subtitle counts, routing counts, or dub statistics unless the user explicitly
   asks; the validate JSON already guarantees the video is fine. If the user does ask for
   dubbing details, quote `primary_dub_voice` / `voice_routing_counts` verbatim from JSON and
   never invent numbers.
8. Do not claim success on exit code alone.

Hermes `terminal`: `background=false`; omit `timeout` or use ≤560. Never `timeout=3600/6000`, never `background=true`.

## Do / Don't

### Do

```text
# ✔ after prepare JSON gave job_directory
terminal(command='python3 "${HERMES_SKILL_DIR}/scripts/vt.py" continue "/home/…/translated-videos/VIDEO_ID"', background=false)

# ✔ in_progress → same continue, no other tools
terminal(command='python3 "${HERMES_SKILL_DIR}/scripts/vt.py" continue "/home/…/translated-videos/VIDEO_ID"', background=false)

# ✔ named language
terminal(command='python3 "${HERMES_SKILL_DIR}/scripts/vt.py" prepare "https://youtu.be/xxxx" --lang ja', background=false)
```

### Don't

```text
# ✘ hand-splicing stage flags / inventing CLI
python3 …/translate_video.py --stage recognize --job-dir … --budget-seconds 480 --no-clear-cache

# ✘ one-shot mega run from Hermes
python3 …/translate_video.py --full URL
python3 …/run_cli_local.sh --task vtv …

# ✘ mid-stage chatter / search while Demucs or continue is running
# ✘ timeout=6000 / background=true / process poll loops
# ✘ --force unless the user asked for a clean rerun
# ✘ report subtitle/routing counts unless asked; never invent numbers from memory
```

## Stages (for awareness only)

`preflight → prepare → separate → recognize → translate → dub → validate`

`continue` chooses tick-vs-advance. Long stages may return `in_progress`. `separate` is GPU-sensitive: stay silent between continues. `dub` includes mux; tell the user “配音与合成”.

## Dependencies

`preflight` is read-only. If it fails, open [ENVIRONMENT.md](ENVIRONMENT.md), quote missing paths/sizes from the JSON/log, and ask permission before installing. Keep Python deps in the project `.venv`. Do not stop the shared TTS server on `18081`.

## Deliverables

`~/Videos/translated-videos/<video-id>/` (or `--output-root`):

- `source/`, `result/` (stems, `zh-cn.srt`, final MP4, voice plans)
- `job.json` (`pipeline.stages`), `workcache/` (stable cache — do not delete mid-run)
- `workflow.log`, `worker.log`, `runtime.json`

Authoritative success: `validate` JSON `status=completed` with `final_video`.
