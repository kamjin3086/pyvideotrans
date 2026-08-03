"""Launch pyVideoTrans with the local services available on this machine.

This profile intentionally avoids Ollama and all CUDA/PyTorch model stacks:
* STT: the cached Systran/faster-whisper-small model (CPU/CTranslate2 int8)
* translation: the OpenAI-compatible Qwen3.6 35B-A3B endpoint on port 8101
* TTS: Hermes qwen-tts CLI with the CustomVoice dylan male speaker

Run ``./run_local.sh`` for the desktop GUI.  ``--print-config`` is useful for
checking the effective profile without starting Qt.
"""

from __future__ import annotations

import json
import os
import runpy
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent


LOCAL_PARAMS = {
    "is_cuda": False,
    "source_language": "en",
    "target_language": "zh-cn",
    "translate_type": 9,  # LocalLLM
    "localllm_api": os.getenv("PYVIDEOTRANS_LLM_API", "http://127.0.0.1:8101/v1"),
    "localllm_key": "no-key",
    "localllm_model": os.getenv("PYVIDEOTRANS_LLM_MODEL", "Qwen3.6-35B-A3B-instruct"),
    # Fifty subtitle blocks normally fit comfortably below 4096 output tokens.
    # Capping the response prevents one pathological request from occupying the
    # local model until the five-minute HTTP timeout.
    "localllm_max_token": 4096,
    "recogn_type": 0,  # Faster-Whisper
    "model_name": "small",
    "tts_type": 20,  # OpenAI-TTS adapter redirected to Hermes CLI below
    "openaitts_api": "http://127.0.0.1:18081/v1",
    "openaitts_key": "no-key",
    "openaitts_model": "qwen3-tts-base",
    # The local OpenAI-TTS adapter is switched to qwen-tts CLI mode below.
    "openaitts_role": "dylan",
    "voice_role": "dylan",
    "subtitle_type": 1,  # hard-burn Chinese subtitles
    "is_separate": True,
    "embed_bgm": True,
    "clear_cache": True,
    "voice_autorate": True,
    "align_sub_audio": True,
}


LOCAL_SETTINGS = {
    "uvr_models": "demucs",
    # The local CLI has no API rate limit.  The upstream default sleeps one
    # second after every subtitle, which added 333 seconds in the full test.
    "dubbing_wait": 0,
    # qwen-tts --stream-by-line owns one model process and batches all subtitle
    # lines itself, so provider-level concurrency must remain disabled.
    "dubbing_thread": 1,
    # On this CPU, Faster-Whisper small/int8 was ~1.95x faster than float32.
    "cuda_com_type": "int8",
    # Hard subtitles keep video encoding on the CPU; veryfast was ~1.9x faster
    # than slow locally and also beat VAAPI once libass upload overhead applied.
    "preset": "veryfast",
    # Preserve the separated non-vocal stem at its original level.  The mix
    # stage applies a limiter after adding the Chinese voice to avoid clipping.
    "backaudio_volume": 1.0,
}


def configure() -> None:
    os.environ.setdefault("PYVIDEOTRANS_DEMUCS_BIN", "/home/kamjin/.local/bin/demucs")
    os.environ.setdefault("PYVIDEOTRANS_DEMUCS_MODEL", "htdemucs")
    os.environ.setdefault("PYVIDEOTRANS_DEMUCS_DEVICE", "cuda")
    os.environ.setdefault("PYVIDEOTRANS_QWENTTS_CLI", "1")
    os.environ.setdefault("PYVIDEOTRANS_QWENTTS_BIN", "/home/kamjin/projects/hermes-tts-lab/src/qwentts.cpp/build/qwen-tts")
    os.environ.setdefault("PYVIDEOTRANS_QWENTTS_MODEL", "/home/kamjin/projects/hermes-tts-lab/models/qwen-talker-1.7b-customvoice-Q8_0.gguf")
    os.environ.setdefault("PYVIDEOTRANS_QWENTTS_CODEC", "/home/kamjin/projects/hermes-tts-lab/models/qwen-tokenizer-12hz-Q8_0.gguf")
    os.environ.setdefault("PYVIDEOTRANS_QWENTTS_INSTRUCT", "自然、沉稳的普通话男性教程旁白，语速适中，避免夸张表演")
    os.environ.setdefault("PYVIDEOTRANS_QWENTTS_SEED", "42")
    os.environ.setdefault("PYVIDEOTRANS_QWENTTS_TEMP", "0.62")
    os.environ.setdefault("PYVIDEOTRANS_QWENTTS_TOP_P", "0.9")
    # Importing config creates the project's persistent params/settings files.
    from videotrans.configure import config

    config.init_run()
    config.settings.parse_init(LOCAL_SETTINGS)
    config.params.getset_params(LOCAL_PARAMS)


def main() -> int:
    if "--print-config" in sys.argv:
        print(json.dumps(LOCAL_PARAMS, ensure_ascii=False, indent=2))
        return 0

    configure()
    args = [arg for arg in sys.argv[1:] if arg != "--print-config"]
    if args and args[0] in {"--webui", "webui"}:
        # WebUI is an optional upstream extra and is deliberately not installed
        # in the AMD-minimal profile.  Keep the error actionable if requested.
        try:
            import gradio  # noqa: F401
        except ImportError as exc:
            raise SystemExit(
                "WebUI 需要额外安装 gradio；当前轻量部署只启用桌面 GUI。"
            ) from exc
        sys.argv = [str(ROOT / "webui.py"), *args[1:]]
        runpy.run_path(str(ROOT / "webui.py"), run_name="__main__")
        return 0

    sys.argv = [str(ROOT / "sp.py"), *args]
    runpy.run_path(str(ROOT / "sp.py"), run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
