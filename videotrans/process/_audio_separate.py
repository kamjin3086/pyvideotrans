# -*- coding: utf-8 -*-
import os
import shutil
import subprocess
import traceback, time
from videotrans.configure.config import ROOT_DIR, logger, settings
from pathlib import Path
from videotrans.process._audio_utils import _write_log


def vocal_bgm(*, input_file, vocal_file, instr_file, logs_file=None, is_cuda=False, uvr_models="UVR-MDX-NET-Inst_HQ_4"):
    if str(uvr_models).lower().startswith('demucs'):
        return vocal_bgm_demucs(input_file=input_file, vocal_file=vocal_file,
                                instr_file=instr_file, logs_file=logs_file)

    if uvr_models.startswith('spleeter'):
        return vocal_bgm_spleeter(input_file=input_file, vocal_file=vocal_file, instr_file=instr_file,
                                  logs_file=logs_file)

    import numpy as np
    import sherpa_onnx
    import soundfile as sf

    def create_offline_source_separation():
        model = f"{ROOT_DIR}/models/onnx/{uvr_models}.onnx"

        if not Path(model).is_file():
            raise ValueError(f"{model} does not exist.")

        _cf = sherpa_onnx.OfflineSourceSeparationConfig(
            model=sherpa_onnx.OfflineSourceSeparationModelConfig(
                uvr=sherpa_onnx.OfflineSourceSeparationUvrModelConfig(
                    model=model,
                ),
                num_threads=int(settings.get('noise_separate_nums', 4)),
                debug=False,
                provider="cpu",
            )
        )
        if not _cf.validate():
            raise ValueError("Please check your config.")

        return sherpa_onnx.OfflineSourceSeparation(_cf)

    def load_audio(wav_file):
        samples, sample_rate = sf.read(wav_file, dtype="float32", always_2d=True)
        samples = np.transpose(samples)
        assert (
                samples.shape[1] > samples.shape[0]
        ), f"You should use (num_channels, num_samples). {samples.shape}"

        assert (
                samples.dtype == np.float32
        ), f"Expect np.float32 as dtype. Given: {samples.dtype}"

        return samples, sample_rate

    start = time.time()
    try:
        sp = create_offline_source_separation()
        samples, sample_rate = load_audio(input_file)
        samples = np.ascontiguousarray(samples)
        _write_log(logs_file, "vocals non_vocals...")
        output = sp.process(sample_rate=sample_rate, samples=samples)
        end = time.time()
        non_vocals = output.stems[0].data
        vocals = output.stems[1].data

        vocals = np.transpose(vocals)
        non_vocals = np.transpose(non_vocals)

        sf.write(vocal_file, vocals, samplerate=output.sample_rate)
        sf.write(instr_file, non_vocals, samplerate=output.sample_rate)

        elapsed_seconds = end - start
        _write_log(logs_file, f" use time:{elapsed_seconds:.3f}s")
        logger.debug(f'分离背景声和人声成功[{uvr_models}],耗时 {elapsed_seconds:.3f}s')
        return True, None
    except Exception as e:
        msg = traceback.format_exc()
        logger.exception(f"人声背景声分离失败{e}:{msg}", exc_info=True)
        return False, f'{e}{msg}'


def _demucs_find_stems(out_dir: Path, model: str, input_file: str):
    track_dir = out_dir / model / Path(input_file).stem
    vocals = track_dir / 'vocals.wav'
    instrumental = track_dir / 'no_vocals.wav'
    if not vocals.exists() or not instrumental.exists():
        # Keep compatibility with alternate Demucs wrappers that choose a
        # slightly different model directory layout.
        vocal_candidates = list(out_dir.rglob('vocals.wav'))
        instrumental_candidates = list(out_dir.rglob('no_vocals.wav'))
        vocals = vocal_candidates[0] if vocal_candidates else vocals
        instrumental = instrumental_candidates[0] if instrumental_candidates else instrumental
    return vocals, instrumental


