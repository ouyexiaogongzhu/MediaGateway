# 效能戰役 round 3 — handoff

**日期：** 2026-10-03
**給：** 另一個 session 直接接著做，不需要讀前兩輪的 transcript。
**前兩輪結論在哪：** `docs/h3_speed_plan.md` §9（h3 VAE）、`docs/flashvsr_knob_series.md`（FlashVSR）。本文件只講「接下來做什麼」和「哪些路已經被證死」。

---

## 建議順序：草稿解析度 instrumented run → 看 drift → 有決定才跑生產

**為什麼先跑草稿（~2-3 分鐘）而不是直接 41 分鐘：**

41 分鐘那個 run 回答兩個問題，但只有第二個需要生產解析度：

| 問題 | 需要什麼 | 草稿能回答嗎 |
|---|---|---|
| VAE 這筆稅**是不是算術受限**？ | 每個 tile 的 `wait` 分佈 | **能** — tile 形狀（304×304、seq 2532）跟畫布無關 |
| 生產解析度的**絕對數字** | 544×960 | 不能，只有生產解析度有 |

草稿 288×512 是 2 個 tile、生產 8 個，但每個 tile 的 GEMM 形狀幾乎一樣（seq 2273 vs 2532）。所以：

- **草稿跑出來 drift 明顯** → VAE 的稅是搶資源不是算術，**41 分鐘不用跑**
- **草稿跑出來 168 個 tile 的 `wait` 高度均勻** → 算術受限成立，再花 41 分鐘拿絕對數字才值得

而且草稿跑順便**驗證儀器本身**。見下面「為什麼沒有 microbenchmark」。

**跳過中位數版 microbenchmark：** 要重新驗證它自己的雜訊地板 —— 那正是它現在失敗的地方 —— 花的时间比省下的 41 分鐘更多，而且驗證完還是同一個「跑不跑 41 分鐘」的問題。

---

## 為什麼沒有 microbenchmark

h3-bench 的提案是「用幾秒鐘的 bf16 vs fp32 GEMM 微基準，決定那 41 分鐘值不值得跑」。**想法對，在這台機器上做不到** —— 實測負向測試（rows 翻倍時間應翻倍）重跑五次：

```
1.61×   1.69×   1.87×   2.06×   3.03×
```

同一段程式碼、不同 literal，應該約 2.0×。內部一致性低於任何可用門檻 → 儀器自己判 `RESULT: VOID`，一個數字都不印。

不是 warmup 次數問題（5 reps 給 1.66×、20 reps 給 3.26×，反而更差）。是**排程抖動與功耗管理壓過訊號**：單顆 GEMM ~4ms，太短。

**結論寫進 `benchmark_thermal_drift.md` 記憶檔了：這台機器上「用秒級測試決定要不要花 41 分鐘」不成立。** 短負載要可靠，單次時間必須長到能壓過雜訊。

---

## 已被證死的路（別再試）

| 假設 | 證偽方式 |
|---|---|
| `core_reuse=4` 能加速正片 | 544×960 下畫面全毀。草稿的雙贏結論已反轉 |
| `H3_VAE_TILE_PIXELS=512` | 304 是整個 256-512 範圍的**全域 FLOP 最小值**（編譯真的 `configured_tile_pixels()` 算出來，不是手推） |
| FlashVSR 三個旋鈕有效果 | `topk_ratio` 1.5/1.0 與 `local_range` 7 產出**位元完全相同**（`md5 9c4fb4dc`）＝ 同一份計算 |
| FlashVSR 有 VAE 槓桿 | decode 只佔 **11.3%**，六個 arm 只差 1.14× 而 diffusion 差 13-17% |
| h3 的 432s VAE 稅可套到 FlashVSR | 那是 h3 在 544×960 的東西，FlashVSR 整場 diffusion 才 284s |
| 排隊是 8-9 分鐘排程數字的主因 | `gateway.db`：11 個 flashvsr job 的 `started_at − created_at` 全是 **0.01-0.47s** |
| 精度（fp32→bf16）值 2× | 上限 **1.761×**（SDPA 13.4% 留在 fp32）；端到端只 **7.2%**，且 `encode` 那 53.1s 不縮小 → 實際只有 **2.3%** 那一支 |

