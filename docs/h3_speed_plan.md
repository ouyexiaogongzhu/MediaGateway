# h3.c M5 Pro 48GB 速度优化方案（结合上游 antirez/h3.c 现状修订）

> 2026-09-05 · 基于《h3c_M5Pro_48GB_Speed_Optimization.md》+ 上游 main(8974cc0) 实测核对。
> 本地 `~/tool/h3.c` 与 origin/main **零落后**；原方案中「Phase 2 增加 token_reduction_strength」上游同样没有——是真代码改造点。

## 0. 原方案没覆盖、但上游已有的关键事实

1. **Internal canvas（render_width/render_height）**：DiT/VAE 内部小画布 + vImage 放大。
   上游验证过的缩放点：384→512（快速质量档）、320→512（激进档）。
   **参数已在 bridge 里（h3_params），worker 已于本次透传**——这是文档之外的最大独立杠杆。
2. **上游"validated balanced preset"是 steps 20 / layers 45 / reuse 2**（11 次 fresh DiT）。
   而 MediaGateway 当前默认是 **steps 6 / reuse 1（6 次评估）+ layers 45**——denoise 次数比文档首选还少。
   → benchmark 矩阵必须把「我们现有默认」作为候选，而不是假设文档配置就是基线。
3. `--reuse` 与 `--core-reuse` 互斥；`--use-int8-row-fc2` 与 `--ssd-streaming` 互斥；benchmark 关 `--show`。
4. token_reduction 仍是 `int`（ON/OFF）——可调强度确属上游未做的真改造。

## 1. 现状基线（docs/tools.md + E2E 实测）

| 项 | 值 |
|---|---|
| worker 默认 | steps 6 / layers 45 / reuse 1 / 无 token / 无 int8 / 无 internal canvas |
| 实测（864×480, 5s=120f, 6 步） | ~100-210s（含引擎加载，页面缓存热时 ~100s） |
| 影策两段式 | 草稿=同配置再 512×288；成片=864×480 |

## 2. Profile 设计（Gateway video worker + 影策两段式映射）

| Profile | steps | layers | reuse/core | token | int8 | internal canvas | 用途 |
|---|---:|---:|---|---|---|---|---|
| `draft` | 6 | 45 | reuse 1 | ON | ON | 0.625× 输出 | 影策「生成草稿」，几十秒 |
| `standard`（现默认） | 6 | 45 | reuse 1 | OFF | OFF | 无 | 快速正式 |
| `quality`（待 P0 验证） | 20 | 45 | reuse 2 | ON | ON | 无 | 文档目标配置 |
| `reference` | 20 | 50 | reuse 1 | OFF | OFF | 无 | 质量基准（仅 benchmark 用） |

worker 新透传参数：`use_int8_row_fc2`、`render_width/render_height`（32 取整）。影策侧后续把 profile 映射进 render 端点。

## 3. P0 Benchmark 矩阵（固定 prompt+seed，864×480 / 120f / 5s）

| # | 配置 | 目的 |
|---|---|---|
| R | 20/50/reuse1 | 质量基准 100% |
| A | 20/45/reuse2 | 文档首选 |
| B | A + token | token 收益（上游 24.5% 参考） |
| C | B + int8 | 叠加收益（~2.6% 参考） |
| D | 20/45/core-reuse4 | reuse 替代路线（二选一对照） |
| E | **6/45/reuse1（现默认）** | 我们的事实基线 |
| F | E + internal 0.625×（544×320→864×480） | 新杠杆 |
| G | F + token + int8 | 激进草稿上限 |

度量：
- 时间：job created→finished（含加载）+ progress 阶段耗时（denoise 段单独看）
- 质量：`ffmpeg -filter_complex ssim` 逐帧对 R；composition/主体数/脸手 **人眼 checklist**（SSIM 对构图漂移不敏感，token reduction 恰好主要伤构图——SSIM ≥0.95 且人眼过 checklist 才算 ≥95%）
- 内存：`footprint`（ps/`/usr/bin/time -l`）

产出写回 `docs/tools.md`，据此选 standard/quality 的最终参数与 internal canvas 缩放点。

## 4. P1：上游贡献（真代码改造）

1. `float token_reduction_strength`（0/0.25/0.5/0.75/1.0）——`h3_dit.c` 合并阈值暴露为参数，benchmark 定档
2. **middle-blocks-only reduction**：早期/后期 block 恢复 full tokens（保构图/脸/手），中期压缩——「≤5% 损失」最优先方向
3. 两者都以 PR 形式回馈上游 antirez/h3.c

