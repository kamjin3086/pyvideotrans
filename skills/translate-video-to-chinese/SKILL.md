---
name: translate-video-to-chinese
description: Download an English YouTube or other yt-dlp-compatible video, translate its speech and subtitles into Simplified Chinese with pyVideoTrans, replace only the English vocal track with a Chinese male voice while preserving background audio through Demucs, burn Chinese subtitles into the video, validate the result, and return the final path. Use when the user asks to translate, dub, localize, or download an online English video as a Chinese version, especially prompts such as “帮我使用 skill 翻译这个视频：[URL]” or “把这个 YouTube 视频做成中文配音版”.
---

# Translate Video to Chinese

Use the repository's tested local workflow. Keep translation calls serial, use Qwen TTS through its CLI, preserve the Demucs `no_vocals` background track, replace only vocals, and hard-burn Simplified Chinese subtitles.

## Run the workflow

1. Extract exactly one video URL or local video path from the user's request.
2. Ask for a URL or path only if none was provided.
3. Run the bundled [workflow script](scripts/translate_video.py) through Hermes' absolute skill-directory template variable:

   ```bash
   python3 "${HERMES_SKILL_DIR}/scripts/translate_video.py" "<URL-or-local-video>"
   ```

4. Let the script complete. Do not start a second translation of the same video concurrently.
5. Report the absolute final video path, job directory, validation status, and manifest path printed by the script.

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
