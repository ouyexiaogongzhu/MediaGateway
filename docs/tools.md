# 本地引擎盘点（P0 实测）

> 2026-09-04 · Mac M5 Pro 48GB · Gateway Worker 实现依据。
> 工具根目录：`/Users/vincent/tool`（后续所有工具项目放这里）。

## 内存预算总览（48GB 统一内存）

| 引擎 | 峰值内存（实测） | 并行结论 |
|---|---|---|
| iris.c (flux-klein-9b，唯一档) | ~11GB RSS | 可与 cosyvoice 并行 |
| cosyvoice 0.5B | 8.7GB RSS | 可与 iris 并行 |
| h3.c (MiniMax-H3) | MEM_GB=35 独占调度 | 单独跑，其余排队 |
| qwen3.8-27b (TEXT) | 19.3GB RSS=MEM_GB | 与 video 互斥（_admit_next 卸载） |
| flashvsr / ffmpeg | 低 | 随插随跑 |

## 1. iris.c — Image Engine

- 位置：`~/tool/iris.c`，二进制 `./iris`，另有 `libiris.dylib`（后续可 ctypes，同 h3 模式）
- 权重：`flux-klein-4b/` 15G（主力）、`zimage-turbo/` 31G（备用）
- CLI：

```bash
cd ~/tool/iris.c
./iris -d flux-klein-4b -p "PROMPT" --seed 42 --steps 4 -W 1024 -H 1024 -o out.png
# img2img: 加 -i input.png
```

- 实测（1024×1024 / 4 steps）：**66.8s 总耗时（含模型加载）**，10.8GB RSS
- 已验证能力：txt2img、img2img（`run_test.py` 内置参考图回归）

## 2. h3.c — Video Engine（唯一视频引擎；LTX-2.5 已于 2026-09-27 下线删除）

- 位置：`~/tool/h3.c`，`libh3.dylib`；权重 `MiniMax-H3/` 196G（`FL2VA/` 首尾帧、`Ref2VA/` 参考视频 两种 transformer，内容不同不可互删）
- 调用：ctypes FFI（`vendor/h3_bridge.py`，加载时 chdir 到 dylib 目录）
- 关键约束：
  - 分辨率宽高均为 32 倍数，乘积 ≤ 768×1344（=官方竖屏画质基准档）；H3-Base 为 768p 模型
  - 严格 16:9 最大 1024×576；1280×720 非法（720 非 32 倍数，会被 _round32 静默改形）
  - 时序格 5+17n；参数 `seconds` 胜过 `frames`
- 实测渲染（15s 片）：768×1344≈123min、768×1024≈76min、544×960≈41min、288×512≈8min（超线性，batch 用草稿档）；frames=362 单次渲染已验证
- 默认采样档 steps4/reuse2/core4（WorkBuddy 62 链实证）

## 3. cosyvoice — Voice Engine

- 位置：`~/tool/cosyvoice`（Fun-CosyVoice3-0.5B），Python 环境 `.venv/bin/python`
- 两种用法：
  1. 微服务：`tts_server.py`（FastAPI **:8001**，当前未启动）
     `POST /tts {text, prompt_text, prompt_wav, speed, out_path}` → `{ok, wav_b64|path, sample_rate}`
     `prompt_text` 必须含 `<|endofprompt|>`
  2. 直调：`.venv/bin/python test_tts.py`（zero_shot 推理）
- 音色克隆：zero-shot，每个 voiceId = 参考音频 + 参考文本；注册表复用 `h3cweb/workers/cosyvoice/voices.json`，客户端复用 `client.py`（重试逻辑齐全）
- 实测：冷启动约 4 分钟（含 modelscope 下载 wetext）；热加载 **4.2s** + 3 秒音频推理 **8.0s**，8.7GB RSS，采样率 24000

## 4. 其他

