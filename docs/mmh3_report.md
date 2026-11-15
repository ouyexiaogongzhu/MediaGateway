# mmh3 測試報告（M5 Pro 48GB，2026-11-15）

**結論先講：mmh3 在本機跑通，生產規格（544×960/15s）9.7 分 vs h3.c 38.7 分 = 4.0×，畫質生產級（單幀目檢），峰值內存 18.5GiB 遠低於 40G 上限。轉正前剩三個未驗證項：音頻品質、時序一致性、server 模式穩定性。**

---

## 1. 環境

- mmh3-org/mmh3 `9c0652f`（2026-11-12），Rust + Metal kernels，無 PyTorch/ComfyUI
- `make` 編譯 25.6s → `target/release/mmh3`
- 本機：M5 Pro / macOS 27.0.1 / 48GB——M5 Neural Accelerators 路徑生效（log 明示 `Metal DiT INT8-weight products: Int8, attention: Fp16`）
- 模型 5 檔 ~52G（int8 convrot，Comfy-Org / yniw），**sha256 全驗通過**
- 下載全走 **hf-mirror.com**（huggingface.co 直連在本機是死的：resolve HTTP 000/ENOTFOUND）

## 2. 崩潰原因（兩次生產規格 run 崩潰，已定案）

**不設 `--vram-budget` 時 mmh3 吃滿 Metal 建議工作集（~36G），與 IDE/瀏覽器搶到超限 → macOS SIGKILL。** 無崩潰報告、無部分產物，兩次一致。

修法：`--vram-budget 40`。設了之後所有 run 的實際峰值 RSS 只在 **15.9–19.3 GiB**，距上限很遠。

## 3. 核心對比（544×960 / 362幀 / 15.08s）

| 引擎 | 配置 | 端到端 | DiT | VAE 解碼 | 峰值內存* |
|---|---|---|---|---|---|
| h3.c（生產現況） | core1，6步 | 2324.5s = **38.7 分** | 1877s | ~380s | 24.7 GiB |
| h3.c（已出局） | core4，6步 | 1371.6s = 22.9 分 | 928.9s | ~380s | 25.8 GiB |
| **mmh3** | 4步 FastH3+VSA | **581s = 9.7 分（4.0×）** | 478.3s | **92.4s** | 18.5 GiB |

*內存通道不同（h3.c 只認自家 `peak=`；mmh3 認行程 max RSS）——方向性結論成立，非實驗室級對照。mmh3 的 RSS 可信（權重經 mmap 進 RSS）；h3.c 的 RSS 是壞的（0.24G vs 實握 18G）。

附帶：mmh3 的 VAE 解碼只佔 92.4s——h3.c 那個 ~440s 的 VAE 稅在 mmh3 不存在。

## 4. 5 秒片矩陣（124幀 / 4步 / budget 40）

| 檔位 | 形狀 | 總耗時 | 峰值 RSS | VAE |
|---|---|---|---|---|
| 草稿 | 544×960 | ~2.7 分 | 17.9 GiB | 30.6s |
| 720p 級 | 768×1344 | ~5.5 分 | 15.9 GiB | 48.7s |
| 1080 級 | 1088×1920 | ❌ `packed matrix shape mismatch` | — | — |
| 1080 級替代 | 1024×1792 | ~13 分 | 19.0 GiB | 84.8s |

**形狀規則**：必須 32 倍數，**但不是所有 32 倍數都能跑**——INT8 預打包權重對形狀有額外限制（1088×1920 崩、1024×1792 過）。用前逐形狀試。

**內存不是約束**：像素 4× 範圍內峰值只漲 3 GiB（權重 ~15G 是大頭，激活漲得慢）。720p 是甜蜜點（時間/記憶體雙優）。

## 5. latent upscaler（draft-steps）

機制：一次生成內，前 N 步跑**半尺寸畫布** → upscaler 放大 latent → 餘步全尺寸畫細節。不是「先出片再放大」。

720p / 124幀 / `--draft-steps 2`：

| 路徑 | 總耗時 | 峰值 RSS |
|---|---|---|
| 直出 720p | 330s = 5.5 分 | 15.9 GiB |
| draft-2步 + upscaler | **224s = 3.7 分（-33%）** | 19.3 GiB（+3.4）|

- 帳目：2 個半尺寸步 24.8s + upscaler 5.2s + 2 個全尺寸步 130.8s
- 畫質：同級乾淨，無放大偽影（雙線性放大的失敗簽章＝斑點/字母紋/點陣，皆無）
- **代價：構圖隨機化**——同 seed 也會變構圖（文檔明載）。要構圖確定性（同 seed 重跑同畫面）就不能開
- 限制：目標畫布必須 **64 倍數**（544×960 不合規，768×1344 合規）；4 步模型 draft-steps ≤ 2

## 6. 沒有起作用的東西

- latent upscaler 在其他所有 run 中**未參與**（可選項，未開）
- h3.c 的三個測量陷阱不適用於 mmh3（它有自己的正確計時）；但「hf 直連是死的」「sha256 必驗」兩條繼續有效

## 7. 未驗證項（轉正前的門）

| 項 | 為什麼重要 |
|---|---|
| **音頻品質** | T2VA 原生出音頻，未聽過——嘶聲/品質未知（h3.c 的老問題是否重演） |
| **時序一致性** | 單幀看不出閃爍/漂移；4步 FastH3+VSA ≠ 6步 Euler，需看整段 |
| **server 常駐模式** | `--vram-budget`/`--idle-unload` + loopback HTTP，掛 Gateway 的前置 |
| PDMD 2步 / TaoMate 3步 LoRA | DiT 時間可能再砍半，畫質待驗 |
| Ref2VA（--reference-audio/video） | 對白克隆路徑，對應 h3.c 的 --ref-audio |

## 8. 產物與復現

```bash
# 樣片
~/tool/mmh3/samples/  # draft_5s / 720p_5s / 1080alt_5s / 720p_draft2_5s / B_prod(15s)

# 生產規格復現（必帶 --vram-budget）
cd ~/tool/mmh3 && target/release/mmh3 generate --models models \
  --prompt "..." --width 544 --height 960 --frames 361 --steps 4 \
  --vram-budget 40 \
  --patch minimax_h3_fasth3_vsa_datafree_patch_rank64.safetensors \
  --out out.mp4
```

## 9. 轉正路徑（建議，未做）

1. 使用者目檢 `mmh3_B_prod.mp4` + 聽音頻（§7 前兩項）
2. server 模式常駐試驗（`mmh3 server`，權重不卸載 + idle-unload）
3. Gateway 加 `mmh3` worker（同 h3.c 模式：create job → HTTP → 出片路徑），與 h3.c 並列可選引擎
4. PDMD/Ref2VA 試驗排後
