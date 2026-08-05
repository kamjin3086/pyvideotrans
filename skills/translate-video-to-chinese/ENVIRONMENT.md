# Environment setup (new machine)

This document is the **reproducible environment checklist** for the
`translate-video-to-chinese` skill and this fork’s local AMD / Hermes profile.

It is written from a known-working host and meant for bringing up another
machine. Keep [`SKILL.md`](SKILL.md) for runtime orchestration; use **this file**
for “why can’t I run yet?”.

---

## Does the first run check the environment?

**Yes — every skill run starts with a read-only preflight.**

| What | Behavior |
| --- | --- |
| Entry | `python3 "${HERMES_SKILL_DIR}/scripts/vt.py" preflight` (also runs before `prepare` / `continue`) |
| Checks | Project `.venv`, `run_cli_local.sh`, `yt-dlp`, `ffmpeg`/`ffprobe`, Demucs CLI, Faster-Whisper **small** weights, `qwen-tts` / `qwen-codec` + GGUF models, voice assets under `assets/voices/`, HTTP reachability of the local LLM (`:8101/v1`) |
| Does **not** | Auto-install packages, download multi‑GB models, restart TTS/LLM services, or mutate other projects |
| On failure | Stops with missing paths / approximate sizes and asks the agent to get user permission before installing |

So a bare new machine will **fail early and explain gaps**, not silently half-run. You still need this guide to *create* those dependencies once.

Manual check without translating:

```bash
# From the pyVideoTrans repo clone:
python3 skills/translate-video-to-chinese/scripts/vt.py preflight

# Or after Hermes skill install:
python3 "${HERMES_SKILL_DIR}/scripts/vt.py" preflight
```

---

## Reference stack (known working)

Treat paths as **examples**; override with env vars (below) when your layout differs.

| Piece | Working reference | Approx. size / notes |
| --- | --- | --- |
| OS | Linux (Fedora/ROCm AMD GPU used in reference) | NVIDIA CUDA also works if Demucs device is `cuda` |
| Repo | This fork: `pyVideoTrans` with `.venv` + `run_cli_local.sh` | Python **3.10+** (reference used 3.12) |
| STT | Faster-Whisper **small**, CPU int8 | ~464 MB HF cache `models--Systran--faster-whisper-small` |
| Translation LLM | OpenAI-compatible HTTP on **`http://127.0.0.1:8101/v1`** | Model id like `Qwen3.6-35B-A3B-instruct` (must already be served) |
| TTS | Hermes **`qwen-tts`** + **`qwen-codec`** CLI (not the shared `:18081` tts-server) | Lived under sibling `hermes-tts-lab` in the reference host |
| TTS GGUF | CustomVoice 1.7B Q8, Base 1.7B Q8, 12 Hz tokenizer/codec Q8 | ~2 GB + ~2 GB + ~278 MB |
| Demucs | External CLI `demucs`, model `htdemucs` | Separate venv/PyTorch (ROCm or CUDA); **not** installed into project `.venv` |
| Tools | `ffmpeg`, `ffprobe`, `yt-dlp` | System / user PATH |
| Voice assets | In-repo `assets/voices/` (`serious-male-05`, `gender-router`, `female-01`…`10`) | Small files; ship with the repo |

**GPU rule:** Demucs and the `:8101` LLM typically share one GPU. Do not chat/poll the LLM while `separate` is running; the skill’s `vt.py continue` loop is designed for that.

---

## Bring-up checklist (new host)

Do these **once** before expecting the Hermes skill to succeed.

### 1. Clone this fork and create the project venv

```bash
git clone https://github.com/kamjin3086/pyvideotrans.git
cd pyvideotrans
# Prefer the repo’s documented Python flow (uv or venv). Example:
python3 -m venv .venv
source .venv/bin/activate
# Install per upstream + this fork’s needs used by run_cli_local.sh / start_local.py
pip install -U pip
# Use the project’s usual install path (uv sync / requirements) until
# `.venv/bin/python` and `./run_cli_local.sh` exist.
```

Confirm:

```bash
test -x .venv/bin/python && test -x ./run_cli_local.sh && echo ok
```

Point Hermes / scripts at the clone if it is not `~/projects/pyVideoTrans`:

```bash
export PYVIDEOTRANS_HOME=/absolute/path/to/pyvideotrans
```

### 2. System tools

```bash
# Fedora-ish example — adapt to your distro
sudo dnf install -y ffmpeg
pipx install yt-dlp   # or: pip install --user yt-dlp
```

Demucs must be a **working CLI** on PATH (or `PYVIDEOTRANS_DEMUCS_BIN`), with its **own** PyTorch stack capable of `htdemucs` on your GPU (device name stays `cuda` under ROCm).

```bash
export PYVIDEOTRANS_DEMUCS_BIN=/path/to/demucs
export PYVIDEOTRANS_DEMUCS_MODEL=htdemucs
export PYVIDEOTRANS_DEMUCS_DEVICE=cuda
demucs --help
```

### 3. Faster-Whisper `small` weights (~464 MB)

Either let Hugging Face cache them once (agent should ask before large downloads), or place / symlink so preflight finds:

- `$PYVIDEOTRANS_HOME/models/models--Systran--faster-whisper-small/…/model.bin`, or
- `~/.cache/huggingface/hub/models--Systran--faster-whisper-small/…/model.bin`

Optional pin:

```bash
export PYVIDEOTRANS_WHISPER_MODEL_DIR=/path/to/models--Systran--faster-whisper-small
```

### 4. Local translation LLM (`:8101`)

