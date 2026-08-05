# serious-male-05

这是视频转译工作流的沉稳中文男声音色包，用于纪录片、知识、心理、历史、科学和严肃叙事类内容。

- `reference.spk`：Qwen Base speaker encoder 提取的 2048 维说话人嵌入。
- `reference.rvq`：参考语音的 16-codebook、168 帧声学编码。
- `reference.txt`：与参考编码严格对应的中文文本，用于 ICL 克隆。
- `profile.json`：音色用途、提取参数、校验结果和文件哈希。

运行时不需要原始 MP3 或 WAV。工作流使用已有的
`qwen-talker-1.7b-base-Q8_0.gguf` 和 `qwen-tokenizer-12hz-Q8_0.gguf`，通过
`qwen-tts --ref-spk --ref-rvq --ref-text` 直接复用预提取音色，不启动共享 TTS 服务。

可在工作流中显式选择：

```bash
python3 skills/translate-video-to-chinese/scripts/translate_video.py \
  "<video-url>" --voice-profile serious-male-05
```

`--voice-profile auto` 会让本地 Qwen 根据视频标题、频道、分类和简介串行选择：轻松、娱乐、生活、新闻和资讯类使用 `dylan`，其他内容使用本音色。
