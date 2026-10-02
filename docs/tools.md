# 本地引擎盘点（P0 实测）

> 2026-09-04 · Mac M5 Pro 48GB · Gateway Worker 实现依据。
> 工具根目录：`/Users/vincent/tool`（后续所有工具项目放这里）。

## 内存预算总览（48GB 统一内存）

| 引擎 | 峰值内存（实测） | 并行结论 |
|---|---|---|
| h3.c (MiniMax-H3) | MEM_GB=35 独占调度 | 单独跑，其余排队 |
| **qwen_image**（Qwen-Image-2.1 7B，sd.cpp GGUF Q4_K） | ~10GB（worker MEM_GB） | 生圖主引擎（2026-09-28 投產） |
| qwen3.8-27b (TEXT, mtplx :8000) | 19.3GB RSS=MEM_GB | 与 video 互斥（_admit_next 卸载） |
| qwen3.8-uncensored (TEXT, omlx :8082) | ~19GB（未实测，外部守護進程不佔 MEM_GB） | oQ4e-mtp 量化與 mtplx 不兼容（亂碼），必須走 omlx |
| flashvsr / ffmpeg | 低 | 随插随跑 |
| ~~iris.c~~ | — | **已停用**：flux-klein-9b 模型目錄失蹤（2026-09-27 發現），route 被 qwen_image 頂替；重下 ~30GB 後可恢復 |
| cosyvoice 0.5B | 8.7GB RSS | 可与 iris 并行 |

## 1. iris.c — Image Engine（⚠ 已停用：模型目錄失蹤，生圖由 §8 Viggle/Krea2 頂替）

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
- ⚠️ **2026-10-02 修正一个被反复转述错的标签**：所谓「15s→1080p 纯推理 5.3 分钟」，**5.3 分钟是 720 级扩散本身，从不是 1080p 扩散的墙钟**。`upscale_cli.py:85` 把扩散输入硬编码为 `min(args.resolution, 720)`，所以 `--resolution 1080` 永远是「720 级扩散 + lanczos 放大」，CLI 无法触达真 1080p 扩散（要改上游代码）。
- 实际链路（source 核对）：扩散画布 **1280×768**（`snap()` 只把长边取 16 倍数 → 1280×720，`infer_mps.py:81-82` 再向上取 128 倍数 → 768 行，跑完在 `upscale_cli.py:53-55` 中心裁回）→ `s720.mp4`（`save_video quality=7`）→ 解码 → LANCZOS → `s1080.mp4`（`imageio quality=7`）→ `mux()` `-c:v copy`。**quality=7 ⇒ `-crf 15`，两次有损生成；第三个 ffmpeg 是流拷贝，不损像素**——是 2 次有损不是 3 次。
- 该路径**没有任何计时埋点**（`infer()` 只 print 一行 `inference: Xs`），5.3 / 8-9 分这两个数字都待重测；引用前请确认。已排入分阶段实测。
- `Wan2.1_VAE.pth`（484MB）从未被载入：`model_manager.py:438-440` 印 `No wan_video_vae models available` 并回传 None，`self.vae is None` 被 `load_models_to_device` 的 `is not None` guard 跳过（生产 log 实锤）。
- 同 seed 同输入逐位元可复现（首帧差 0.00/255 实测）；`FLASHVSR_NO_MASK=1` 必开（稠密 SDPA：快 1.8×、无亮度漂移），worker/CLI 已默认
- `FLASHVSR_NO_MASK=1` 必开（稠密 SDPA：快 1.8×、无亮度漂移）；worker/CLI 已默认
- 推理盒宽高都必须是 128 的倍数（VAE/8 × patch(1,2,2)，两空间维各 %8），推理后中心裁回精确尺寸
- 分辨率族（32 网格精确比例）：竖 288×512/576×1024/1080×1920、横 512×288/1024×576/1920×1080
- CLI 推理盒：`--target WxH`（128 倍数，推理后中心裁回）；`--scale 2` 会向 128 取整（576→512），精确档位用 --target；venv 在 `~/tool/FlashVSR/.venv`，跑法 `PYTHONPATH=~/tool/FlashVSR infer_mps.py in out --target WxH`
- 用途扩展：VACE 動作遷移管线的后端超分工序（288×512→640×1152→576×1024，33帧 18s，见 §8）
- SeedVR2（>2h/5s 片）与 Real-ESRGAN（逐帧条纹）已弃用；seedvr2 worker/tests/~/tool/seedvr2（11G）已于 2026-09-27 删除
- 详见 memory `project_flashvsr_upscale.md`