def _demucs_remove_outputs(*paths: Path):
    for path in paths:
        try:
            if path and Path(path).exists():
                Path(path).unlink()
        except OSError:
            pass


def _demucs_run_once(*, demucs_bin: str, model: str, device: str, input_file: str,
                     out_dir: Path, logs_file=None, extra_args=None):
    command = [
        demucs_bin,
        '-n', model,
        '--two-stems=vocals',
        '--float32',
        '-o', str(out_dir),
    ]
    if device.lower() != 'auto':
        command.extend(['-d', device])
    if extra_args:
        command.extend(extra_args)
    command.append(str(input_file))

    _write_log(logs_file, f'Demucs starting: {" ".join(command)}')
    logger.debug(
        'Demucs GPU 分离开始；请保持调用方阻塞等待，期间不要并发发起占用同一 GPU 的本地 LLM 请求'
    )
    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding='utf-8',
        errors='replace',
        check=False,
    )
    if result.stdout:
        _write_log(logs_file, result.stdout[-12000:])
    if result.returncode != 0:
        msg = f'Demucs exited with code {result.returncode}: {result.stdout[-4000:]}'
        logger.error(msg)
        return False, msg, None, None

    vocals, instrumental = _demucs_find_stems(out_dir, model, input_file)
    if not vocals.exists() or not instrumental.exists():
        msg = f'Demucs output not found below {out_dir}'
        logger.error(msg)
        return False, msg, None, None
    return True, None, vocals, instrumental


def vocal_bgm_demucs(*, input_file, vocal_file, instr_file, logs_file=None):
    """Separate vocals/background with the machine's standalone Demucs CLI.

    Demucs is intentionally invoked as an external command so the lightweight
    pyVideoTrans environment does not need to install a second PyTorch stack.
    ``PYVIDEOTRANS_DEMUCS_BIN`` may point at a wrapper such as the local
    ``~/.local/bin/demucs`` launcher; the wrapper's own ROCm environment is
    inherited by the worker process.

    After a successful exit code, stem energy is validated against the source.
    Silent post-segment collapse (common when another process contends for the
    same GPU during separation) fails the run and triggers one cheaper retry
    with ``--shifts 1``. Callers should block until this returns and avoid
    concurrent local-LLM GPU use; this function does not unload any LLM.
    """
    from videotrans.process._demucs_validate import validate_demucs_stems

    start = time.time()
    demucs_bin = os.environ.get('PYVIDEOTRANS_DEMUCS_BIN', '').strip()
    if not demucs_bin:
        demucs_bin = shutil.which('demucs') or ''
    if not demucs_bin or not Path(demucs_bin).exists():
        raise FileNotFoundError(
            'Demucs executable not found; set PYVIDEOTRANS_DEMUCS_BIN'
        )

    model = os.environ.get('PYVIDEOTRANS_DEMUCS_MODEL', 'htdemucs').strip() or 'htdemucs'
    device = os.environ.get('PYVIDEOTRANS_DEMUCS_DEVICE', 'cuda').strip() or 'cuda'
    out_dir = Path(instr_file).parent / 'demucs'
    out_dir.mkdir(parents=True, exist_ok=True)
    vocal_path = Path(vocal_file)
    instr_path = Path(instr_file)

    attempts = [
        {"label": "default", "extra_args": None},
        # Cheaper second pass if the first stems collapsed under GPU contention.
        {"label": "shifts1", "extra_args": ["--shifts", "1"]},
    ]
    last_error = None
    for index, attempt in enumerate(attempts):
        try:
            ok, err, vocals, instrumental = _demucs_run_once(
                demucs_bin=demucs_bin,
                model=model,
                device=device,
                input_file=input_file,
                out_dir=out_dir,
                logs_file=logs_file,
                extra_args=attempt["extra_args"],
            )
        except Exception as e:
            logger.exception(f'Demucs 启动失败: {e}', exc_info=True)
            return False, str(e)
        if not ok:
            last_error = err
            continue

        valid, report = validate_demucs_stems(input_file, vocals, instrumental)
        _write_log(logs_file, f'Demucs stem validation[{attempt["label"]}]: {report}')
        if not valid:
            last_error = (
                "Demucs produced near-silent stems after the first segment; "
                "this usually means the GPU was shared with another heavy job "
                f"(often a local LLM). validation={report}"
            )
            logger.error(last_error)
            _demucs_remove_outputs(vocals, instrumental, vocal_path, instr_path)
            # Drop cached demucs track dir so the retry writes fresh files.
            try:
                track_dir = vocals.parent if vocals else None
                if track_dir and track_dir.exists():
                    shutil.rmtree(track_dir, ignore_errors=True)
            except OSError:
                pass
            if index + 1 < len(attempts):
                _write_log(logs_file, 'Demucs retrying with --shifts 1 after stem validation failure')
                continue
            return False, last_error

        shutil.copy2(vocals, vocal_path)
        shutil.copy2(instrumental, instr_path)
        elapsed_seconds = time.time() - start
        _write_log(
            logs_file,
            f'Demucs finished in {elapsed_seconds:.3f}s attempt={attempt["label"]} device={device}'
        )
        logger.debug(
            f'分离背景声和人声成功[demucs/{model}/{attempt["label"]}],耗时 {elapsed_seconds:.3f}s'
        )
        return True, None

    return False, last_error or 'Demucs failed'