- **不列入开发计划**：`~/tool/qwen`（27B LLM）、`~/tool/qwen-asr` — 与本流水线无关
- Music：ACE-Step 1.5 **未部署**，Phase 4 下载
- FFmpeg：系统级，统一走 `server/render.py`（concat/mux/freeze/extract_last_frame，替代 h3cweb 的 *.sh）

## 5. h3cweb 可复制清单 → MediaGateway

| 文件 | 用途 |
|---|---|
| `server/h3_bridge.py` | h3 ctypes FFI 核心（原样复制） |
| `workers/cosyvoice/client.py` + `voices.json` | TTS 客户端 + 音色表（原样复制） |
| `workers/render/*.sh` | ffmpeg 拼接/混音/冻帧（原样复制） |
| `server/main.py` | 参考（队列/进度模式），MediaGateway 重写为多 Worker 统一调度 |

## 5. ACE-Step 1.5 — Music Engine（P4 已部署）

- 位置：`~/tool/ace-step`（官方 repo `ace-step/ACE-Step-1.5`，权重 `ACE-Step/Ace-Step1.5` 共 9.4GB）
- 独立 `.venv`（py3.12 + torch MPS/MLX）；包装脚本 `mg_music.py`（DiT-only，thinking=False）
- 实测：45s cinematic BGM 冷 128s / **暖 17s**，峰值 RSS **14.8GB**，输出 48kHz/16bit 立体声 WAV
- Memory budget：`MEM_GB=15`（保守值）；LM/thinking 路径未启用（需要歌词扩展时再开）

## 6. FlashVSR — Video Upscale（P12，唯一存活超分）

- `server/workers/flashvsr.py`，`POST /v1/upscale`；分辨率族 576/720/1080（短边语义，128 倍数规则）
- 实测：15s→1080p 纯推理约 5.3 分钟、墙钟约 8.5 分钟（含排队+引擎加载）；同 seed 同输入逐位元可复现
- `FLASHVSR_NO_MASK=1` 必开（稠密 SDPA：快 1.8×、无亮度漂移）；worker/CLI 已默认
- 推理盒宽高都必须是 128 的倍数（VAE/8 × patch(1,2,2)，两空间维各 %8），推理后中心裁回精确尺寸
- 分辨率族（32 网格精确比例）：竖 288×512/576×1024/1080×1920、横 512×288/1024×576/1920×1080
- SeedVR2（>2h/5s 片）与 Real-ESRGAN（逐帧条纹）已弃用；seedvr2 worker/tests/~/tool/seedvr2（11G）已于 2026-09-27 删除
- 详见 memory `project_flashvsr_upscale.md`

## 7. SDXL Daemon — 图像 + aux 控制图（2026-09-27）

- `~/tool/sdxl-daemon`（无 launchd，手动 nohup）；生图 + `POST /annotate`（白模/深度/线稿/骨骼，<2s/张）
- Gateway `POST /v1/aux` 同步直通；images/edits 模型名含控制类型词即走 ControlNet（无官方 SDXL normal CN，白模复用 depth CN）
- 坑：NormalBaeDetector 小图固定输出 512×512，daemon 已做尺寸归位

## Uncensored Text Encoder(2026-09-07 已装)

- `flux-klein-9b/text_encoder/` 已替换为 [ponpoke/flux2-klein-9b-uncensored-text-encoder](https://huggingface.co/ponpoke/flux2-klein-9b-uncensored-text-encoder)
  (ablated Qwen3-8B,BF16,含 index;原版备份在同目录 `text_encoder.orig/`)
- **效果**:角色参考图的服装/发型锚定显著增强——无造型锁 prompt 也能跟随参考图
  (实测:冰法师白蓝长裙不再漂成裤子);无害提示词余弦 0.97,一般能力无损
- 原理:审查在文字编码器(概念 embedding 打折),DiT 无拒绝回路;ablation 移除拒绝方向
- 回滚:`rm -rf text_encoder && mv text_encoder.orig text_encoder`