## 7. SDXL Daemon — 图像 + aux 控制图（2026-09-27）

- `~/tool/sdxl-daemon`（无 launchd，手动 nohup）；生图 + `POST /annotate`（白模/深度/线稿/骨骼，<2s/张）
- Gateway `POST /v1/aux` 同步直通；images/edits 模型名含控制类型词即走 ControlNet（无官方 SDXL normal CN，白模复用 depth CN）
- 坑：NormalBaeDetector 小图固定输出 512×512，daemon 已做尺寸归位

## 8. VACE 動作遷移 — 視頻換角/替身（2026-09-28，sd.cpp Metal）

- 位置：`~/tool/sd.cpp`（stable-diffusion.cpp，Metal 原生；编译需拉 ggml submodule + `-DCMAKE_POLICY_VERSION_MINIMUM=3.5`）；权重 `~/tool/sd.cpp-models/`
- 管线：源视频 → ffmpeg 16fps 抽帧 → Gateway `/v1/aux` pose（0.4s/帧）→ `sd-cli -M vid_gen`（VACE 控制视频 + 角色参考图）→ FlashVSR 超分 → 576×1024 H.264
- 量产配置（双档）：草稿 = 1.3B v2-Q4_0 + CausVid LoRA 0.8 + TAEHV（45s/2s）；成品 = 14B LightX2V-VACE Q4_K_M（QuantStack，融合蒸馏免 LoRA）+ TAEHV（3min/2s、17min/9s 两窗）
- 关键：`--tae taew2_1.safetensors`（Metal 快路，真 VAE 只能 CPU 且慢 2.5×）；单窗上限 81帧/5s（4n+1），分窗切点选镜头切换处；GGUF 只认 calcuis v2 系列（其余缺 vace 张量）；输出 MJPEG-AVI 需转 H.264
- depth 控制已除名（会把源人物身形/服装轮廓锁进生成，换角色着必污染）
- 样片：`~/tool/sd.cpp-test/test2/`（n9_final.mp4 = 9s 全片）；详见 memory `project_vace_motion.md`

## Uncensored Text Encoder(2026-09-07 已装)