def vocal_bgm_spleeter(*, input_file, vocal_file, instr_file, logs_file=None):
    import numpy as np
    import sherpa_onnx
    import soundfile as sf

    def create_offline_source_separation():
        vocals = f"{ROOT_DIR}/models/onnx/vocals.fp16.onnx"
        accompaniment = f"{ROOT_DIR}/models/onnx/accompaniment.fp16.onnx"
        config = sherpa_onnx.OfflineSourceSeparationConfig(
            model=sherpa_onnx.OfflineSourceSeparationModelConfig(
                spleeter=sherpa_onnx.OfflineSourceSeparationSpleeterModelConfig(
                    vocals=vocals,
                    accompaniment=accompaniment,
                ),
                num_threads=int(settings.get('noise_separate_nums', 4)),
                debug=False,
                provider="cpu",
            )
        )
        if not config.validate():
            raise ValueError("Please check your config.")

        return sherpa_onnx.OfflineSourceSeparation(config)

    def load_audio(wav_file):

        samples, sample_rate = sf.read(wav_file, dtype="float32", always_2d=True)
        samples = np.transpose(samples)
        assert (
                samples.shape[1] > samples.shape[0]
        ), f"You should use (num_channels, num_samples). {samples.shape}"

        assert (
                samples.dtype == np.float32
        ), f"Expect np.float32 as dtype. Given: {samples.dtype}"

        return samples, sample_rate

    start = time.time()
    try:
        sp = create_offline_source_separation()
        samples, sample_rate = load_audio(input_file)
        samples = np.ascontiguousarray(samples)

        output = sp.process(sample_rate=sample_rate, samples=samples)
        end = time.time()

        assert len(output.stems) == 2, len(output.stems)

        vocals = output.stems[0].data
        non_vocals = output.stems[1].data
        vocals = np.transpose(vocals)
        non_vocals = np.transpose(non_vocals)
        sf.write(vocal_file, vocals, samplerate=output.sample_rate)
        sf.write(instr_file, non_vocals, samplerate=output.sample_rate)

        elapsed_seconds = end - start
        _write_log(logs_file, f" use time:{elapsed_seconds:.3f}s")
        logger.debug(f"分离背景声和人声成功[spleeter],耗时: {elapsed_seconds:.3f}s")
        return True, None
    except Exception as e:
        msg = traceback.format_exc()
        logger.exception(f"人声背景声分离失败{e}:{msg}", exc_info=True)
        return False, f'{e}{msg}'