---

## 開跑前必讀的五個測量陷阱

全部在 `docs/h3_speed_plan.md` §9.4，每一個都真的造成過錯誤結論：

1. **`h3_gpu_profile_mark` 的 `wall` = 距離上一個 mark 的間隔**，不是 phase 耗時。跨 job 會把上一個 job 的尾巴算進來。
2. **`ps -o rss` 讀 0.24GiB 而 h3 真握 ~18GiB**（unified memory 上 Metal 配額不進 process RSS）。佔位只認 h3 自己的 `peak=` 和 `memory_pressure`。
3. **`root-gpu` 不是忙碌度量**。MPSGraph 子 buffer 不計入 root 時間戳，實測 7.754s vs wait 774.162s（99% 假氣泡）。`h3_gpu.h:29` 明寫 `command_wait_seconds` 才是完整 turnaround。
4. **整進程 total mark 跨多個 job 存活** → per-job 必須 ÷2。判別法不靠轉寫是否正確：**用報告自述的占比反解**（32%×928.9s ⇒ 437s，落在 440s 附近）。
5. **Laplacian 方差不能當銳利度**，**檔案大小是反向品質訊號**。糊塊本身就是高頻紋理，該指標會把損壞的 arm 評成「銳利 2.7 倍」；而壞 arm 的 mp4 反而大 1.79 倍。

**另外兩條不是量測陷阱，是流程規則：**

- **絕對門檻在這台機器上會誤判。** swap 是 macOS 的配置高水位不是壓力訊號（閒置機可停在 28GB）；`memory_pressure` free % 才是。要設門檻就用 **used ≤ 40GiB**，不要用 swap，也不要用「free ≥ 85%」（那是 ~7GiB used，比實際需求嚴太多）。負載中另需逐 chunk 記 **GPU utilization**（`ioreg -r -d 1 -c IOAccelerator | grep -oE '"Device Utilization %"=[0-9]+'`），因為 `top=` 在這台認不出搶 GPU 的行程。
- **跨 arm 切換旗標要用獨立進程順序跑，不要同進程交錯。** 切換會逐掉 prepared-DiT cache（8 個 arm 有 5 個出現 22-24s 重載），製造出 42-49% 的假 CV。

---

## 現在的生產狀態

| | |
|---|---|
| `video.py` `core_reuse` 缺 key fallback | **已由 4 改 1**（`5359d14`）。毀畫質的值曾經是預設 |
| FlashVSR `kv_ratio` | **已 3.0→2.0 上線**（`~/tool/FlashVSR` `2e6275f`）。wide −13.6%、texture −16.6%；dark 未測 |
| `keep_loaded` | **仍未上線**。−28.87s/job、零程式碼成本，但沒任何 caller 傳 flag。要接只能改影策 `~/code/open-ai-canvas` |

---

## 如果 round 3 做完，41 分鐘仍然不動

那是誠實的答案，不是失敗。已證明的事實：

- 快的設定會毀畫質（`core_reuse=4`）
- 41 分鐘裡最大的一塊（VAE 17.1%）端到端只值 7.2%，而且其中一半（`encode`）bf16 碰不到
- 幀數降到 f209 以內省不了多少（`wait`/frame 平坦在 0.22-0.235）

**剩下唯一沒被量過的槓桿是 `dit_layers`。** 生產用 45，`h3_params` 裡可調，而它直接決定 DiT 的深度。草稿就能量，成本 ~3 分鐘。**如果要找下一個 41 分鐘的答案，從這裡開始，不要回到已證死的三條路。**
