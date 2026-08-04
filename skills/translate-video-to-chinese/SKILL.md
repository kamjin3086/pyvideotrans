---
name: translate-video-to-chinese
description: Download a common-language YouTube or other yt-dlp-compatible video, translate its speech and subtitles into Simplified Chinese with pyVideoTrans, replace only the original vocal track with a Chinese male voice while preserving background audio through Demucs, burn Chinese subtitles into the video, validate the result, and return the final path. Supports automatic detection plus English, Japanese, Korean, French, German, Spanish, Italian, Portuguese, and Russian. Use when the user asks to translate, dub, localize, or download an online video as a Chinese version, especially prompts such as “帮我使用 skill 翻译这个视频：[URL]”, “把这个日语视频做成中文配音版”, or “将这个法语视频翻译成中文”.
---

# Translate Video to Chinese

Use the repository's tested local workflow. Keep translation calls serial, use Qwen TTS through its CLI, preserve the Demucs `no_vocals` background track, replace only vocals, and hard-burn Simplified Chinese subtitles.

## Run the workflow

1. Extract exactly one video URL or local video path from the user's request.
2. Ask for a URL or path only if none was provided.
3. Infer an explicitly stated source language and map it to `en`, `ja`, `ko`, `fr`, `de`, `es`, `it`, `pt`, or `ru`. Otherwise use `auto`.
4. Run the bundled [workflow script](scripts/translate_video.py) through Hermes' absolute skill-directory template variable:

   ```bash
   python3 "${HERMES_SKILL_DIR}/scripts/translate_video.py" "<URL-or-local-video>" --source-language <code-or-auto>
   ```

5. Let the script complete. Do not start a second translation of the same video concurrently.
6. Report the absolute final video path, job directory, validation status, and manifest path printed by the script.

The default output directory is `~/Videos/translated-videos/<video-id>/`. Honor a user-requested destination with `--output-root /absolute/path`.

## Respect dependency boundaries

The script performs a read-only preflight before downloading or translating. It must not automatically install packages, download large models, modify another project, or restart the shared TTS service.

If preflight fails:

- Quote the missing dependency or model paths reported by the script.
- Tell the user the reported approximate download size when one is available.
- Ask permission before installing or downloading anything large.
- Keep any Python dependencies inside the pyVideoTrans `.venv`.
- Use the local Qwen TTS executable directly; do not use or stop the TTS server on port `18081`.

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

Treat the `final_video` value in the script's final JSON output as authoritative.