## 5. 禁忌（沿用原方案 §13）

不改权重/VAE/DiT 架构；不 reuse3+layers40+token 三连开；reuse 与 core-reuse 互斥；int8 与 ssd 互斥；不做 SSD streaming 追速度；benchmark 关 `--show`；2K 走「低分辨率 latent + tiled VAE decode」独立项目，不动 Native DiT。

## 7. P0 结果（2026-09-05 实测，864×480/120f/5s/seed42）

| # | 配置 | 耗时 | vs R | SSIM* |
|---|---|---:|---:|---:|
| R | 20/50/reuse1 | 1291s | 1× | 1.0 |
| A | 20/45/reuse2 | 640s | 2.0× | 0.63 |
| B | A+token | 370s | 3.5× | 0.62 |
| C | B+int8 | 370s(+0%) | 3.5× | 0.61 |
| D | 20/45/core4+token | **280s** | **4.6×** | 0.64 |
| E | 6/45/reuse1（原默认） | 340s | 3.8× | 0.67 |
| F | E+internal 576×320 | 120s | 10.8× | 0.61 |
| G | F+token+int8 | **110s** | **11.7×** | 0.58 |

结论：
- **int8 在 M5 Pro 零收益**（与上游 M5 Max +2.6% 不符），profile 已剔除
- **core-reuse4+token（D）= quality 档**（4.6×）；**internal+token（G）= draft 档**（11.7×）
- *SSIM 局限：跨去噪路径的逐帧 SSIM 天然 0.6 档（生成内容本就不同），不能作 ≥95% 门槛——
  质量验收以人眼构图/主体/运动 checklist 为准（R vs D/G 样片在 /tmp/h3bench/assets/）

## 8. 执行顺序（历史）

```
P0  benchmark 矩阵（R/A/B/C/D/E/F/G，固定 seed，ffmpeg ssim + 人眼 checklist）→ 定 standard/quality
P0  profile 接进影策（草稿=draft、成片=standard 或 quality）
P1  token_reduction_strength + middle-blocks-only（上游 PR）→ 再 benchmark
P2  Metal kernel fusion / buffer 复用（视 P0 瓶颈再定）
```

目标：质量 ≥95%（SSIM+人眼双门槛），速度相对 reference 提升 25-40%（目标值，非承诺）。

## 9. T4：core_reuse 在生产解析度下的结论（2026-10-02 实测，544×960/f362/steps6/dit_layers45）

### 9.1 像素结论优先：core_reuse=4 在生产解析度毁画面

544×960 下 `core_reuse=4` 与 `core_reuse=1` 的画质关系**与草稿解析度完全相反**。每 arm 抽 9 张原生 544×960 帧（f0/45/…/361）逐张比对，全片均匀：

- **core_reuse=1（生产现状）= 好**：戴口罩的大衣人物、手提包、湿地面反光、霓虹招牌可读、建筑边缘干净、曝光正确。
- **core_reuse=4 = 糊块**：人物完全无法辨认，招牌全是马赛克块面，全帧抖动纹理。

**这不是末帧衰变也不是取帧位置的错觉**——f0 与 f180 同样糊。跨越点被夹在 **288×512（core4 好）与 544×960（core4 坏）之间**，未做更细定位（该点不改变决策：core4 在生产解析度已出局）。

### 9.2 速度：41% 端到端，不是 50%

两 arm 各自**独立进程 + 丢弃式 warmup**（同 prompt/seed/全部参数），prepared-DiT cache 命中已确认。ABBA 交错不可用（见 §10）。

| | core_reuse=1（生产） | core_reuse=4 | 比值 |
|---|---:|---:|---:|
| `wait`/frame | 5.1863 s | **2.5661 s** | 0.495 |
| `direct` | 2256 | 1158 | 0.513 |
| `attn` | 270 | 135 | 0.500 |
| DiT load | 21.343 s | 22.156 s | 持平（均命中缓存） |
| **端到端 DONE** | 2324.50 s | **1371.57 s** | **0.590** |
| h3 peak | 24.681 GiB | 25.798 GiB | +4.5% |
| mp4 字节 | 4,070,578 | 7,277,227 | **1.79×（更差却更大）** |

**DiT 赚 50.5%，墙钟只赚 41%**——差额是 core_reuse 碰不到的固定 VAE 税。对外只应引用 41%。

