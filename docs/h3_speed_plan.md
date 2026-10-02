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

**544×960/f362 的 VAE decode = 每 job ~432s GPU 固定税**（~380s wait + ~52s encode），占 core4 GPU wait 的 32%、core1 的 18%。这是端到端增益落后 DiT 增益的全部原因，也是 core_reuse 出局后下一个该攻的杠杆。

⚠️ **VAE 的 `wall` 不是解码成本**：1803.738 vs 2767.907 那个 1.53× 是假的——它是 resident context 的生命週期，大半时间在陪 DiT 闲置。**只用 `wait` / `encode`。**

### 9.4 三个测量陷阱（本轮各踩一次）

1. **`h3_gpu_profile_mark` 的 `wall` = 距上一个 mark 的间隔，不是 phase 耗时。** 跨 job 时它会把上一个 job 的尾巴算进来；曾据此误判「432s 卡在 mark 内 = VAE 慢」。
2. **`ps -o rss` 不能当占用读数**：读 0.24GiB 而 h3 真握 ~18GiB（unified memory 上 Metal 配额不进 process RSS）。占用只认 h3 自己的 `peak=` 与 `memory_pressure`。
3. **Laplacian 方差不能当锐利度**：把 core_reuse=4 评为「锐利 2.7 倍」（lap_var 917 vs 339），因为糊块本身就是高频纹理，而该指标奖励一切局部曲率。块格检测在 2/4/8/16/32/64px 全部为阴性 ⇒ 是真糊不是压缩格。**坏 arm 的 mp4 反而大 1.79 倍**（抖动难编码）——**文件大小是反向品质信号，不是细节保真度信号**。

### 9.5 结论与待办

- **`core_reuse=4` 不得用于生产解析度**，此前基于草稿解析度的签核作废。
- ✅ **`video.py` 缺省 `core_reuse` 已从 4 改为 1（2026-10-02 修复）**，现为 `profile.get("core_reuse", 1)`（`server/workers/video.py:127`）。**这里曾是一个与本战役无关的既存陷阱，记录它为什么曾经开着**：当时 fallback 写死 4，而 `standard`/`draft` 两个 profile 都没设该 key，于是**毁画质的值正好是任何不显式钉值的调用方的默认值**——一个从未打算测过生产解析度的默认值，静悄悄地决定了产出画质。`gateway.db` 120 个 video job 中 114 显式钉 1、0 钉 4、**6 无此 key（当时吃 4）**。
  ⚠️ **`_PROFILES["quality"]` 里的 `core_reuse: 4` 是显式档位，不是默认值，勿动**；但若有人日后把它挪进任何 fallback 路径，先回来看这一节。另：`core_reuse=4` 还被 `compat_h3cweb.py:60,62` 归一为 1，要真开需两处同改。
- 未决：VAE 的草稿基线（`wall=125.216 wait=89.228`）无可查日志、帧数未知，故 ~432s/job 是回归还是优于线性**悬空**，待有基线时再判。