Start whatever serves an OpenAI-compatible `/v1/models` (llama-swap, vLLM, etc.). Defaults:

```bash
export PYVIDEOTRANS_LLM_API=http://127.0.0.1:8101/v1
export PYVIDEOTRANS_LLM_MODEL=Qwen3.6-35B-A3B-instruct
curl -sS "$PYVIDEOTRANS_LLM_API/models" | head
```

Preflight **requires** this endpoint to answer; it will not start the LLM for you.

### 5. Qwen TTS CLI + GGUF (sibling lab or custom paths)

Reference layout (do **not** assume you must clone the same absolute path):

```text
hermes-tts-lab/
  src/qwentts.cpp/build/qwen-tts
  src/qwentts.cpp/build/qwen-codec
  models/
    qwen-talker-1.7b-customvoice-Q8_0.gguf
    qwen-talker-1.7b-base-Q8_0.gguf
    qwen-tokenizer-12hz-Q8_0.gguf
```

Overrides:

```bash
export HERMES_TTS_HOME=/path/to/hermes-tts-lab
# or explicit:
export PYVIDEOTRANS_QWENTTS_BIN=.../qwen-tts
export PYVIDEOTRANS_QWEN_CODEC_BIN=.../qwen-codec   # optional; default = sibling of qwen-tts
export PYVIDEOTRANS_QWENTTS_MODEL=.../qwen-talker-1.7b-customvoice-Q8_0.gguf
export PYVIDEOTRANS_QWENTTS_BASE_MODEL=.../qwen-talker-1.7b-base-Q8_0.gguf
export PYVIDEOTRANS_QWENTTS_CODEC=.../qwen-tokenizer-12hz-Q8_0.gguf
```

**Do not** stop or reconfigure a shared `tts-server` on port `18081` for this skill; the workflow calls the CLI directly.

### 6. Voice assets in the repo

Ensure these exist in the clone (normally committed or shipped with the fork):

- `assets/voices/serious-male-05/{reference.spk,reference.rvq,reference.txt,source-audition.mp3}`
- ICL clone uses a **~1.5s trimmed clause** (`只需要更强的意志。`), not the full ~14.5s audition; see `profile.json` → `reference_clip`.
- `assets/voices/gender-router/{male.spk,female.spk,profile.json}`
- `assets/voices/female-styles/female-01` … `female-10` + `profile.json` + `reference.txt`

If preflight lists missing voice files, restore them from this fork — do not invent empty placeholders.

### 7. Install / refresh the Hermes skill

```bash
hermes skills install \
  https://raw.githubusercontent.com/kamjin3086/pyvideotrans/main/skills/translate-video-to-chinese/SKILL.md \
  --force --yes
```

Then copy or sync the **whole** skill directory (scripts + this file), not only `SKILL.md`:

```bash
# Example: from a local clone
rsync -a skills/translate-video-to-chinese/ ~/.hermes/skills/translate-video-to-chinese/
```

Reload: `/reload-skills` in Hermes, or restart Hermes.

Set `PYVIDEOTRANS_HOME` in the Hermes environment if the project is not at the default search path.

### 8. Verify

```bash
python3 "${HERMES_SKILL_DIR}/scripts/vt.py" preflight
```

Expected: `[preflight] …均已就绪` and a completed `[stage]` JSON with `next_command` pointing at `vt.py prepare …`.

Only then run a real video (`prepare` → loop `continue`).

---

## Environment variables (summary)

| Variable | Purpose | Default / search |
| --- | --- | --- |
| `PYVIDEOTRANS_HOME` | Project root | `~/projects/pyVideoTrans` and skill parent heuristics |
| `PYVIDEOTRANS_LLM_API` | Translation OpenAI base | `http://127.0.0.1:8101/v1` |
| `PYVIDEOTRANS_LLM_MODEL` | Model id on that API | `Qwen3.6-35B-A3B-instruct` |
| `PYVIDEOTRANS_DEMUCS_BIN` | Demucs executable | `demucs` on PATH |
| `PYVIDEOTRANS_DEMUCS_MODEL` | Demucs model name | `htdemucs` |
| `PYVIDEOTRANS_DEMUCS_DEVICE` | Device string | `cuda` |
| `HERMES_TTS_HOME` | Root to find qwen-tts + models | sibling `hermes-tts-lab` |
| `PYVIDEOTRANS_QWENTTS_*` | Explicit TTS binaries/models | Resolved from `HERMES_TTS_HOME` |
| `PYVIDEOTRANS_WHISPER_MODEL_DIR` | Faster-Whisper small dir | HF hub cache / `models/` |
| `PYVIDEOTRANS_OUTPUT_ROOT` | Job output root | `~/Videos/translated-videos` |
| `YTDLP_BIN` / `FFMPEG_BIN` / `FFPROBE_BIN` | Tool overrides | PATH |

Tunables also live in repo [`LOCAL_SETUP.md`](../../../LOCAL_SETUP.md) (`start_local.py` settings such as `dubbing_thread=1`, `backaudio_volume=1.0`).

---

## What “ready” looks like

- `vt.py preflight` exits 0.
- Demucs can run on GPU without stealing the LLM mid-`separate` (skill continue loop).
- One short video completes `prepare → … → validate` with a final MP4 under `~/Videos/translated-videos/<id>/result/`.

If preflight passes but a mid-pipeline stage fails, that is usually a runtime/GPU/content issue — use `vt.py continue <job_dir>` once, then read `worker.log` / `workflow.log`. Do not wipe with `--force` unless the user asked for a clean rerun.