### 9.3 VAE 已被证无罪（这是让结论成立的对照组）

video VAE decoder 的 `wait` = 774.162(core1) vs 759.186(core4)、`encode` = 106.209 vs 104.946——**相差 1.9% / 1.2%，对 core_reuse 完全免疫**。同一条 VAE 路径、同样成本、相反画质 ⇒ **损伤发生在 DiT latent，不在解码**。

⚠️⚠️ **上面这两个数字是整进程累计，不是单 job。** 该 resident decoder 的 context 跨 warmup + measured **两个 job** 存活，所以 774.162s ÷ 2 ≈ **387s wait/job**，106.209 ÷ 2 ≈ **53s encode/job**，合计 **~440s/job**。**本文早期版本把 774.162 当成单 job 报出，与本节的 432s 自相矛盾**（h3-bench 发现）。本节的 32%/18% 占比反解出 412–437s，与 ~440s 一致、与 774s 不一致，故 **432–440s 是正确的单 job 值**。

**544×960/f362 的 VAE decode = 每 job ~440s GPU 固定税**（~387s wait + ~53s encode），占 core4 GPU wait 的 32%、core1 的 19%。这是端到端增益落后 DiT 增益的全部原因，也是 core_reuse 出局后下一个该攻的杠杆。

⚠️ **VAE 的 `wall` 不是解码成本**：1803.738 vs 2767.907 那个 1.53× 是假的——它是 resident context 的生命週期，大半时间在陪 DiT 闲置。**只用 `wait` / `encode`。**

### 9.4 五个测量陷阱（本轮各踩一次或各差点踩上）

1. **`h3_gpu_profile_mark` 的 `wall` = 距上一个 mark 的间隔，不是 phase 耗时。** 跨 job 时它会把上一个 job 的尾巴算进来；曾据此误判「432s 卡在 mark 内 = VAE 慢」。
2. **`ps -o rss` 不能当占用读数**：读 0.24GiB 而 h3 真握 ~18GiB（unified memory 上 Metal 配额不进 process RSS）。占用只认 h3 自己的 `peak=` 与 `memory_pressure`。
3. **Laplacian 方差不能当锐利度**：把 core_reuse=4 评为「锐利 2.7 倍」（lap_var 917 vs 339），因为糊块本身就是高频纹理，而该指标奖励一切局部曲率。块格检测在 2/4/8/16/32/64px 全部为阴性 ⇒ 是真糊不是压缩格。**坏 arm 的 mp4 反而大 1.79 倍**（抖动难编码）——**文件大小是反向品质信号，不是细节保真度信号**。
4. **`root-gpu` 不是 GPU 忙碌度（对 MPSGraph 工作负载）**：T4 实测 `root-gpu=7.754s` vs `wait=774.162s`——99% 的“气泡”，会让人得出「GPU 在 168 个 tile 上都处于饥饿」的**自信且完全错误**的结论。`h3_gpu.h:29` 写得很清楚：*"Root MTLCommandBuffer timestamps; MPSGraph may schedule child buffers, so command_wait_seconds is the complete turnaround measurement."* VAE 的每个 kernel 都在 MPSGraph 子 buffer 内，root 时间戳几乎不动。**只用 `wait` / `encode`。**
5. **⚠️ 数值要标范围（whole-process ≠ per-job）**：§9.3 曾在同一节里同时给出 432s 与 774.162s。两者都对，**范围不同**：T4 的 warmup + measured 跑在同一进程、retained decoder context 跨两次 decode 存活，故 774.162 是两次之和，per-job = 387.081s。**同一进程跑两个 job 时，所有 `total` mark 都是整进程的。** 判别方法不依赖任何数字是否转写正确：报告自述的占比反解即可——32%×928.9s⇒437s、18%×1877.4s⇒412s，落在 432–440s，与两个候选都不矛盾地被排除。

### 9.5 结论与待办