- `flux-klein-9b/text_encoder/` 已替换为 [ponpoke/flux2-klein-9b-uncensored-text-encoder](https://huggingface.co/ponpoke/flux2-klein-9b-uncensored-text-encoder)
  (ablated Qwen3-8B,BF16,含 index;原版备份在同目录 `text_encoder.orig/`)
- **效果**:角色参考图的服装/发型锚定显著增强——无造型锁 prompt 也能跟随参考图
  (实测:冰法师白蓝长裙不再漂成裤子);无害提示词余弦 0.97,一般能力无损
- 原理:审查在文字编码器(概念 embedding 打折),DiT 无拒绝回路;ablation 移除拒绝方向
- 回滚:`rm -rf text_encoder && mv text_encoder.orig text_encoder`

## 8. 生圖 — Viggle v0.3 主力 + Krea2 v1.1 第二引擎（2026-10-02 投產）

- `server/workers/qwen_image.py` → sd.cpp Metal（`~/tool/sd.cpp`，build/bin/sd-cli）
- **兩條引擎共用一個 worker**，`ENGINES` 表驅動；選誰 = `engine` 參數 > `QWEN_IMAGE_ENGINE` env > `uncensored`（→ Krea2）
- `POST /v1/images/generations` + `/v1/images/edits`：model 名含 `qwen-image` **或 `krea2` 即路由**；尺寸 32 倍數 ≤1536
- ⚠️ HF 大文件 `curl -C -` 續傳經代理會重疊追加損壞——原子下載（.part+尺寸校驗+mv），腳本 `models/dl2.py`

| | Viggle v0.3（主力） | Krea2 v1.1（第二引擎） |
|---|---|---|
| 底模 | `Qwen-Image-2.1-viggle-turbo-v0.3-6step-Q4_K_M.gguf` 4.0G | `Krea2_turbo_uncensored_v1.1-Q6_K.gguf` 9.8G |
| VAE | `qwen_image_2.1_vae_bf16` | `wan_2.1_vae` + **`--tae taew2_1`**（TAEHV） |
| 文本編碼器 | `Qwen3VL-8B-Instruct-Q4_K_M` | `Qwen3VL-4B-Instruct-Q4_K_M` |
| 步 / cfg | 6 步 cfg1.0 | **8 步** cfg1.0（4 步是蒸餾設計點，+60~82% 牆鐘換品質） |
| 特有旗標 | `--scheduler discrete --sigmas 1,0.9375,0.875,0.75,0.5,0.25,0` | `--diffusion-fa` |
| 強項 | 中文文字、構圖全能 | 西方寫實質感、直白構圖 |
| 參考圖 | ✅ `-r` + `mmproj-Qwen3VL-8B-Instruct-F16` | ✅ `-r` + **`mmproj-Qwen3VL-4B-Instruct-F16`**（2026-10-02 實測通過，**視覺塔必須同尺寸**，見 §14） |

- ⚠️ **Krea2 是第二個引擎，VAE 和編碼器都跟 Qwen 系不同**（wan VAE + TAEHV + 4B vs qwen VAE + 8B），**不是換個 diffusion 檔**
- ⚠️ **Viggle 的 `--sigmas` 必須 7 個值、尾巴補 0**。HF model card 寫 6 個那是 diffusers 格式，**照抄 6 個進 sd-cli 產出整片鹽胡椒噪點**
- ⚠️ **`warmup` 留預設 0**。舊的 `warmup=4` 是照 20 步檔定的，在 4/6 步引擎上前 4 步強制計算 = 快取根本沒開
- **`--tae` 是 Krea2 的 2 倍提速來源**：wan VAE 真 decode 84s（占 207s 的 41%），TAEHV 打到接近 0。Qwen 系不需要（decode 本來就 ~1s）
- 兩條都是 DMD 蒸餾，cfg 固定 1.0，步數與 sigma 是燒錄進去的配對，不是可調參數

## 9. GGUF 量化選型基準 + 提速配方（2026-10-02 實測，M5 Pro）

四版同 prompt/seed 42/1024² 對照，sd.cpp 168f7b8：

| 模型 | 配方 | s/图 | 中文文字 |
|---|---|---|---|
| `qwen_image_2.1_turbo_Q4_K_M` 4.2G | 4步 discrete cfg1 | **50** | ★★★ 4/4 全對 |
| `Krea2_turbo_uncensored_v1.1-Q6_K` 10.5G | 8步 cfg1 | 214 | ★★ 4/4 |
| `Krea-2-Turbo-Q8_0` 13.6G | 8步 cfg1 | **100**（+tae） | ★ 第3字風/鳳難辨 |
| `qwen_image_2.1-Q8_0` 7.7G | 20步 cfg6 | 457 | — |

- ⚠️ **已證偽：step time 近線性於權重字節**。實測反例——qwen base `Q4_K` 4.20G **20.9s/it** vs `Q8_0` 7.69G **21.1s/it**（檔案大 83%、耗時差 1%）。原推論把步數混進了帶寬效應。**真正決定 s/it 的是 cfg**：cfg>1 跑 cond+uncond 兩次前向（2×）。**量化在本機免費，純畫質/磁碟取捨**——要快就降 cfg，不是降量化
- **提速（已 A/B，勝者加粗）**：Krea2 加 `--tae taew2_1.safetensors` **207→100s（2.07×）**——wan VAE decode 84s→~0，質量幾乎無損；qwen 加 `--fa` **54→50s**（不帶時 qwen2.1 prefix cache 退 FP32 K/V）
- **保持現狀（去掉反而慢）**：`--offload-to-cpu`（53 vs 50s）、`--scheduler discrete`（模型默認 FLUX_SCHEDULER 91s）
- **別用**：`--max-vram 32`（50→85s）
- ⚠️ **`--cache-mode dbcache --cache-option threshold=0.1` 已評估完畢，生產選擇「關」**（2026-10-02，見 §11 的 512² 鬼影）。以下參數知識保留供日後重開：`threshold` **必須自己帶**——不帶時走 sd.cpp 預設 warmup=8，在 4~8 步蒸餾模型上前 8 步強制計算 = 快取完全沒開（log 會寫 skipped 0 步）；`warmup` 保持預設 0，不要加 `warmup=4` 之類的值。省時是真的（牆鐘 71→63s = **-9.1%**），但買不到 512² 的乾淨
- **槓桿在 VAE decode 不在 DiT**——qwen 的 VAE decode 本來就 ~1s，同一 `--tae` 對 qwen 無收益
- 上游無解：sd.cpp 21 種 sampler 無新增（"SPEED" PR #1761 close 未合併）、無 Metal 專屬 FA 缺口、無 Q6_K vs Q8_0 畫質數據；未修 Metal bug #1030
- 產物 `~/tool/sd.cpp-models/bench4/`（12 png + times.txt + flag A/B 日誌）

## 10. 全機生圖模型實測總表（2026-10-02，7/7 覆蓋，1024²，每型 normal+nsfw 各一次）

| 模型 | 大小 | 配方 | 步 | s/it | normal | nsfw | 審查 |
|---|---|---|---|---|---|---|---|
| `qwen_image_2.1_turbo_Q4_K_M` | 3.9G | discrete cfg1 | 4 | 9.7 | 53s | 51s | 零降級 |
| **`Qwen-Image-2.1-viggle-turbo-v0.3-6step`** | 4.0G | discrete cfg1 + 7 sigma | 6 | 8.7 | **71s** | **72s** | 零降級 |
| `Krea-2-Turbo-Q8_0`（+tae） | 12.7G | cfg1 --tae --diffusion-fa | 4 | 13 | 66s | 64s | 零降級 |
| `Krea-2-Turbo-Q8_0`（+tae，官方 8 步） | 12.7G | 同上 | 8 | 12.5 | 100s | 108s | 零降級 |
| `Krea2_turbo_uncensored_v1.1-Q6_K` | 9.8G | 8步 cfg1（無 tae） | 8 | 27 | 120s | 115s | 零降級 |
| `qwen-image-2.1-UC-Q4_K_M`（生產） | 4.3G | discrete cfg6 | 20 | 20.4 | 407s | 402s | 零降級 |
| `qwen_image_2.1-Q4_K`（生產） | 3.9G | discrete cfg6 | 20 | 20.9 | 431s | 431s | 零降級 |
| `qwen_image_2.1-Q8_0` | 7.2G | discrete cfg6 | 20 | 21.1 | 436s | 428s | 零降級 |

- **Krea2 走 8 步**（2026-10-02 定案）。4 步不是「省時間但差不多」，是**實質崩壞**：放大裁切後 4 步的副標「精選古籍」被畫成直排放在招牌左側（prompt 要求在**下方**）、末字「籍」糊成一團；多人場景紅衣男的拳頭是分不開指頭的團塊、握筷那手手指拉長糊掉。8 步四字全清、橫排在底部、指節可數，皮膚也才有 prompt 寫明的 `natural skin texture` + 顆粒。代價 61/68/63s → **111/111/101s（+60~82%）**。cn/multi/nsfw 三組結論一致。⚠️ 每格 n=1 seed=42；4 步的手部崩壞**可能**是該 seed 特有，要更硬的證據得再跑 2-3 個 seed
- **Viggle v0.3 6-step 是最優質/秒比**：4 步 turbo 50s 與之同級速度，但 viggle 出圖細節與文字明顯更乾淨；sigmas `1,0.9375,0.875,0.75,0.5,0.25,0`（6 步要 7 個 sigma，sd.cpp 會自動糾正步數）
- **審查：7 版全部零降級直出，含兩個官方版**（qwen base Q8_0 20 步、Krea2 官方 Q8_0 都是）。官方 Krea2 走藝術化構圖（腿手遮擋），uncensored 版完全正面無遮擋——差異是「含蓄 vs 直白」不是「拒絕 vs 產出」。樣本單一（1 prompt/1 體型/1 族裔），不足以下「審查完全繞過」結論
- 產物 `~/tool/sd.cpp-models/nc/`（times.txt + 全部 png/log），下載腳本 `untested.sh` / `viggle03.sh` / `nc.sh`

## 11. Step-Cache 實測（2026-10-02，1024²/seed42，採樣時間不含加載）

> ⚠️ **本節全部 1024²。dbcache 在 1024² 目檢乾淨，但在 512² 實測明顯鬼影**（見本節末「512² 鬼影」）。生產已改 nocache。

| 配置 | Viggle 6步 | Krea2 8步 | 品質 |
|---|---|---|---|
| baseline | 67.1s | 118.4s | — |
| `--fa` | 64.1s | — | ✅ |
| `--cache-mode easycache threshold=0.1` | 64.4s | — | ✅ 但 log 明寫「skipped 0 步」 |
| **`--cache-mode dbcache threshold=0.1`** | **54.2s（跳 1/6）** | **94.6s（跳 1/8）** | ✅ **目檢無瑕，-19%/-20%（同 session 採樣時間）** |
| `dbcache threshold=0.2` | 44.3s（跳 2/6） | 97.3s（跳 2/8） | ❌ **鬼影重曝，報廢** |
| `dbcache threshold=0.25` | — | 76.8s（跳 3/8） | ❌ 未目檢，勿用 |
| `--scm-mask ... --scm-policy static` | 65.3s | 95.3s | ❌ **空操作**，見下 |

- **`--scm-mask` 是空操作**：log 顯示帶了 mask 仍啟動預設 `CacheDIT mode=DBCache+TaylorSeer Fn:8 warmup:8`，SCM 模式從未進入。觀察到的加速全部來自 DBCache 啟發式，不要歸因給 mask
- **根因（上游 `src/runtime/cache_dit.hpp:19-20`）**：`residual_diff_threshold=0.08`、`max_warmup_steps=8`。6~8 步的蒸餾模型**預設參數**下結構性用不了快取
- **❌ taylorseer 已實測，是死的**（原以為 `max_warmup_steps=2` 預設夠小，值得試——試完了，不行）。三重證據：
  1. 預設下 log 印 `CacheDIT completed without skipping steps`：Viggle 71→**73s**、Krea2 63→**62s**，等於沒開
  2. `cache_dit.hpp:626` 的 `parse_taylorseer_options` 收**位置參數** `"n_deriv,warmup,interval"`，但 `common.cpp:2365` 的 `parse_named_params` 只認 `key=value`，位置三元組會被擋在 `error: cache option '1' missing '=' separator`——**該函式從 CLI 路徑永遠呼叫不到**
  3. 繞過用 CLI 合法鍵 `--cache-option warmup=1`，log 確認 `warmup: 1` 生效，**仍然 0 跳步 / 70s**
- **⇒ 結論：`--cache-mode dbcache --cache-option threshold=0.1` 是技術上唯一可用的快取線**
- **Viggle sigma 必須 7 個值**：`--sigmas 1,0.9375,0.875,0.75,0.5,0.25,0` ✅。HF model card 寫的 6 值是 diffusers 格式（無 terminal），**照抄進 sd-cli 產出整片鹽胡椒噪點**（實測 `cache_ab/viggle_sigma6card.png`）
- `--taesd` 走 SD1.5 latent 格式（`madebyollin/taesd`），**對 Krea2/wan 不通用**，只有 `--tae`（TAEHV）能用
- **上游獨立佐證量化不提速**：`byteshape/Krea-2-Turbo-GGUF` model card 原文「diffusion inference is not heavily constrained by memory bandwidth... Quantization here buys you VRAM headroom, not throughput」——與本機 Q4_K vs Q8_0 實測一致
- 產物 `~/tool/sd.cpp-models/cache_ab/`（11 png + log），腳本 `cache_ab.sh` / `th_ab.sh`
- ⚠️ **上面那個 -19%/-20% 是同一 session 內的「採樣時間」，不能當成牆鐘效益**。跨 session 的牆鐘對照是 71s → **63s = -9.1%**（1024²）。**雜訊地板 6%**（見 §12），所以真實生產收益接近 -9%，-19% 是 session 內對照的樂觀值。§13 的解析度分級表用牆鐘數字

### 11.1 512² 鬼影 —— 生產改 nocache 的原因（2026-10-02）

- **現象**：Viggle v0.3 6 步 + dbcache(th=0.1) 在 **512²** 產出明顯鬼影——左側半透明重複人形 + 主體周圍 2~3 張幽靈臉。同一配方在 1024²（`cache_ab/viggle_dbcache.png`、`cutover/viggle_1024_cache.png`）目檢乾淨
- **裁決：生產關閉 dbcache，改 nocache**。理由是產量分布不是平均畫質：短劇草稿檔就是 512x288/512²，髒的正好是跑得最多那一級。代價 1024² **+8s**（63s → 71s），1024² 本身畫質無差
- **實作**：`qwen_image.py` 的 `DBCACHE` 預設翻成 opt-in（`QWEN_IMAGE_DBCACHE=1` 才開），`DBCACHE_OPTION` 留著不刪
- ⚠️ **開關有兩處，plist 那處才是生產真開關**：`~/Library/LaunchAgents/com.aifilm.gateway.plist` 原本硬寫 `QWEN_IMAGE_DBCACHE=1`，會蓋掉程式碼預設——**只翻程式碼在生產完全不生效**。已把該 key 從 plist 移除（`scripts/deploy_config.py` 本來也不產它，現在 plist 與 generator 一致）。同理移除死設定 `QWEN_IMAGE_TURBO`
- ⚠️ `image_upscale.py` 共用同一個 `DBCACHE` 常數，**超分也一起變 nocache**——這是刻意的（避免兩處各抄一份改漏）， upscale 的 nocache 畫質另行確認

## 12. MacBook Pro (M5 Pro 48GB) 專項（2026-10-02）

硬體 = **MacBook Pro M5 Pro 48GB（Mac17,9）**，不是桌機。Viggle 6 步 1024²：

| 配置 | 牆鐘 | 採樣 | 第1步 | 加載 |
|---|---|---|---|---|
| baseline | 77s | 63.2s | 14.3s | 5.1s |
| `--eager-load` | 81s | 62.9s | **10.5s** | 9.7s |
| `--disable-prefetch` | 79s | 65.9s | 14.3s | 5.1s |
| `--mmap` | 84s | 68.3s | 16.5s | **0.2s** |

- **散熱漂移是真的**：同設定連跑三次 63.15 → 63.93 → **65.56s（+3.8%）**。批次作業要嘛插冷卻要嘛接受這個 compounding
- **`--eager-load` 反而慢**：第 1 步 14.3→10.5s，但加載 5.1→9.7s，淨多 4s。工作只是從第 1 步搬到 load 階段
- **`--mmap` 別用**（統一記憶體下是負收益）：加載 5.1→0.2s（25×），但採樣 +8.1%。權重留在磁碟頁面上，compute 期間 page fault 拖慢
- **`--disable-prefetch` 別用**：+4.4%。async weight prefetch 在預設開著且有效
- **`--disable-segmented-compute` 不用測**：graph cut 規劃每圖只跑 4 次 × 1-3ms，總計 ~10ms，無意義
- **雜訊地板 ≈ 6%**：同一配置在不同 session 量到 67.1s 與 63.2s。**低於 6% 的差異都是雜訊**——dbcache th=0.1 的真實牆鐘收益是 **-9.1%**（71→63s），session 內量到的 -19% 是樂觀值；散熱漂移（3 次單調上升）也是真的。（dbcache 本身已在生產關閉，見 §11.1；這裡留著當**量測方法論**的紀錄）
- 48GB 對兩個模型都寬鬆（Viggle 峰值 ~9.1GB、Krea2 ~13GB）。**小筆電上量化才有意義，而且意義是「塞得下」不是「跑得快」**（與上游 model card 一致）
- 產物 `~/tool/sd.cpp-models/macbook_ab/`，腳本 `macbook_ab.sh`

### 12.1 效能天花板：模型端已到頂，管道端還有 18~40%（2026-10-02）

**採樣本身沒有設定可調了。** 每個槓桿都到底或已證偽：cfg 1.0（已砍掉 uncond 那半）、6/8 步 = 蒸餾設計點、flash attention 已開、dbcache 只 -9.1% 且 512² 鬼影、taylorseer 死路、併發負報酬、`--batch-count` 是循序迴圈。

**量化在這台機器上是免費的——所以降精度換不到速度。** 這條最容易誤判：viggle 1024² 是 **9.16s/步**（54.96s ÷ 6），而**載入全部權重只要 4.36s**（`model_loader.cpp:1380`）。一步的算力成本是整份權重載入的 **2.1 倍** → 權重不是瓶頸，Q4 降到 Q3/Q2 只會掉畫質不會變快。瓶頸是 DiT forward pass 的算力強度，沒有開關。

**沒吃到的是進程啟動。** 牆鐘減採樣 = 每個 job 重載模型：

| 解析度 | 牆鐘 | 採樣 | 啟動 | 占比 |
|---|---|---|---|---|
| 288×512 | 17s | 10.13s | 6.87s | **40%** |
| 512×288 | 23s | 15.49s | 7.51s | **33%** |
| 1024² | 67s | 54.96s | 12.04s | 18% |

**越小的解析度浪費比例越高**——草稿級本來就沒多少東西要算，結果三分之一時間在讀磁碟。唯一能吃到的是 **sd-server 常駐**（模型留記憶體，不每次 fork）。這是架構改動不是設定，難點在三條引擎的 DiT/VAE/LLM 全不同、切換成本可能吃掉收益；但 production 批量本來就同引擎同解析度（24 格分鏡表），攤得起來。

## 13. 生圖解析度分級（短劇，M5 Pro 48GB）

Viggle v0.3 6 步 + dbcache 的**牆鐘**實測（非採樣時間）。
⚠️ **這批數字是帶 dbcache 量到的，生產現已改 nocache**（§11.1）——純 nocache 牆鐘未重測，別當現況引用：

| 解析度 | 牆鐘 | 用途 |
|---|---|---|
| 512×288 / 288×512 | 17s | **草稿** |
| 512×512 | 15s | 定格/測試 |
| 1024×576 / 576×1024 | **38s** | **正片** |
| 1024×1024 | 63s | 方畫幅，非 16:9 |
| 1920×1080 / 1080×1920 | — | **走 FlashVSR，不要生** |

- **固定成本約 9s 主導**：512² 只比 512×288 快 2s，512 以下再降解析度省不到時間
- **32 網格上精確 16:9 只有 `512k×288k` 一族**（由 `9a=16b` 推導），9:16 是同一族的轉置。1024×576 是它的 2 倍而非 16:9 的最近鄰
- `compat_openai.py` 的 1536 上限對草稿/正片兩級都放行，**不用改**；但 1080×1920（長邊 1920）會被擋，所以那一級本來就該走 FlashVSR

## 14. 生圖負結果（2026-10-02，四個都是「別再試了」）

- **併發 2 個 sd-cli = 負報酬**：1024² **0.97×**、512² **0.81×**，峰值 RSS 22.94GB / 48GB。瓶頸是**算力不是記憶體**（22.9GB 用掉不到一半，時間卻變慢）。`qwen_image.py` 的 `_run_lock` + `MEM_GB=26` 是對的，別改成併發
- **`--cache-mode taylorseer` = 死的**（已實測，三重證據見 §11）：預設 0 跳步（Viggle 71→73s、Krea2 63→62s）；CLI 只認 `key=value`（`common.cpp:2365`）而 `parse_taylorseer_options`（`cache_dit.hpp:626`）收位置三元組，**從 CLI 永遠呼叫不到**；繞過用 `warmup=1` 生效後仍 0 跳步。**dbcache `threshold=0.1` 是技術上唯一可用的快取線，但生產已因 512² 鬼影關閉（§11.1）**
- **`--batch-count` = 循序迴圈不是真 batch**（`src/pipeline/image.cpp:842-846`）：text encode 在迴圈**外**，每張各一次 `sd->sample()`，實際只省 N-1 次模型載入。**同提示詞 n 張變體有意義，影策的分鏡批次（不同提示詞）沒有意義**
- **視覺塔 hidden size 對不上 = 靜默產錯圖，不是報錯**：`llm.hpp:386` 只印一行 ERROR 就把 vision disable 掉，sd-cli **rc=0 照樣存檔成功**，但輸出完全無視 `-r` 參考圖。實測 Krea2(4B) + `mmproj-8B`：`vision projector output size (4096) does not match LLM hidden size (2560)` → 臥床裸女輸入產出**穿紅衣棚拍**，參考圖沒生效還附贈審查降級。`conditioner.hpp:2557` 那條保護在 Krea2 上**不觸發**（它不是 qwen_image_2.1 editing 架構）。唯一防線是 `ENGINES["krea2"]["refs"] = False` 在程式碼層擋掉——**要開 Krea2 編輯必須先下 4B 的 mmproj**，別圖省事指 8B 那顆

## 15. 模型盤點清理（2026-10-02）

舊 qwen 系底模退役，**刪除 14.0GB**：UC `Q4_K_M` 4.3G + heretic TE 4.7G + heretic mmproj 1.1G + 非 turbo fallback `Q4_K` 3.9G。

- **`image_upscale.py` 收成單一 turbo 路徑**。舊 docstring「NSFW 行必須走 UC base（官方/turbo 會把衣服畫回去）」**已被實測推翻**——生產設定 strength 0.35 / 4 步對 NSFW 輸入完整保留無衣狀態（`view/up_A_turbo.png`）。`params["uncensored"]` 保留但忽略（不動 compat_openai 介面合約）
- **⚠️ NSFW upscale 的預設參數變了，是取捨不是 bug**。移除預設分流後，`uncensored=True` 從 `strength 0.3 / 12 步` 變成 **`0.35 / 4 步`**：時間 **66.76s → 38.17s（-43%）**，但**畫質降一級**（目檢 `up_B` 比 `up_A` 銳）。**依賴舊品質的呼叫端要自己傳 `strength` / `steps`**。`uncensored=False` 的呼叫端完全不受影響
- ⚠️ **strength >0.5 未測**：超過就不是放大語義，會把參考圖重畫掉
- `mmproj-Qwen3VL-8B-Instruct-F16.gguf` **保留**——是 Viggle 參考圖路徑的視覺塔，仍在用
- **`qwen_image_2.1_turbo_Q4_K_M.gguf`（3.9G）保留，不是冗餘**。它是 `image_upscale.py` 唯一的 DiT。A/B 過「能不能讓 Viggle 接手」：同參數（1024²/strength 0.35/seed 42/UltraSharp）turbo 4 步 **20.94s** 乾淨忠實 vs Viggle 6 步 **31.66s（+51%）** 且**把 Viggle 的底片顆粒灌進全圖**（手臂/床單變數位雜訊、中腹糊掉）。超分的目的是增細節不是換風格，顆粒正好抵消它。**turbo 是中性精修器、Viggle 是風格化生成器，不是同一件事的兩個版本，別為了統一引擎而合併**
- `models/` 現 28G。磁碟 88%（111GiB free）

## 16. MLX 原生路線評估（2026-10-02，未操作，純查證）

**結論：對目前這兩個模型，MLX 輸。**

- 現成可用：`ddalcu/mlx-serve`（`POST /v1/images/generations` + `/v1/images/edits`，brew 安裝，OpenAI 相容 → Gateway 換 base URL 即可）、`mflux-community/mflux`（MIT 2430★，**只有 library 無 server**）
- **致命阻礙一：沒有 viggle 6 步蒸餾 checkpoint 的 MLX 版**。所有 MLX Qwen-2.1 pack 都源自 **40 步 base**。M5 Pro 約 1.8-2.2 s/step → 40 步 **85-100s**，比 viggle 6 步 + dbcache 的 63s **更慢**。vanch007 那個宣傳 `--lora-preset viggle-turbo --steps 6` 的 README，**程式碼裡查無此 preset**
- **致命阻礙二：沒有 MLX 版 Krea2 uncensored**。全部 MLX Krea 都源自 stock `krea/Krea-2-Turbo`；`avlp12/*` 更內建 `krea2/safety.py` **預設開啟**（`KREA2_DISABLE_SAFETY=1` 才能關）
- 無任何 MLX-vs-ggml 同權重同步數的對比發表；MLX pack 反而大 2-3 倍（8.9-11.2GB vs 4.04GB）
- sd.cpp 繼續用。**若日後換模型，mlx-serve 是最省事的第二後端**（OpenAI 相容、可熱插）

## 17. `MEM_GB` 是准入門檻，不是效能旋鈕（2026-10-02）

`core.py:250-253` 只做一件事：

```python
inc = worker.MEM_GB
if running_gb + inc <= BUDGET_GB:
    return worker, _job_row(row)
break   # 放不進去就 FIFO 頭阻塞
```

**它決定要不要放行，不決定跑多快。** `qwen_image.MEM_GB = 26.0` 配合 `qwen_image.py` 的 `_run_lock`（sd-cli 嚴格序列化）：

- **調大只會更慢（或造假帳）**。放行更多 job → 全部堵在鎖上 → 還是一個一個跑；期間 `started_at` 在拿到鎖之前就寫入，**鎖等待被算成工時** → 就是「超預算 4 倍」的假象。拆掉鎖也沒用，併發本身實測負報酬
- **記憶體從來不是瓶頸**：2 個併發峰值 RSS 22.94GB / 48GB，只用掉不到一半，時間卻變慢
- **26.0 不要動**。它是實測峰值（8 ref 編輯 22~27GB），調高等於對排程器說謊

**它唯一真實的作用是混合負載排擠**：`BUDGET_GB=36`，一個 music（15GB）＋ 生圖（26GB）= 41 > 36，一個音樂 job 就能擋掉全部生圖。但那是「過度保守造成假拒絕」，解法不是調大，而是把預算調高或讓不同引擎的 `MEM_GB` 更貼近實測。

⚠️ `MEM_GB = 26.0` **從沒在 Krea2 + refs 下實測過**——它是照 qwen 系（8B LLM + 8B mmproj）定的。Krea2 用 4B + 4B mmproj 兩項都更小，峰值應該更低，但那是推論。上線前實測一次再決定要不要下調。
