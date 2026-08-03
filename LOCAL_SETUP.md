# 本机本地服务配置

这个启动器使用当前设备已经运行的两个 OpenAI 兼容服务，不启动 Ollama：

| 环节 | 服务/模型 |
| --- | --- |
| 英语 STT | Faster‑Whisper `small`，CPU `int8`，项目通过符号链接复用已有的 464MB 缓存 |
| 英语→中文翻译 | `http://127.0.0.1:8101/v1`，`Qwen3.6-35B-A3B-instruct`，串行调用 |
| 中文 TTS | Hermes `qwen-tts` CLI，CustomVoice Q8 模型，男声 `dylan`，单进程批量逐行生成 |
| 人声/背景分离 | 本机 Demucs 4.1.0，`htdemucs`，通过已有 ROCm GPU 环境运行 |

启动桌面界面：

```bash
cd /home/kamjin/projects/pyVideoTrans
./run_local.sh
```

在界面中选择英文源语言、简体中文目标语言；配置已预填为 Faster‑Whisper / LocalLLM / OpenAI‑TTS(Hermes)，并默认生成硬字幕。

查看配置而不启动界面：

```bash
./run_local.sh --print-config
```

也可以用命令行处理一个视频（参数中的渠道编号固定为 Faster‑Whisper=0、LocalLLM=9、OpenAI‑TTS=20）：

```bash
./run_cli_local.sh --task vtv --name /绝对路径/video.mp4 \
  --recogn_type 0 --model_name small --detect_language en \
  --translate_type 9 --source_language_code en --target_language_code zh-cn \
  --tts_type 20 --voice_role dylan \
  --subtitle_type 1 --voice_autorate --align_sub_audio --is_separate
```

当前配置已启用 Demucs 两轨分离：中文配音替换原英语人声，同时按原音量重新混入 Demucs 输出的非人声轨（音乐、环境声和音效）。混音后会限幅以防削波。Demucs 可执行文件和 `htdemucs` 模型复用本机已有安装，不会在项目环境中再次安装 PyTorch。

TTS 不调用或修改共享的 18081 `tts-server`。本项目通过 `qwen-tts --stream-by-line` 加载一次现有 CustomVoice Q8 模型，使用 `dylan` 男声批量生成整份字幕，并传入教程旁白风格、固定随机种子和采样参数；不会新增模型下载。

## 项目内性能配置

`start_local.py` 在每次启动时仅向本项目的 `videotrans/cfg.json` 写入以下值：

- `cuda_com_type=int8`：Faster‑Whisper 在 CPU 上使用 int8。
- `dubbing_wait=0`：取消每条 TTS 后的 1 秒限流等待。
- `dubbing_thread=1`：禁用渠道层并发，由单个 `qwen-tts --stream-by-line` 进程批量生成。
- `preset=veryfast`：加快硬字幕 H.264 软编码。
- `backaudio_volume=1.0`：保留 Demucs 非人声轨的原始音量。
- `localllm_max_token=4096`：限制单批翻译的尾部延迟。
本地 LLM 保持串行调用，避免多个长请求同时抢占共享 Qwen GPU 资源。项目仅修复 SRT 翻译缓存键，使相同输入的后续重跑可以命中缓存。

本配置不启动第二个 TTS 服务，不修改 `/home/kamjin/projects/hermes-tts-lab`、`/home/kamjin/projects/hermes-omnivoice-lab` 或 llama-swap 的配置。

## 安装 Hermes 视频翻译 Skill

仓库自带 `translate-video-to-chinese` skill。先按本文档完成当前项目和本机模型配置，再从 fork 的功能分支安装 skill：

```bash
hermes skills install https://raw.githubusercontent.com/kamjin3086/pyvideotrans/feat/local-amd-dubbing-workflow/skills/translate-video-to-chinese/SKILL.md
```

安装后重启 Hermes，或在交互会话中执行 `/reload-skills`。随后可以直接说：

```text
帮我使用 skill 翻译这个视频：https://www.youtube.com/watch?v=FhTjL1FxRUs
```

skill 会依次执行环境预检、单视频下载、英文语音识别、串行中译、Demucs 人声替换、Qwen CLI 男声配音、中文字幕压制和成片校验。默认输出到 `~/Videos/translated-videos/<视频ID>/`，最终 MP4、源视频、日志和 `job.json` 清单会保存在同一个任务目录中。

预检不会自动安装依赖或下载大模型。若缺少 Faster-Whisper、Qwen TTS 或 Demucs 资源，它会先停止并报告缺失项和大致体积，避免未经确认占用磁盘或修改其他项目。

也可以在仓库内直接检查环境：

```bash
python3 skills/translate-video-to-chinese/scripts/translate_video.py --preflight-only
```