- **`core_reuse=4` 不得用于生产解析度**，此前基于草稿解析度的签核作废。
- ✅ **`video.py` 缺省 `core_reuse` 已从 4 改为 1（2026-10-02 修复）**，现为 `profile.get("core_reuse", 1)`（`server/workers/video.py:127`）。**这里曾是一个与本战役无关的既存陷阱，记录它为什么曾经开着**：当时 fallback 写死 4，而 `standard`/`draft` 两个 profile 都没设该 key，于是**毁画质的值正好是任何不显式钉值的调用方的默认值**——一个从未打算测过生产解析度的默认值，静悄悄地决定了产出画质。`gateway.db` 120 个 video job 中 114 显式钉 1、0 钉 4、**6 无此 key（当时吃 4）**。
  ⚠️ **`_PROFILES["quality"]` 里的 `core_reuse: 4` 是显式档位，不是默认值，勿动**；但若有人日后把它挪进任何 fallback 路径，先回来看这一节。另：`core_reuse=4` 还被 `compat_h3cweb.py:60,62` 归一为 1，要真开需两处同改。
- 未决：VAE 的草稿基线（`wall=125.216 wait=89.228`）无可查日志、帧数未知，故 ~432s/job 是回归还是优于线性**悬空**，待有基线时再判。

### 9.6 事前預測 vs T4 實測 log（2026-10-02，h3-bench）

下表四項**在讀 T4 log 之前**就由 CPU 側算好並寫下，事後才拿去對。對照來源：`/tmp/h3bench/t4_c1/close.log`。**對得上才說明模型是對的；「我量到了所以對」不構成同一種證據。**

| 事前預測 | T4 log 實測 | 結果 |
|---|---|---|
| 21 chunks × 8 tiles = **168** tile-decode/job | `submissions=336 ÷ 2` = **168** | ✅ 精確 |
| 36 layers × 1 SDPA = **36** attention/tile | `12096 ÷ 2 ÷ 168` = **36.0** | ✅ 精確 |
| 36 × 4 GEMM + 1 out-proj = **145** linear/tile | `48720 ÷ 2 ÷ 168` = **145.0** | ✅ 精確 |
| 權重 9.03 GiB + activation ~0.47 = **9.50 GiB** | `peak = 9.503 GiB` | ✅ 相符 |

**讀法注意：**

- `submissions` 與 `attention`（以及 `linear`）都必須 **÷ 2**，因 T4 的 warmup + measured 跑在同一進程、retained decoder context 跨兩次 decode 存活（見 §9.4 陷阱 5）。**直接引用未除的數字就會得到錯誤的 per-tile 結構。**
- **145 而非 147 的原因**：`h3_gpu_linear_f32` 只在 `rows≥32 && input_dim≥256 && output_dim≥256` 時走 MPS matmul。144 個 per-layer GEMM（rows=2532, in=2048, out=6144/2048/16384/8192）全部滿足；兩個 input projection（24→24、24→2048）因 `input_dim<256` 落到自訂 kernel，記在 `direct` 而非 `linear`。**這個差異本身就是自洽性檢查**——若不追進原始碼，145 看起來會像四捨五入誤差。

**為什麼這張表要長這樣：** 第一版 FLOP 比較表裡，`H3_VAE_TILE_PIXELS=288` 那一列我**憑印象填成 2×4**，真值是 **3×4 = 12 tiles**，整列差 50%，而且看不出來。改成把 `configured_tile_pixels()` / `tile_axis_build()` 編譯成可執行檔直接跑（`/tmp/vae_tile_check.c`）之後才抓到。**手推的每一步都「看起來合理」，所以錯誤會安靜活到結論裡；編譯執行沒有中間層可以藏錯。** 這也是本表存在的形式理由——讓讀者看見數字是算出來的，不是抄來的。

### 9.7 VAE decode 的成本結構（CPU 側，step 1 前）

**tile plan（由 `configured_tile_pixels()` 實跑，非手推）：** 544×960 → **304 px，2×4 = 8 tiles**，latent tile 19×19、patches 2527、**seq 2532**；21 chunks × 8 tiles = **168 次 tile-decode**。對照 288×512 → 288 px、1×2 = 2 tiles、seq 2273。**生產解析度是草稿的 4 倍 tile 數**，且重疊浪費從 12.5% 升到 **41.6%**。

**算力分佈（每 tile 14.157 TFLOP）：GEMM 86.4% / SDPA 13.4% / output proj 0.2%。** 全程 **`conv=0`**，145 個 GEMM dispatch + 36 個 SDPA，沒有任何卷積。

⚠️ **bf16 上限是 1.761×，不是 2×；而端到端只有 7.2%，不是 32%。** 這兩層都曾被口頭傳大過。

