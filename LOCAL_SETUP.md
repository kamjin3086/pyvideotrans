# 本机本地服务配置

这个启动器使用当前设备已经运行的两个 OpenAI 兼容服务，不启动 Ollama：

| 环节 | 服务/模型 |
| --- | --- |
| 常用语言 STT | Faster‑Whisper `small` 多语模型，CPU `int8`，项目通过符号链接复用已有的 464MB 缓存 |
| 常用语言→中文翻译 | `http://127.0.0.1:8101/v1`，`Qwen3.6-35B-A3B-instruct`，串行调用 |
| 中文 TTS | Hermes `qwen-tts` CLI；轻松/新闻类用 CustomVoice `dylan`，其他内容用项目内 `serious-male-05` ICL 克隆音色；单进程批量逐行生成 |
| 人声/背景分离 | 本机 Demucs 4.1.0，`htdemucs`，通过已有 ROCm GPU 环境运行 |

启动桌面界面：

```bash
cd /home/kamjin/projects/pyVideoTrans
./run_local.sh
```

在界面中选择源语言和简体中文目标语言；配置已预填为 Faster‑Whisper / LocalLLM / OpenAI‑TTS(Hermes)，并默认生成硬字幕。

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

当前配置已启用 Demucs 两轨分离：中文配音替换原英语人声，同时按原音量重新混入 Demucs 输出的非人声轨（音乐、环境声和音效）。混音后会限幅以防削波。Demucs 可执行文件和 `htdemucs` 模型复用本机已有安装，不会在项目环境中再次安装 PyTorch。默认 `PYVIDEOTRANS_DEMUCS_DEVICE=cuda`（ROCm 下设备名仍是 `cuda`）。

Demucs 与 localhost:8101 的本地 Qwen **共用同一块 AMD GPU**。不要在分离阶段并发跑 LLM；不要靠提高 Hermes 超时硬撑整条流水线。skill 把流程拆成 **preflight → prepare → separate → recognize → translate → dub → validate**：agent 按阶段编排，阶段边界用一行 `user_hint` 汇报；长阶段若超过约 480s 会返回 `in_progress`，agent 立刻同阶段续等且中途不聊天。默认 `--no-clear-cache` 断点续跑；仅用户要求清权重跑时用 `--force`。分离结束后会校验 stem 能量，塌陷时自动 `--shifts 1` 重试。

TTS 不调用或修改共享的 18081 `tts-server`。本项目通过 `qwen-tts --stream-by-line` 批量生成整份字幕，并使用固定随机种子和采样参数。默认 `--voice-profile auto` 会通过 localhost:8101 的 Qwen 串行判断内容类型：轻松、娱乐、生活、新闻、资讯类选择带北京口音轻松感的 `dylan`；纪录片、知识、心理、历史、科学、教育、严肃叙事或无法判断时，选择 `assets/voices/serious-male-05` 中预提取的 2048 维说话人嵌入和 ICL 参考编码。

可用 `--voice-profile dylan` 或 `--voice-profile serious-male-05` 强制指定。后者使用本机已经存在的 `qwen-talker-1.7b-base-Q8_0.gguf`（约 2 GB）和 Q8 codec；本次没有下载新模型。音色包运行时只需要仓库中的 `.spk`、`.rvq` 和参考文本，不依赖原始试听 MP3，也不会修改 `hermes-tts-lab`。

自动配音采用两级路由，不安装 pyannote、PyTorch 或额外性别模型。STT 完成后，本地 Qwen 根据视频元数据和全片转录抽样串行分类一次，从 10 种内容风格中选择一套固定男女音色；结果写入 `voice-style-plan.json`。随后 Demucs 得到原始人声轨，项目按字幕时间段调用同一套 `qwen-codec` speaker encoder，并与仓库中两个 8 KB 的男女声线原型比较；明确女性声线使用该视频选定的 `female-01` 至 `female-10` 固定克隆音色，明确男性声线使用 Dylan 或 `serious-male-05`，短片段、重叠人声、噪声或低置信度片段保持默认。逐句结果写入 `voice-routing.json`。这里判断的是声学呈现，不是说话人的性别身份。设置 `PYVIDEOTRANS_AUTO_VOICE_STYLE=0` 可关闭视频风格选择，设置 `PYVIDEOTRANS_AUTO_VOICE_GENDER=0` 可关闭逐句男女声路由。

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

仓库自带 `translate-video-to-chinese` skill。先按本文档完成当前项目和本机模型配置，再从 fork 的主分支安装 skill：

```bash
hermes skills install https://raw.githubusercontent.com/kamjin3086/pyvideotrans/main/skills/translate-video-to-chinese/SKILL.md --force --yes
```

这里需要 `--force`，因为 Hermes 会把社区 URL 中“读取本机路径环境变量、调用 `yt-dlp`/`ffmpeg`/项目 CLI、访问本地 LLM HTTP 接口”的组合保守标记为 `CAUTION`。该参数只覆盖这种可审查的 caution 结论，不能覆盖 `dangerous`；skill 的脚本源码位于 `skills/translate-video-to-chinese/scripts/translate_video.py`，可在安装前直接检查。

安装后重启 Hermes，或在交互会话中执行 `/reload-skills`。随后可以直接说：

```text
帮我使用 skill 翻译这个视频：https://www.youtube.com/watch?v=FhTjL1FxRUs
```

skill 默认通过 `scripts/vt.py` 编排：`preflight` → `prepare <URL>` → 循环 `continue <job_dir>`，直至校验完成。长阶段可返回 `in_progress`。默认输出到 `~/Videos/translated-videos/<视频ID>/`。

源语言默认自动识别，也可以明确指定 `--source-language en|ja|ko|fr|de|es|it|pt|ru`。该轻量工作流只正式支持英语、日语、韩语、法语、德语、西班牙语、意大利语、葡萄牙语和俄语，不会为了冷门语言自动安装额外模型。

预检不会自动安装依赖或下载大模型。若缺少 Faster-Whisper、Qwen TTS 或 Demucs 资源，它会先停止并报告缺失项和大致体积，避免未经确认占用磁盘或修改其他项目。新机完整搭建清单见 skill 旁的 [`skills/translate-video-to-chinese/ENVIRONMENT.md`](skills/translate-video-to-chinese/ENVIRONMENT.md)。

也可以在仓库内直接检查环境：

```bash
python3 skills/translate-video-to-chinese/scripts/translate_video.py --preflight-only
```
