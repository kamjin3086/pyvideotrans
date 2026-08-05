# -*- coding: utf-8 -*-
"""Stage orchestration: sync short stages + detached tick for long ones."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable

import job_runtime
import pipeline_stages as stages


WriteManifestFn = Callable[[Path, dict[str, Any]], None]
RunLoggedFn = Callable[..., None]
EnvFn = Callable[..., dict[str, str]]
PreflightFn = Callable[[str | None], dict[str, Path | str]]
ExpandFn = Callable[[str | Path], Path]


def _load_job(manifest_path: Path) -> dict[str, Any]:
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def build_vtv_command(
    config: dict[str, Path | str],
    *,
    source_video: Path,
    result_dir: Path,
    source_language: str,
    voice_role: str,
    cli_stage: str,
    force: bool,
    cache_folder: Path | None = None,
) -> list[str]:
    command = [
        str(config["run_cli"]),
        "--task",
        "vtv",
        "--name",
        str(source_video),
        "--output-dir",
        str(result_dir),
        "--recogn_type",
        "0",
        "--model_name",
        "small",
        "--detect_language",
        source_language,
        "--translate_type",
        "9",
        "--source_language_code",
        source_language,
        "--target_language_code",
        "zh-cn",
        "--tts_type",
        "20",
        "--voice_role",
        voice_role,
        "--subtitle_type",
        "1",
        "--voice_autorate",
        "--align_sub_audio",
        "--is_separate",
        "--vtv-stage",
        cli_stage,
        "--clear_cache" if force else "--no-clear-cache",
    ]
    if cache_folder is not None:
        command.extend(["--cache-folder", str(cache_folder)])
    return command


def workcache_dir(job_dir: Path) -> Path:
    """Stable cache shared by all stage workers for one job.

    Upstream TEMP_DIR embeds os.getpid(), so each detached stage otherwise
    starts with an empty cache and recognize fails looking for source_wav.
    """
    path = job_dir / "workcache"
    path.mkdir(parents=True, exist_ok=True)
    return path


def hydrate_workcache(
    *,
    job_dir: Path,
    result_dir: Path,
    source_language: str,
    ffmpeg: Path | str | None = None,
) -> dict[str, str]:
    """Ensure workcache has vocal/instrument/source wav regenerated from result/.

    Safe to call before recognize/translate/dub so mid-pipeline retries work
    even if an older process used a pid-scoped tmp dir.
    """
    import shutil
    import subprocess

    cache = workcache_dir(job_dir)
    notes: dict[str, str] = {"cache_folder": str(cache)}
    for name in ("vocal.wav", "instrument.wav"):
        src = result_dir / name
        dst = cache / name
        if src.is_file() and src.stat().st_size > 1000:
            if (not dst.is_file()) or dst.stat().st_size != src.stat().st_size:
                shutil.copy2(src, dst)
            notes[name] = str(dst)

    source_wav = cache / f"{source_language or 'auto'}.wav"
    vocal = cache / "vocal.wav"
    if vocal.is_file() and (
        not source_wav.is_file() or source_wav.stat().st_size < 1000
    ):
        ff = shutil.which(str(ffmpeg)) if ffmpeg else shutil.which("ffmpeg")
        if not ff:
            raise RuntimeError("hydrate_workcache: ffmpeg not found")
        cmd = [
            ff,
            "-y",
            "-i",
            str(vocal),
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(source_wav),
        ]
        proc = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if proc.returncode or not source_wav.is_file() or source_wav.stat().st_size < 1000:
            raise RuntimeError(
                "hydrate_workcache: failed to build source wav from vocal.wav: "
                + (proc.stderr or proc.stdout or "")[-1500:]
            )
        notes["source_wav"] = str(source_wav)
    elif source_wav.is_file():
        notes["source_wav"] = str(source_wav)
    return notes


def run_cli_stage(
    config: dict[str, Path | str],
    *,
    source_video: Path,
    result_dir: Path,
    log_path: Path,
    source_language: str,
    voice_role: str,
    voice_profile: str,
    metadata: dict[str, Any],
    cli_stage: str,
    force: bool,
    run_logged: RunLoggedFn,
    translation_environment: EnvFn,
    cache_folder: Path | None = None,
) -> None:
    command = build_vtv_command(
        config,
        source_video=source_video,
        result_dir=result_dir,
        source_language=source_language,
        voice_role=voice_role,
        cli_stage=cli_stage,
        force=force,
        cache_folder=cache_folder,
    )
    run_logged(
        command,
        log_path,
        Path(config["project"]),
        translation_environment(config, voice_profile, metadata),
    )


def _job_context(job: dict[str, Any], job_dir: Path, expand: ExpandFn) -> dict[str, Any]:
    settings = job.get("settings") or {}
    return {
        "source_video": expand(job["source_video"]),
        "result_dir": job_dir / "result",
        "log_path": expand(job.get("log") or str(job_dir / "workflow.log")),
        "manifest_path": job_dir / "job.json",
        "metadata": dict(job.get("metadata") or {"title": job.get("title")}),
        "source_language": str(settings.get("source_language") or "auto"),
        "voice_profile": str(settings.get("voice_profile_requested") or "auto"),
        "voice_role": str(settings.get("voice_role") or "serious-male-05"),
        "force": bool(job.get("force")),
    }


def execute_cli_stage_in_process(
    *,
    config: dict[str, Path | str],
    job: dict[str, Any],
    job_dir: Path,
    stage: str,
    force: bool,
    run_logged: RunLoggedFn,
    translation_environment: EnvFn,
    write_manifest: WriteManifestFn,
    expand: ExpandFn,
    now_iso: Callable[[], str],
) -> dict[str, Any]:
    ctx = _job_context(job, job_dir, expand)
    cli_stage = stages.CLI_STAGE_MAP[stage]
    wipe = force and stage == "separate"
    if wipe:
        job["force"] = False
        write_manifest(ctx["manifest_path"], job)

    cache_folder = workcache_dir(job_dir)
    # Post-separate stages need source_wav inside the stable cache. Hydrate from
    # result/ so retries still work after an older pid-scoped tmp run.
    if stage in {"recognize", "translate", "dub"}:
        notes = hydrate_workcache(
            job_dir=job_dir,
            result_dir=ctx["result_dir"],
            source_language=ctx["source_language"],
            ffmpeg=config.get("ffmpeg"),
        )
        print(f"[stage] hydrated workcache for {stage}: {notes}")

    job_runtime.write_runtime(
        job_dir,
        {
            "status": "running",
            "stage": stage,
            "phase": stage,
            "worker_pid": os.getpid(),
            "message": f"running stage {stage}",
            "cache_folder": str(cache_folder),
            "heartbeat_at": now_iso(),
        },
    )
    run_cli_stage(
        config,
        source_video=ctx["source_video"],
        result_dir=ctx["result_dir"],
        log_path=ctx["log_path"],
        source_language=ctx["source_language"],
        voice_role=ctx["voice_role"],
        voice_profile=ctx["voice_profile"],
        metadata=ctx["metadata"],
        cli_stage=cli_stage,
        force=wipe,
        run_logged=run_logged,
        translation_environment=translation_environment,
        cache_folder=cache_folder,
    )
    _ready, artifacts = stages.stage_artifacts_ready(
        stage,
        job_dir=job_dir,
        result_dir=ctx["result_dir"],
        source_video=ctx["source_video"],
        source_language=ctx["source_language"],
        force=False,
    )
    stages.mark_stage(job, stage, status="completed", artifacts=artifacts)
    write_manifest(ctx["manifest_path"], job)
    job_runtime.write_runtime(
        job_dir,
        {
            "status": "completed",
            "stage": stage,
            "phase": stage,
            "worker_pid": os.getpid(),
            "message": f"stage {stage} completed",
            "artifacts": artifacts,
            "heartbeat_at": now_iso(),
        },
    )
    return artifacts


def run_worker_main(
    *,
    job_dir: Path,
    project_dir: str | None,
    stage: str,
    preflight: PreflightFn,
    run_logged: RunLoggedFn,
    translation_environment: EnvFn,
    write_manifest: WriteManifestFn,
    now_iso: Callable[[], str],
    expand: ExpandFn,
) -> int:
    job_dir = expand(str(job_dir))
    manifest_path = job_dir / "job.json"
    job = _load_job(manifest_path)
    config = preflight(project_dir)
    try:
        artifacts = execute_cli_stage_in_process(
            config=config,
            job=job,
            job_dir=job_dir,
            stage=stage,
            force=bool(job.get("force")),
            run_logged=run_logged,
            translation_environment=translation_environment,
            write_manifest=write_manifest,
            expand=expand,
            now_iso=now_iso,
        )
        print(json.dumps({"status": "completed", "stage": stage, "artifacts": artifacts}, ensure_ascii=False))
        return 0
    except Exception as exc:
        stages.mark_stage(job, stage, status="failed", message=str(exc))
        write_manifest(manifest_path, job)
        job_runtime.write_runtime(
            job_dir,
            {
                "status": "failed",
                "stage": stage,
                "phase": stage,
                "worker_pid": os.getpid(),
                "message": str(exc),
                "error": str(exc),
                "heartbeat_at": now_iso(),
            },
        )
        raise


def ensure_stage_worker(
    *,
    config: dict[str, Path | str],
    job_dir: Path,
    stage: str,
    force: bool,
    script_path: Path,
) -> dict[str, Any]:
    runtime = job_runtime.read_runtime(job_dir)
    pid = job_runtime.read_worker_pid(job_dir)
    alive = bool(pid and job_runtime.pid_is_alive(pid))
    same_stage = runtime.get("stage") == stage

    if force:
        print(f"[stage] --force：停止旧 worker 并重跑 {stage}")
        job_runtime.stop_worker(job_dir)
        alive = False
        runtime = {}

    if same_stage and runtime.get("status") == "completed" and not force:
        return runtime
    if same_stage and runtime.get("status") == "failed" and not force and not alive:
        # Soft retry: a previous stage worker failed (often empty pid-scoped
        # cache). Re-invoking the same --stage should resume, not stick on the
        # stale failed runtime forever.
        print(f"[stage] 上次 {stage} 失败，将 resume 重试（不加 --force 清缓存）")
        runtime = {}
    if alive and same_stage:
        print(f"[stage] 复用运行中的 {stage} worker pid={pid}")
        return runtime
    if alive and not same_stage:
        print(f"[stage] 停止其它阶段 worker，改跑 {stage}")
        job_runtime.stop_worker(job_dir)
        alive = False

    if runtime.get("status") == "running" and same_stage and pid and not alive:
        print(f"[stage] {stage} worker 异常退出，resume 重启")
        runtime = {}

    worker_cmd = [
        sys_executable(),
        str(script_path),
        "--worker",
        "--stage",
        stage,
        "--job-dir",
        str(job_dir),
        "--project-dir",
        str(config["project"]),
    ]
    print(f"[stage] 启动 {stage} worker：{' '.join(worker_cmd)}")
    job_runtime.start_detached_worker(
        worker_cmd,
        job_dir,
        Path(config["project"]),
        os.environ.copy(),
    )
    return job_runtime.read_runtime(job_dir)


def sys_executable() -> str:
    import sys

    return sys.executable


def orchestrate_stage(
    *,
    config: dict[str, Path | str],
    stage: str,
    job_dir: Path,
    force: bool,
    budget_seconds: float,
    script_path: Path,
    run_logged: RunLoggedFn,
    translation_environment: EnvFn,
    write_manifest: WriteManifestFn,
    expand: ExpandFn,
    now_iso: Callable[[], str],
    validate_result: Callable[..., dict[str, Any]],
    completed_job_finalize: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run or tick one agent-facing stage; return status payload."""
    manifest_path = job_dir / "job.json"
    job = _load_job(manifest_path)
    ctx = _job_context(job, job_dir, expand)
    if force:
        job["force"] = True
        write_manifest(manifest_path, job)

    ready, artifacts = stages.stage_artifacts_ready(
        stage,
        job_dir=job_dir,
        result_dir=ctx["result_dir"],
        source_video=ctx["source_video"],
        source_language=ctx["source_language"],
        force=force and stage in {"separate", "prepare", "recognize", "translate", "dub"},
    )
    if ready and stage != "validate":
        stages.mark_stage(job, stage, status="completed", artifacts=artifacts, message="skipped existing")
        write_manifest(manifest_path, job)
        return stages.stage_payload(
            status="completed",
            stage=stage,
            job_dir=job_dir,
            message=f"stage {stage} already satisfied",
            artifacts=artifacts,
        )

    # Skill-local stages
    if stage == "validate":
        expected = ctx["result_dir"] / f"{ctx['source_video'].stem}.mp4"
        if not expected.is_file():
            _ready, arts = stages.stage_artifacts_ready(
                "dub",
                job_dir=job_dir,
                result_dir=ctx["result_dir"],
                source_video=ctx["source_video"],
                source_language=ctx["source_language"],
            )
            if not _ready:
                return stages.stage_payload(
                    status="failed",
                    stage=stage,
                    job_dir=job_dir,
                    message="找不到成片，请先完成 dub 阶段",
                )
            expected = expand(arts["final_video"])
        validation = validate_result(expected, config)
        if completed_job_finalize:
            completed_job_finalize(job, expected, validation)
        else:
            job["status"] = "completed"
            job["final_video"] = str(expected)
            job["validation"] = validation
            job["completed_at"] = now_iso()
            stages.mark_stage(job, "validate", status="completed", artifacts={"final_video": str(expected)})
            write_manifest(manifest_path, job)
        return stages.stage_payload(
            status="completed",
            stage="validate",
            job_dir=job_dir,
            message="validation ok",
            artifacts={"final_video": str(expected)},
            extra={
                "final_video": str(expected),
                "manifest": str(manifest_path),
                "validation_ok": True,
                "voice_style": validation.get("voice_style_plan", {}).get("style"),
                "female_voice": validation.get("voice_style_plan", {}).get("female_voice"),
            },
        )

    if stage not in stages.CLI_STAGE_MAP:
        return stages.stage_payload(
            status="failed",
            stage=stage,
            job_dir=job_dir,
            message=f"unknown stage {stage}",
        )

    # Long stages: detached worker + budgeted wait
    if stage in stages.LONG_STAGES:
        runtime = ensure_stage_worker(
            config=config,
            job_dir=job_dir,
            stage=stage,
            force=force,
            script_path=script_path,
        )
        if runtime.get("status") == "completed" and runtime.get("stage") == stage:
            arts = runtime.get("artifacts") or {}
            return stages.stage_payload(
                status="completed",
                stage=stage,
                job_dir=job_dir,
                message=f"stage {stage} completed",
                artifacts=arts if isinstance(arts, dict) else {},
            )
        if runtime.get("status") == "failed" and runtime.get("stage") == stage:
            return stages.stage_payload(
                status="failed",
                stage=stage,
                job_dir=job_dir,
                message=str(runtime.get("message") or runtime.get("error") or "stage failed"),
                extra={"log_tail": job_runtime.tail_text(job_runtime.worker_log_path(job_dir))},
            )

        print(f"[stage] 等待 {stage}，预算 {budget_seconds:.0f}s……")
        waited = job_runtime.wait_for_runtime(job_dir, budget_seconds=budget_seconds)
        if waited.get("status") == "completed":
            arts = waited.get("artifacts") or {}
            return stages.stage_payload(
                status="completed",
                stage=stage,
                job_dir=job_dir,
                message=f"stage {stage} completed",
                artifacts=arts if isinstance(arts, dict) else {},
            )
        if waited.get("status") == "failed":
            return stages.stage_payload(
                status="failed",
                stage=stage,
                job_dir=job_dir,
                message=str(waited.get("message") or waited.get("error") or "stage failed"),
                extra={"log_tail": job_runtime.tail_text(job_runtime.worker_log_path(job_dir))},
            )
        return stages.stage_payload(
            status="in_progress",
            stage=stage,
            job_dir=job_dir,
            message=f"stage {stage} still running",
            tick_command=(
                f'python3 "{script_path}" --stage {stage} --job-dir "{job_dir}" '
                f"--budget-seconds {int(budget_seconds)}"
            ),
            extra={
                "worker_alive": bool(waited.get("worker_alive")),
                "worker_pid": job_runtime.read_worker_pid(job_dir),
                "log_tail": job_runtime.tail_text(job_runtime.worker_log_path(job_dir), 2000),
            },
        )

    # Short CLI stages (none currently besides if we reclassify) — run sync.
    artifacts = execute_cli_stage_in_process(
        config=config,
        job=job,
        job_dir=job_dir,
        stage=stage,
        force=force,
        run_logged=run_logged,
        translation_environment=translation_environment,
        write_manifest=write_manifest,
        expand=expand,
        now_iso=now_iso,
    )
    return stages.stage_payload(
        status="completed",
        stage=stage,
        job_dir=job_dir,
        message=f"stage {stage} completed",
        artifacts=artifacts,
    )