**第一層（VAE decode 階段內）：** SDPA 那 13.4% 若留在 fp32，即使 GEMM 快 2×，整段只快 `1/(0.864/2 + 0.136) = 1.761×`，387.081s → **220s**，省 167.3s（`encode` 的 53.1s 是 CPU 串行、不受影響）。

**第二層（這才是決策者手上的數字）：** 那 167.3s 要對**哪一個 arm** 兌現，差一倍：

| arm | job 牆鐘 | → | 端到端 |
|---|---:|---:|---:|
| `core_reuse=1`（**生產預設**，5.1863 s/frame） | 2324.5s | 2157.2s | **7.2%**（1.078×） |
| `core_reuse=4`（生產解析度已出局，僅供對照） | 1371.6s | 1204.3s | 12.2%（1.139×） |

因為 VAE 只佔 core1 GPU wait 的 **17.1%**，不是 32%——32% 是 core4 的占比，而 core4 在生產解析度已被證毀畫質。**對外只應引用 7.2%。**

> ⚠️ **這 7.2% 是上限，不是預期值。** 它假設 bf16 能讓 GEMM **整整快 2×**，而**這一點在本機從未被實測過**——repo 內沒有任何 fp32 vs bf16 matmul 的對照數據。實際可能是 2×、1.5×、或更少。**這句話必須一路留到最終簡報**，不要在轉述時被磨掉。

**對照：`encode` 那條線比 7% 小得多。** 53.1s 的 MPS graph 建構若靠 deferred submit 完全重疊，理論上省 53.1s = core1 端到端 **2.3%**（core4 為 3.9%）。**它是 2% 級的槓，不是 7% 級**；且與 precision 完全正交。

**tile 形狀這條槓已死（不是因為 288×512 的舊結論）。** 枚舉 `H3_VAE_TILE_PIXELS` 全部可取值 256–512，`304` 是**整個可接受區間的全域 FLOP 極小**，不只是自動調參器 256–320 視窗內的最好點：1.76:1 的長寬比下，grid 要到 512 才從 8 降到 4 tiles，而 seq 漲到 7173，二次 attention 吃掉全部好處。**這是「幾何上每個選項都更差」，不是「換個 operating point」。** 除非 step 1 顯示 per-tile 固定成本很大（見下），否則不值得重測。

**不需要 roofline 的檢驗法：** 168 個 tile 的 GEMM FLOPs **完全相同**（同樣 304×304、同樣 seq 2532），所以「GEMM-bound」這個假說預測 **per-tile wait 近乎完全一致**；**觀測到的離散度就是非算術成本的直接上界**。
⚠️ 離散度**必須用 CV 與 drift 分開量**：CV 量的是「離均值的散佈」，**看不見單調趨勢**——21 個 chunk 上 5% 的線性漂移只會產生約 1.5% 的 CV。所以 drift 要另外用 **per-chunk 均值的前後三分之一比較**來量。三個正交訊號：**scatter（robust CV = IQR/median，看 bulk 是否算術受限）× drift（per-chunk 趨勢）× tail（p95 之外的少數 tile）**。

⚠️ **這把尺是被「已知答案」驗過的，不是定義出來就算數。** 拿三個 ground truth 已知的合成 run 餵進分析器：

| 合成情境 | robust CV | drift | tail | 判讀 |
|---|---:|---:|---:|---|
| 完全一致 | 1.43% | +0.00% | +0.0s | 算術受限 ✓ |
| 單次 hiccup（第 11 chunk 一格 20.38s） | 1.00% | −0.05% | +2.8s | bulk 乾淨、尾巴另計 ✓ |
| **5% 單調 ramp** | 2.62% | **+3.46%** | +0.1s | **drift 被抓到** ✓ |

**第三列是重點**：那個 ramp 的 CV 只有 1.5%，和乾淨的 run 幾乎一樣——**只報 CV 的版本會判成「算術受限、可以動手」，然後帶著數字送出錯誤結論。** 是 drift 那一欄（+3.46%）把它抓出來的。這也是為什麼 scatter / drift / tail 必須分開報。

⚠️ **repo 內沒有任何實測 matmul 參考值**（已查 AGENTS.md / README.md / docs/），所以**不引用「fp32 峰值 %」**——那個數字要靠本機實測才拿得到，不該編一個 M5 Pro 規格上來。可報的是**聚合 GEMM 吞吐 5.31 TFLOP/s fp32**（2.055 PFLOP ÷ 387.081s），這是**下限**，因為它把 SDPA 與所有空轉都記在 GEMM 帳上。

