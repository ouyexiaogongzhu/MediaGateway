# 整合計劃：ref-audio / h3.c / FlashVSR / 生圖 / 影策全鏈路

> 狀態：**規劃中，尚未執行任何一項**
> 日期：2026-10-03
> 範圍：`MediaGateway`（Python）+ `h3.c`（C/Metal）+ `open-ai-canvas`（影策，React+Go）
> 輸入：6 份並行審計（h3 介面 / Gateway worker 流 / 優化落地 / 影策流程 / docs 記憶 / 現有計劃）
> **本文所有「已驗證」條目都由本人讀原始碼或跑指令確認過，未採信任何 agent 敘述作為證據。**

## 進度（2026-10-03 第一輪並行開發後）

| 項目 | 狀態 | 驗證 |
|---|---|---|
| P0-1 分鏡批次丟鏡頭 | ✅ | `TestRunStoryboardVideoBatchQueuesBeyondActiveLimit` 等 6 條通過 |
| P0-2 `shotNumber` 重編號 | ✅ | 後端 + 前端 payload 都補上；`KeepsShotNumbers` 通過 |
| P0-4 輪詢超時清 taskId | ✅ | 404 才清，其餘保留；`storyboard-batch-resume.test.ts` 14 條通過 |
| P0-6 `mute_source_audio` 語意 | ✅ **已修正 agent 的版本** | 見下方警告 |
| P1-2 `core_reuse` 生產解析度 | ✅ | `video.py` `CORE_REUSE_SAFE_PX = 262_144`，超出即 clamp |
| P1-8 `shot` worker 擬音 | ✅ | `params["sfx"]`，預設音量 0.3 |
| P1-9 `mix` 參數轉發 | ✅ | 原已大部分接好；另補 `ShotIn.sfx` 欄位（`main.py` 原本會丟棄） |
| P1-10 擬音 seed | ✅ | `sfx_seed` → 同 tag 同 seed 得同檔 |
| §5.2 引擎選擇統一 | ✅ | 兩個路由共用 `_engine_params_for_model()` |
| P1-1 `denoise_audio()` 接線 | ✅ 但**濾波器本身是壞的，已修** | 見下方 ⚠️ |
| P0-5 voice 綁定資料 | ✅ 但**踩到兩個更深的斷點** | 見下方 ⚠️⚠️ |
| 真機驗證 1–4 | ✅ **PASS（實測）** | 見下方 ✅ 區塊 |
| 真機驗證 5 影策對白腿 | ⛔ **BLOCKED（誠實記錄）** | `character_voice_bindings` 刻意留空 |

**基準**：MediaGateway `pytest` 118 → **150 passed / 26s**；影策 `go build`+`vet` 乾淨，`bun test` 2034 → 2041 pass（27 fail 皆 clean HEAD 既有，stash 驗過）。

> ### ⚠️ `denoise_audio()` 一直在輸出假數字，而且會摧毀語音（2026-10-04）
>
> 接線是新的，但**函式本體從未真跑過**。`tests/test_denoise.py` 裡**零個 pytest 測試** —— 它是 `main()` 腳本，所以那些自我檢查斷言**從沒進過 CI**。這就是假數字活下來的原因。
>
> **第四個 bug（最嚴重的那個）**：濾波器自己判斷「哪些影格是噪聲」用的是**中位數 +15dB** —— 正是本文件 §7.2 記為量測陷阱的那個相對門檻。連續語音時中位數就落在語音位準上，於是 **69–85% 的影格（含語音）被標成噪聲**，噪聲譜被語音灌高，整片語音被壓到 `gmin=−26dB` 軌。
>
> **那個「降 20–26dB」不是降噪，是整軌砍掉。**
>
> | | 底噪 | 語音 | 峰值 | 耗時 |
> |---|---|---|---|---|
> | 修前（6 支真實 clip） | 「降」20–26 dB ❌ | **掉 0.5–4.0 dB** | 不變 | 0.22–0.28s |
> | 修後 | **降 3.8–14.0 dB** ✅ | 掉 0.0–0.6 dB | 不變 | 0.26–0.34s |
>
> **docstring 那組「7.5–13.7dB / 語音 0.0dB」是量測假象** —— 那是用**相對門檻**量出來的（§7.2 第 7 條陷阱）。門檻已改為 `p10 + 10dB`（錨在安靜端，對 −25…−85dB 的底噪漂移免疫），並修掉第三個 bug：ISTFT 尾巴 224 樣本（7ms）未初始化 = 每支檔結尾都是數位零。
>
> 接線的守門條件（`not video_audio_ref and not tracks`）**維持不變** —— 它只在 h3 音軌真的會進成品時才觸發，那條路徑底噪約 −60dB，是唯一值得濾的。
> ⛔ `1a_wind` / `1b_wind` 全程無靜默 → `hiss.py` 印「無法分級」，**不報數字**。

> ### ✅ 真機驗證（2026-10-04，全部實測，非文件引述）
>
> | 項 | 結果 | 證據 |
> |---|---|---|
> | 1 排程不再停擺 | **PASS** | 真 35GB video + 1GB concat 併發。concat 在 video 相對 t=7.149s 被准入、7.704s 完成，video 視窗 0→55.864s。舊碼算 35+35+1=71 > 36，會停擋剩下 48 秒 |
> | 2 concat 單一連續配樂 | **PASS** | 波形自相似度：舊的逐鏡配樂在剪切點 **0.9648**（同素材＝重啟），`/v1/concat` 端點 **0.0135**，對齊源音樂本身的自然失相關 0.013 |
> | 3 9:16 批次解析度 | **PASS** | `width:0,height:0` 送進去，Gateway 收到 **480×864**，輸出檔 480×864（h>w）。對照：`/v1/videos` 空 size 會落到 864×480 —— 那是舊的直式專案結果 |
> | 4 `images` `n>1` | **PASS**（兩分支） | generations n=2 → 2 張相異；edits n=2 → 2 張相異。**SDXL 分支無法驗證**：:8187 無回應。四個 26GB 生圖 job 的 **swap 增量 = 0** |
> | 5 影策對白腿 | **BLOCKED** | 見下 |
> | 2b pipeline B `render-all` | **PASS** | `concat_f04deed8` 480×864。後半段對「連續位置」相關 **+0.683**、對「重啟」**−0.141** → 單一連續配樂成立（Go 路徑，非只端點） |
>
> **本輪驗證抓到四個「測試／對照本身是假的」問題：**
> 1. **第 1 項的第一次嘗試是無效的。** concat 在 video 轉 `running` 後 0.57s 就送出，那時引擎還在載入 —— 舊 bug 在那一瞬間**同樣**會算 0 進帳，所以那個測試**分辨不出新舊版本**，是因為錯誤的理由而 PASS。判斷必須閘在**引擎常駐**（`precompute AdaLN` / `load transformer core` 這類生成期 phase），不是 job 狀態。
> 2. **concat 測試偵測不到端點切換。** `/v1/jobs` stub 記錄的 `{"type":..., "params":...}` 與 `/v1/concat` 記錄的形狀完全相同，`submitted(t,"concat")` 兩邊都匹配 —— 把呼叫改回舊形式，**主測試照樣過**。已新增 `submittedAt()` 直接斷言請求路徑。
> 3. ⛔ **`git stash` 當對照組會給出假陰性。** stash `storyboard_batch.go` 使 `storyboard_batch_test.go` 編譯失敗 → **一個測試都沒跑** → 失敗計數讀出 **0**，看起來像「移除我的改動後零失敗」。實際上那 5 個測試在 clean HEAD 本來就失敗。正解：`git archive HEAD` 解到獨立目錄跑，工作樹完全不動。
> 4. **`| tail -N` 會把崩潰的腳本回報成 exit 0** —— 管線的 exit code 是 `tail` 的，不是腳本的。
>
> **第 5 項 BLOCKED 的原因**：`voice_profiles`=9 但 `character_voice_bindings`=0、`shots`=0。`shotVoiceKey()` 仍回 `""` → `params["voice"]` 從不寫入 → 對白/ref-audio 路徑**仍然無法執行**。這是**刻意的**（見 ⚠️⚠️ 區塊：`asset_versions` 空，角色資產對分鏡渲染本來就隱形），不是 #1 沒落地。
>
> **⚠️ 本文件與先前給 agent 的簡報都把生產預算寫成 40 —— 錯的。**
> `core.py:40` 的 40 只是**預設值**；launchd plist 設 `MG_BUDGET_GB=36`，**生產實際是 36**。
> 這不改變第 1 項的結論（35+1=36 ≤ 36 剛好放行；預算 40 也放行），但 **36 才是準數**：video 佔 35 後只剩 **1 GB**，而註冊的 worker 裡**剛好只有三個塞得進去** —— `concat` 1.0、`mix` 1.0、`noop` 0.1；其餘全部 ≥8.0（`sdxl` 8.0 / `voice` 8.7 / `tts_qwen` 9.0 / `music` 15.0 / `flashvsr` 20.0 / `qwen_image`＝`image_upscale` 26.0）。**排程器的實際規則是「一個 video ＋ 一個 concat/mix/noop」**，不是「≥2GB 就擋」。第 1 項的測試正好踩在 35+1=36 這條界線上。
>
> 另外兩個量測陷阱（實測踩到）：
> - **RSS 不能當 h3 記憶體的代理** —— macOS 不把 Metal／統一記憶體的權重算進 RSS。
> - **`/info` 的 `model.loaded` 是反向陷阱** —— 閒置時是「存在且為 false」，載入時是**整個 key 不存在**。

> ### ⚠️⚠️ P0-5 補完後才看見：對白路徑有**兩個比「資料沒種」更深的斷點**（2026-10-04）
>
> `voice_profiles` 已 0 → 9（schema v36 遷移），音色名**不是**原本清單裡的 OpenAI 名，而是直接讀模型 `config.json → talker_config.spk_id` 得來：
> `vivian serena uncle_fu ryan aiden ono_anna sohee eric dylan` —— 與 Gateway 端 `tts_qwen` 接受的 9 個**完全相同**。
> 額外收穫：`spk_is_dialect` 顯示 **`dylan`＝北京話、`eric`＝四川話**，其餘為通用。
> ⚠️ **性別未經驗證** —— 模型卡沒列，確認要真的出一段音。
>
> **斷點一：`/v1/audio/speech` 路由整條是斷的。**
> 影策把**音色名塞在 `model` 欄位**，而 Gateway 靠 `model` 分流（`compat_openai._is_qwen` = `startswith("qwen")`）→ 任何不是 `qwen*` 的都掉進 CosyVoice → 那條需要 torch、本機沒有 → **全線死亡**。
> 也就是說 **`/v1/audio/speech` 這條路對所有對白都是壞的，不論有沒有綁定。**
> 已改成 `model:"qwen3-tts"` + `voice:<key>`。
>
> **斷點二：批次路徑即使有綁定也解析不到音色。**
> `storyboard_batch.go` 的 `batchShotToRowVideoRow` 只映射 `CharacterAssetID`、丟掉 `CharacterVersionID`，而 `storyboardRowVoiceKey` 靠後者 → 永遠回 `""`。已在 DB 層回填。
>
> **`character_voice_bindings` 仍是 0，而且是刻意的。**
> `asset_versions` 表 **0 列** —— 11 個角色資產全是舊式圖片上傳，`primary_version_id` 空、沒有 `definition`，沒有東西可以綁；`autoShotCharacterReferences` 也會因為 `versionID == ""` 跳過它們，**不論音色如何它們對分鏡渲染本來就是隱形的**。沒有偽造 version、沒有臆測性自動配音色（payload 裡沒有性別/定義資料可以誠實地選）。
> 正路徑是 UI 的 `BindProjectCharacterVoice`（它一次建立 version + binding）。

> ⚠️ **P0-6 agent 版本做錯了，已手動修正。** Agent 只改了 `mute_source_audio`，卻仍把 TTS wav 鋪進混音 —— 結果是 **h3 的 ref-audio 對白 + Qwen3-TTS 同一句疊兩個音色**，那正是使用者棄用的 ttsmux 變體。已改為：有 audio ref 時 TTS wav **只當 ref 不進混音**。

| 項目 | 狀態 |
|---|---|
| P0-3 `renderAllProjectShots` | ✅ **已接**（不再是未做項）。決策仍成立：它是同步阻塞 handler，所以前端改走 `/storyboard-video-batch` 輪詢 |
| P0-5 voice 綁定資料 | ✅ **9 個 profile 已種**（schema v36）。`character_voice_bindings` 仍刻意留空 —— `asset_versions` 也是 0，角色資產對分鏡渲染本來就隱形。正路徑是 UI 的 `BindProjectCharacterVoice` |
| P1-1 `denoise_audio()` | ✅ **已接線**（`shot.py:140`），守門條件＝無 dialogue ref 且無其他音軌 |
| P1-2 `core_reuse` 生產解析度 | ✅ **已加守門** `CORE_REUSE_SAFE_PX`（`video.py:96`），超出即 clamp |
| P1-3 `keep_loaded` / 排程帳務 | ✅ **已修**，真機驗證通過 |
| P1-4 批次解析度 | ✅ 已修（9:16 → 480×864，實測） |
| P1-5 concat 配樂 | ✅ 已修並實測（單一連續床） |
| P1-6 取消路徑 | ⛔ **仍未做**。前端無 `cancelTask`／「停止任務」呼叫，批次路徑只能等它自然跑完 |
| L2 / L4 GPU 驗證 | ✅ **已跑**（第 1、2、2b、3、4 項 PASS；第 5 項誠實 BLOCKED） |
| SDXL 分支 `n>1` | ⛔ 無法驗證 —— `:8187` daemon 未起 |

### 待決（agent 查到但沒動的）

1. **`/v1/images/edits` 的 `n` 是死碼** —— `compat_openai.py` 兩個分支都 clamp 了 `n≤4` 但只跑一次 job 回單張。`n=4` 靜默回 1 張。修法兩行（照 generations 那樣迴圈）。
2. **`P1-10` 選了 seed 而非 `#N`** —— 純 seed 下「指定不存在的變體要報錯」不可表達（`seed % n` 恆在範圍內）；agent 改為對真正會失敗的情況（未知 tag / 非整數 / 越界）大聲報錯。代價已加 `ponytail:` 註解：素材增刪會重映射該 tag 的所有 seed。

---

## 0. 怎麼用這份文件

每一項標記三種狀態之一：

| 標記 | 意義 |
|---|---|
| ✅ 已驗證 | 讀過原始碼／跑過指令，結論可複現，附檔案:行號 |
| ⚪ 無引用 | 介面存在、傳輸層已備好，但**沒有任何呼叫端**送這個參數 |
| 🟡 引用已漂移 | 有人寫過／文件說過，但程式碼已改，文件或記憶過期 |

執行順序：**P0 → P1 → P2**。P0 不修完，P1 的任何效能數字都不可信（因為分鏡會丟、profile 送錯、語音永遠不觸發）。

---

## 1. 五個問題的直接回答

| # | 問題 | 答案 |
|---|---|---|
| 1 | ref-audio 接入 Gateway 了嗎？ | **傳輸層 100% 完成，但業務鏈路休眠且有害**。`shot.py:67-72` 已把 TTS wav 當 `{"kind":"audio"}` 塞進 refs；`h3_bridge.py:310` 已映射到 `H3_REFERENCE_AUDIO`。但 DB 裡 `voice_profiles` / `character_voice_bindings` 都是 **0 筆**，所以 `shotVoiceKey()` 永遠回 `""`，`params["voice"]` 從不寫入 → 整條 Ref2VA 路徑從未被執行過。而 `shot.py:97` 的 `mute_source_audio=bool(tracks)` 會丟掉 h3 自己渲染的音軌，正是使用者明確否決過的 ttsmux 變體。 |
| 2 | h3.c + wav(TTS) + 後期擬音 有什麼可優化？ | 有 4 處真縫隙，見 §3。最大的一個：`denoise_audio()`（`render.py:394`，已實測降 7.5–13.7 dB）**從未被任何 worker 呼叫**。**音訊混合鏈（擬音+配樂）只通了一半——見 §3.5**。 |
| 3 | 之前的 h3.c / FlashVSR 優化有落到代碼嗎？ | 混合。已落地：`core_reuse` 缺省值修正、`keep_loaded` 常駐路徑、`video.py:125` 的 fallback。**未落地／已漂移**：見 §7 的 17 條文件矛盾，其中 3 條會導致錯誤的效能結論。 |
| 4 | 生圖模型更新／切換了嗎？ | 換了。`qwen_image.py:44-76` 已有 Viggle v0.3（6 步 DMD 蒸餾）+ Krea2 v1.1（8 步 + TAEHV）雙引擎，2026-10-02 實測。**但選擇器有缺口**：兩個 compat 路由的引擎推導邏輯不一致，且都要靠模型名子字串才到得了 krea2。 |
| 5 | 影策流程問題 + h3.c 未接入介面？ | 影策有 **2 條互不相通的管線**，且其中一條有 3 個 P0 級靜默資料遺失。**擬音（雨/風）只通在舊管線，新管線完全沒有** —— 見 §3.5。h3.c 未接入介面清單見 §4。 |

---

## 2. P0 — 先修這些，否則後面全是白做

### P0-1｜影策分鏡批次在 20 鏡專案裡靜默丟掉 16 鏡 ✅ 已驗證

- `platform/runtime_policy.go:149` `ActiveTaskLimit: 5`
- `repository/repository.go:365-369` `ActiveTaskCountForUser` **沒有排除自己**，所以父批次任務本身佔掉 1 個名額
- `storyboard_batch.go:304-320` **同步**建立全部子任務 → 實際只有 4 個能進
- `:355-357` 只有**全部**子任務都失敗才讓父任務失敗 → 部分成功時 UI 看到「成功」
- `web/src/.../use-canvas-storyboard.ts:894` `if (!result) return;` 把沒回來的鏡頭丟掉

**症狀**：20 鏡的短劇專案，畫布上只出現 4 個分鏡，父任務顯示成功。
**修**：分頁／佇列建立子任務（一次最多 4 個，其餘排隊），並把「部分失敗」回報到 UI。

### P0-2｜`shotNumber` 重編號 vs 前端用真實 row key ✅ 已驗證

- `storyboard_batch.go:305` `shotNumber := index + 1`（用迴圈索引）
- 前端 `use-canvas-storyboard.ts:661, :894` 用真實 `row.shotNumber` 當 key

**症狀**：部分重跑時，前端拿新編號去對舊資料，**結果與首幀同時錯位**。
**修**：批次建立時不要重編號；或前端一律用 DB 主鍵而非 `shotNumber`。

### P0-3｜`renderAllProjectShots` 沒有任何前端呼叫 ✅ 已驗證

README 的「一鍵渲染全部分鏡」在 UI 裡**不存在**。要嘛接上按鈕，要嘛從 README 刪掉。

### P0-4｜任務輪詢超時會清 taskId，按鈕復原 → 重複燒 GPU ✅ 已驗證

- `task-center.ts:538-545`：storyboard 輪詢 13 分鐘、video 輪詢 32 分鐘
- `use-canvas-storyboard.ts:676-680` catch 會清掉 taskId，按鈕因此重新可用

**症狀**：GPU 還在跑，使用者再按一次 → 同一批鏡頭渲染兩遍。
**修**：catch 不得清 taskId 直到服務端確認任務真的不在 active 清單；或加冪等鍵。

### P0-5｜影策 Ref2VA 全鏈路被 0 筆資料閘住 ✅ 已驗證

```
sqlite3 backend/data/open_ai_canvas.db \
  "SELECT 'voice_profiles', COUNT(*) FROM voice_profiles
   UNION ALL SELECT 'bindings', COUNT(*) FROM character_voice_bindings;"
→ voice_profiles 0 / bindings 0
```

- `project_shot_render.go:289-302` `shotVoiceKey()` 需要 `CharacterVoiceBinding` → `VoiceProfileID` → `VoiceKey`，任一跳空回傳 `""`
- `:227-232` 拿不到就設 `voiceSkipped = true`，**靜默降級成無對白**
- `storyboard_batch.go:412-429` 丟棄 `CharacterVersionID`

**修**：(a) 補 seed 資料或遷移既有角色的 voice 綁定；(b) `voiceSkipped` 必須讓 UI 看得見，不可靜默。

### P0-6｜`shot.py:97` 丟棄 h3 音軌 ✅ 已驗證

```python
render.mux(
    video=vid["output_path"], audio_tracks=tracks, output=final,
    # h3 不再出音軌：有 voice/music 時丟棄自帶音軌（Ref2VA 工作流裡那是模型渲染版）
    mute_source_audio=bool(tracks),
    ...
)
```

- 註解的前提「h3 不再出音軌」**是過期的**：開了 `--ref-audio` 之後 h3 照樣渲染一條完整音軌，而且實測乾淨（見 §6 數據表）
- 這正是使用者明確否決過的 ttsmux 變體，目前卻仍是生產預設

**修**：`mute_source_audio` 改為只在**沒有** ref-audio 驅動時才成立（判斷 `refs` 裡有無 `kind == "audio"`）。

---

## 3. P1 — 真實優化點（按價值排序）

### P1-1｜`denoise_audio()` 從未被呼叫 ✅ 已驗證 ⚪

- `render.py:394` `denoise_audio()` / `:352` `_denoise_pcm()` 都存在且已實測有效
- `grep -rn denoise_audio server/workers/` → **零命中**
- 關鍵背景（記憶已定案）：`server/render.py` 的 power-domain Wiener 降 **7.5–13.7 dB，語音 0.0 dB**，5 秒片段 0.3 秒
- `afftdn` 最佳只有 3.60 比值且 `tn=1` / `gs` 無效（底噪不是固定電平）

**修**：`video.py` 產出後、`shot.py` 混音前插一步。**注意**：這會改變 `keep_loaded` 的記憶體佔用估算（去噪需要載入整條 PCM），先量再上。

### P1-2｜`profile: "quality"` 送進生產 = `core_reuse: 4` = 毀畫質 ✅ 已驗證

- `project_shot_render.go:196` `"profile": "quality"`
- `server/workers/video.py:80` quality profile 帶 `core_reuse: 4`
- `video.py:127` 缺省已改回 1，但**影策顯式送 quality 就會覆蓋回去**
- 記憶定案：`core_reuse=4` 在草稿 288×512 是雙贏，在 **544×960 生產解析度實測畫面被毀**

**修**：影策改送 `standard`，或 Gateway 端擋掉 `core_reuse >= 4` 當 `width*height` 超過門檻（把「解析度相依」這條教訓寫成程式碼，而不是靠人記得）。

### P1-3｜h3.c 效能槓桿都沒接 ✅ 已驗證 ⚪

`grep core_reuse|dit_layers|preview_denoise|render_width /Users/vincent/code/open-ai-canvas/backend/` → **零命中**。影策完全沒辦法調這四個。

- `keep_loaded` 在 Gateway 有（`video.py:190`、`compat_h3cweb.py:148/393/422/459/535`），在影策**零命中**。實測 −28.87 s/job，是目前性價比最高的單一槓桿。

**修**：在影策的 shot 參數層加 `keep_loaded`（尤其在批次渲染時）。這個改動最小、回報最直接，建議第一個做。

### P1-4｜影策批次路徑不送 width/height ✅ 已驗證

`storyboard_batch.go` 沒送寬高 → 9:16 專案會渲成 16:9。走 `project_shot_render.go` 那條有 `shotRenderSize()`（`:36-44`，draft 512×288 / 9:16 480×864 / 其他 864×480）。**兩條管線的尺寸政策不一致。**

### P1-5｜`project_shot_render.go:138` concat 沒有 `music_segments` ✅ 已驗證

批次那條路徑鋪音樂，這條不鋪。

### P1-6｜全鏈路沒有取消路徑 ✅ 已驗證

Gateway 有 `cancel` 回調進 worker，影策**沒有任何**取消呼叫。20 鏡專案改稿一次 → 前面所有在跑的鏡頭全部白燒。

### P1-7｜`video.py:119` 缺省 steps 是 4 不是 6 ✅ 已驗證

記憶／`h3_speed_plan.md:21` 都寫 6。程式碼是 `params.get("steps", profile.get("steps", 4))`。draft profile 確實是 6（`video.py:82`），所以「draft=6、standard=4」是對的，但文件寫錯。

---

## 3.5 音訊混合鏈（擬音 雨/風 + 背景配樂）— 只通一半 ✅ 已驗證

**影策有兩條互不相通的渲染管線**（共用 0 張表，見 P0-1）。音訊混合的完成度**兩條不一樣**：

| | 管線 A：分鏡批次 → row_video | 管線 B：RenderProjectShot → `shot` composite worker |
|---|---|---|
| 入口 | `storyboard_batch.go:306` `CreateRowVideoTask` | `project_shot_render.go:512` `RenderProjectShot` |
| 視頻 | `/v1/videos` | `shot` worker 的 video 階段 |
| 對白 | `/v1/audio/speech`（CosyVoice/Qwen3-TTS） | `shot` worker 的 voice 階段 + **ref-audio 驅動口型** |
| **擬音（雨/風）** | ✅ **通了** | ❌ **完全沒有** |
| **背景配樂** | ✅ 分鏡層通了（`storyboard_music_batch` → `/v1/music` → `music_segments` → concat） | ⚠️ 單鏡頭通了（`params["music"]`），**concat 組裝時 music_segments 掉了** |
| `keep_source_audio` | `dialogue != ""` → **保留** h3 音軌 | `mute_source_audio=bool(tracks)` → **無條件丟棄** |

### 擬音在管線 A 的完整鏈路（已驗證可跑）

1. `prompt_template_defaults.go:239` LLM 結構化輸出只能從**凍結的 40 個 tag** 裡挑 0–3 個（`ambience_wind` `ambience_rain` `thunder` `footsteps_stone` …）
2. `storyboard_batch.go:92` 解析進 row；LLM 沒給時由 `storyboard_audio_fallback.go` 兜底
3. `storyboard_row_video.go:310-317` → `rowVideoMixTrack{SfxTag: tag}`
4. `:389` `POST /v1/mix` `{video_job_id, tracks, keep_source_audio}`
5. `compat_h3cweb.py:655` `create_mix` 解 `video_job_id` → `core.create_job("mix", …)`
6. `mix.py:129-135` `_pick_sfx(tag)` 從 `assets/sfx/<tag>/` 隨機挑一個 mp3

**素材庫一致性 ✅ 已驗證**：`assets/sfx/` 實際 **40 個 tag 目錄 / 304 個音檔**，與 LLM 凍結詞表 **40 個 enum 完全相同，零漂移**（無 enum 有而沒素材，也無有素材而 enum 沒列）。每 tag 6–8 個變體（30 個 tag 有 8 個、4 個有 7 個、6 個有 6 個）。

### P1-8｜管線 B（`shot` worker）沒有擬音階段 ✅ 已驗證

`shot.py:33` 的白名單是 `("image", "video", "voice", "music")` —— **沒有 sfx/foley**。`mix.py` 的 legacy `sfx_tag` 契約 `shot.py` 完全沒接。

**後果**：走 `RenderProjectShot`（也就是 ref-audio 那條新路徑）渲染的分鏡，**雨聲、風聲、腳步、雷聲全部沒有**，只有對白 + 配樂。而 LLM 明明已經在 `prompt_template_defaults.go` 為每個分鏡選好了 0–3 個 sfxTags —— **資料產生了，被丟掉。**

**修（最小）**：`shot.py` 的 `enabled` 加 `sfx`，`mix` 段落讀 `params["sfx"]` 走 `mix.py:_pick_sfx`。不需要新 worker，`mix.py` 已經有 `_pick_sfx`。

### P1-9｜`params["mix"]` 影策從不送 ✅ 已驗證

`shot.py:83` `mix = params.get("mix") or {}`，但影策兩條管線都沒有任何地方設這個 key（`project_shot_render.go` grep 無 `mix`）。

**後果**：`subtitles` / `dialogue_volume` / `music_volume` **對影策全部不可達**，一律吃預設（1.0 / 0.15）。字幕是整條鏈路唯一沒被任何呼叫端使用的功能。

**修**：影策的 shot request 加 `mix` 欄位轉發；或至少先把字幕接上（否則 `mix.py:112` 的 `audio_tracks or subtitles` 分支對影策是死碼）。

### P1-10｜擬音隨機挑選、無 seed ✅ 已驗證

`mix.py:34-40` `_pick_sfx` 用 `random.choice`，**沒有 seed**。兩個問題：

1. **不可重現**：同一個分鏡重跑一次，雨聲換一個檔，輸出位元不同 → 無法 diff、無法快取、無法回歸比對。
2. **跨鏡頭不連續**：同一場雨，5 個鏡頭會挑到 5 個**不同的錄音**。對連續短劇這是明顯的聽感斷裂。

**修**：`sfx_tag` 支援 `"ambience_rain#3"` 這種指定變體，或加 `seed` 參數由上層決定。**注意**：要跟 P1-8 一起做，否則只在管線 A 生效。

### P1-11｜兩條管線對「h3 音軌該不該留」判斷相反 ✅ 已驗證

- 管線 A：`storyboard_row_video.go:322` `keepSourceAudio = dialogue != ""` → **保留**（且註解明說「對白行 h3 输出音频=参考音频」）
- 管線 B：`shot.py:97` `mute_source_audio=bool(tracks)` → **無條件丟棄**，註解前提「h3 不再出音軌」已過期

這是 P0-6 的根因。**ref-audio 定案後，正確答案是「refs 裡有 audio 就保留、沒有才丟」**，兩條管線都該照這個改。

---

## 4. h3.c 介面：Gateway 沒接入的清單

`vendor/h3_bridge.py:122-159` 已填滿 **33 個** `h3_params` 欄位且零缺口 —— 那一層是完整的。缺的是**上游沒人送值**，以及**參數從哪裡能被設**。

| h3.c 介面 | 位置 | Gateway 傳輸層 | 影策有沒有送 | 備註 |
|---|---|---|---|---|
| `--ref-audio` | `h3_cli.c` / `h3_bridge.py:374,393` | ✅ 已接 | ⚪ 無 | 需先解 P0-5 |
| `keep_loaded` | `video.py:190` | ✅ 已接 | ⚪ 無 | −28.87 s/job，最該接 |
| `core_reuse` | `video.py:127` | ✅ 已接 | 🔴 送錯值 | 影策送 quality → 4 |
| `dit_layers` | 生產 45 / `h3.h:139` | ✅ 已接 | ⚪ 無 | **下一個未測槓桿**，完全未量過 |
| `preview_denoise` | `h3.h` | ✅ 已接 | ⚪ 無 | |
| `render_width` / `render_height` | | ✅ 已接 | ⚪ 無 | 影策只送 `profile`+`size` 字串 |
| `token_reduction` | `video.py` quality/draft | ✅ 已接 | ⚪ 無 | |
| `denoise_reuse` | draft/reference | ✅ 已接 | ⚪ 無 | |
| `reference_image_size` | `h3.h:44-47` | ✅ 已接 | ⚪ 無 | ⚠️ **不是**死槓桿——它是二值 enum，`h3.c:586-587` 對非 512 直接 reject。`tests/test_video.py:137` 斷言 512 會過**只是因為 FakeEngine 不驗證** |
| `kv_ratio` | | ✅ 已接 | ⚪ 無 | ⚠️ kv_ratio=2 的品質**未被證明可用**，也未被證明不可用 |

**結論**：`h3_bridge` 不是瓶頸。瓶頸是「影策 → Gateway」這一層沒有暴露任何 knob。要接的話一次加一個 dict 欄位即可。

---

## 5. 生圖引擎狀態

### 已落地 ✅

`server/workers/qwen_image.py:44-76`：
- **Viggle**：Qwen-Image-2.1-viggle-turbo-v0.3-6step-Q4_K_M.gguf，DMD 蒸餾 6 步免 CFG，7 個 sigmas（尾巴補 0）。⚠️ 照抄 HF card 的 6 個值會產出整片鹽胡椒。
- **Krea2**：`Krea2_turbo_uncensored_v1.1-Q6_K.gguf`，**8 步**（非設計點的 4 步），2026-10-02 依實測目檢配置。代價 +60~82% 牆鐘。TAEHV（`--tae`）是主要提速來源。
- ⚠️ Krea2 視覺塔**必須是 4B 那顆**。配 8B mmproj 會靜默失效（`llm.hpp:386` 只印 ERROR，sd-cli 照樣 rc=0 存檔，但圖完全無視 `-r`，連審查降級一起發生）。
- `MAX_REFS = 8` 是單機實測硬上限，換機器需重測。

### 需要修的 🟡

1. **記憶過期**：記憶寫「Krea2 4 步」，程式碼是 8 步且註解明寫是依 2026-10-02 實測改的。`docs/tools.md` 也寫 8 步。**記憶錯、文件對。**
2. **兩個 compat 路由邏輯不一致** ✅ 已驗證：
   - `compat_openai.py:155-166`（generations）：有 `unc` 推導 + `"krea2" in mlow` → `params["engine"]="krea2"` + seed 遞增
   - `compat_openai.py:305-317`（edits）：**沒有** `params["engine"]`，只有 `uncensored`/`nsfw` → `unc=True`
   - 結果：走 edits 路由時，除非模型名帶 uncensored/nsfw，否則永遠到不了 krea2；而且兩條路徑的引擎選擇語義已經分叉。
   - **修**：抽一個共用的 `_qwen_engine_for(model_name)`，兩邊都呼叫。
3. **`DBCACHE` 生產預設關閉**（`QWEN_IMAGE_DBCACHE=0`）—— 這是**正確的**，不要動。關的理由是畫質不是效能：512² 實測鬼影（半透明重複人形 + 2~3 張幽靈臉），而短劇草稿檔正是 512 級。牆鐘只快 9.1%，買不到。

---

## 6. 對白嘶聲：已定案，供後續開發直接引用

單一尺（`h3_ab4/hiss.py`，絕對門檻 −55 dB、100 ms 幀）：

| 檔案 | 語音 | 底噪 | 分離 | peak | 削波 |
|---|---|---|---|---|---|
| TTS 原始 wav | −23.3 | −87.1 | 63.8 dB | −6.2 | 0% |
| `sil_refaudio_long.mp4`（9.4s，含 ref-audio） | −21.3 | −86.5 | 65.1 dB | −4.7 | 0% |
| `sil_refaudio.mp4` | −21.6 | −98.4 | 76.8 dB | −7.3 | 0% |
| `sil_ny.mp4`（無 ref-audio） | −36.0 | −60.0 | 23.9 dB | −0.0 | 0.0003% |

- 開 ref-audio 後底噪 −60.0 → −86.5 dB = **降 26.5 dB**，幾乎等於 TTS 源本身
- 時序對齊 r=0.912，75/75 有聲幀重疊，兩者語音窗都是 0.4–8.7 s，零偏移
- **幀數**：8.48 s 的台詞放不進 124 幀（5.167 s）。`align(204) = 209` 是最小值但會**截掉 72 ms**，所以選 **226 幀（9.4167 s）**。這是算過的，不是錯。

**已否決、不要再做的**：把 h3 渲染音軌與 TTS 原聲混合（`sil_refaudio_ttsmux.mp4`）。使用者已明確退回。

---

## 7. 不要做的事（已證偽的 42 個槓桿 + 測量紀律）

### 7.1 已證偽，不要重測也不要推薦

完整 42 條見 `docs/h3_speed_plan.md` 與記憶。重點分類：

- 🟢 **可靠證偽**：`core_reuse=4` 於生產解析度、`afftdn` 的 `tn=1`/`gs`、`DeepFilterNet`（宣告不列 torch 但 `df/enhance.py:8` 硬 import）、`arnndn`（只吃 RNNoise）、`AVAudioIO`（不能處理檔案）、`sd-server` hot-swap（`main.cpp:90`）、HF 上的去噪 LoRA（只有 ASMR 加強）。
- 🟡 **n=1 或無噪聲地板**：單次測量即定案的槓桿，重測成本高且大概率同樣結果。
- 🔴 **可能建立在錯誤量測上**：`FlashVSR topk_ratio` / `local_range` —— 實務上是 inert，但**兩個機制都未被確認**（`h3_speed_plan.md:256-274`）。不要宣稱已證偽，只說「觀察不到效果」。

### 7.2 必須繼承的四個字串級門檻

1. **門檻本身要能抓住「量測工具不可靠」**。本專案已栽 7 次：`unit-rate vs 牆鐘`、文件 headline 其實是兩個 job 的總和（774s）、把 static 函式編譯起來跑（別手推）、RSS 少報 75×、Laplacian rate 把手臂判成 2.7× 銳利、秒級計時不可信（bf16 負向測試 1.61–3.03×）、f32 解 AAC 的 peak 是解碼器 overshoot 要用 s16、**相對門檻分級在兩個方向都會失效**。
2. **跨設定 A/B 一律用單位速率，不用總牆鐘**。位元相同但牆鐘差 2.8× = rate 變了，不是 work 變了。
3. **bf16 7.2% 是天花板不是預期值**（`h3_speed_plan.md:185`）。**沒有 fp32 matmul 參考實作，所以「fp32 peak %」永遠不許引用**（`:204`）。VAE draft baseline 是懸空的（`:146`）。kv_ratio=2 的品質**未被證明可用**（`:298-301`）。
4. **逐 chunk E(n) 曲線的形狀**分辨 progressive / constant-factor：對照組的 hump 會消失、S 曲線會維持。共用機的 loadavg 必須逐 chunk 記錄。

### 7.3 下一個該測的槓桿

**`dit_layers`（生產 45，`h3.h:139`）完全未量過。** 兩個前置條件：

- `perf_round3_plan.md` 裡那個「draft 解析度 instrumented run」（約 2–3 分鐘）**從未執行**，而 41 分鐘的生產 run 卡在它後面 → **先跑它**
- ⚠️ **每個 arm 都必須在獨立進程跑**：每個 arm 改變 prepared-DiT 快取的形狀，同進程會互相污染

另外：`h3_speed_plan.md:191-202` 描述的 scatter/drift/tail 分析器**在 repo 裡不存在**，要測先寫。

---

## 7.4 h3 prompt 格式：兩個已查清的事實 + 一個待測實驗

> ### ⚠️⚠️ 7.4.1 的結論**只對 h3.c 成立，對 Gateway 不成立** —— 本文件曾在此處給出錯誤建議
>
> **原結論（範圍錯了）**：「h3.c 對 prompt 格式完全無感，任何格式都跑得動，⛔ 不要糾結。」
> 對 `h3.c` 這個 C 引擎本身**是對的**（零 Context-IR 命中，它只 tokenize）。
>
> **但漏了一層**：`compat_h3cweb.py:188-195` 的 `POST /jobs` **強制**跑 `_validate_context_ir()`，不合規直接 **400 拒轉**，而 `/v1/videos` 也 funnel 經同一個函式。
>
> **後果（2026-10-04 由生產 DB 的失敗記錄實錘，非推論）**：`storyboard_row_video` 與 `storyboard_video_batch` 兩個任務都死在
> `gateway 400 ... missing field integrated_multimodal_description / overall_soundscape / non_diegetic_music`
> —— **影策從來沒有任何一處產生 Context-IR**（原始碼零命中）。
> 換句話說：那條管線在**執行期是死的**，不是「格式品質較差」。
>
> **唯一活著的管線**：`RenderProjectShot` → `POST /v1/jobs`（`main.py:86` 直接呼叫 `core.create_job`，**繞過那道 gate**）→ `shot` 複合 worker。
> → 隱含風險：**若哪天有人把 gate 補到 `/v1/jobs` 上，唯一能跑的管線會跟著一起死。**
>
> **How to apply:** 談「格式寬容度」要分清是哪一層。**引擎寬容 ≠ 傳輸層寬容。**
>
> **成因（已查證）**：這道 gate 是 **HEAD 那個 commit `1e45880`「feat: Context-IR 強制 gate」** 加的（+84 行）。
> 也就是說「加一道正確性 gate」這件事本身，在**沒有盤點呼叫端**的情況下，**靜默掐死了一整條管線** —— 而那條管線正是影策 UI 分鏰批次渲染實際走的那條。
> ⛔ **為什麼 gate 落地時沒被任何人發現**（已驗證）：
> - `test_h3cweb_prompt.py` 全過 —— **它只測 gate 自己**，沒有任何測試覆蓋「影策送來的 prompt 能不能通過」。
> - **Go 側完全沒有測試走到這條路由**：`grep 'HandleFunc("/v1/videos"'` 在 `backend/internal/` 裡 **0 處命中**。
> - Python 側**早就知道這個坑**：`tests/test_p11.py:106-107` 留著警告「prompt 若被 gate 擋下就會因錯誤的理由 400＝**測試假綠**」，並用合規 prompt `CTX_T2VA` 迴避。防護做了，但只防了自己那一層。

### 7.4.1b Context-IR vs h3.c 格式 —— 引擎層面這個選擇不存在 ✅ 已驗證
### 7.4.1 Context-IR vs h3.c 格式 —— **這個選擇不存在** ✅ 已驗證

`h3_multimodal.c` 全部 360 行 = tokenize + vision + text encode。對
`integrated_multimodal_description` / `overall_soundscape` / `non_diegetic_music` / `<d>` /
`subject_definitions` / `retention_analysis` 的 grep **全部零命中**；
`tests/` `inputs/` `specs/` `docs/` 同樣零命中。

`README.md:319` 唯一一次提到 Context-IR，只寫 "expects a **Context-IR-like** description"，
接著給 `Scene:/Action:/Camera:/Look:/Audio:` —— **h3.c 沒有「自己定義的」格式，那個例子同樣沒有任何程式碼在解析**，就是純文本餵 tokenizer。

**結論：h3.c 對 prompt 格式完全無感。任何格式都跑得動，格式只影響輸出品質（取決於模型訓練時用什麼）。**
⛔ 不要在「Context-IR」與「h3.c 發明格式」之間選 —— 兩者走的是同一條路徑。

### 7.4.2 三欄位結構的來源存疑 ✅ 已驗證

`MiniMax-H3/Ref2VA/tokenizer/tokenizer_config.json` 有完整 ChatML 模板，但**模板裡沒有三欄位結構**
（那是模型卡 / prompt guide 的約定，不是 template 的東西）。

官方推理程式碼不在本機 → **無法交叉驗證**。目前唯一權威複本是
`~/.claude/skills/h3-prompt-writing/references/base-en.txt`。
這才是記憶裡「格式權威 = skill 非 README」的真正理由，**跟 h3.c 無關**。

已有實證支持用三欄位：官方格式渲染後擬音噪聲複驗不變 → **至少沒有變差**。

### 7.4.3 ⚠️ 待測實驗：h3.c 沒套 ChatML wrapper

grep `im_start|im_end|vision_start|vision_end|image_pad` 在所有 `.c`/`.h` 裡**零命中**。
`h3_multimodal.c:221` 只做 `<Picture %zu>: ` 前綴 + vision pad，然後 **raw prompt 進 tokenizer**。
tokenizer 是 `add_bos_token: False` / `bos_token: None` —— 連 BOS 都沒有。

**順序也不同**：h3.c 是「所有參考圖排在前面，之後才是整段 prompt」；官方模板是文字與 vision token **在 content 裡交錯**。

兩種可能，**本機無法判定**：(a) 官方發布的推理程式也不套；(b) h3.c 這裡真的漏了，品質有隱性損失。

> （`<Picture 1>: ` 比模板的 `Picture 1: ` 多一對尖括號這點，`h3_multimodal.h:10` 註解寫 "the **released** presentation"，作者是照官方發布物寫的，這條信作者。）

**測法（成本極低）**：把 `user\n…\n` 直接手寫進 prompt 字串（同一個 tokenizer 入口），
同一 seed、同一參數、同一 draft 短片（L2 的 512×288 / 39 幀就夠），A/B 對比**對白清晰度**與**擬音品質**。

⛔ **不要順便改 picture 順序** —— 那是一次兩個變數，量不出是誰的功勞。
⛔ 一次只改一個變數。

---

## 8. 建議執行順序

| 批次 | 內容 | 為什麼這個順序 |
|---|---|---|
| **1** | P0-1, P0-2, P0-4（影策三個靜默資料遺失） | 純 Go 端改動，不碰 GPU，改完立刻能用現有資料驗證 |
| **2** | P0-5 + P0-6（voice 綁定資料 + `mute_source_audio` 語意） | 解鎖 Ref2VA，讓 `shot.py:67-72` 第一次真的被執行 |
| **3** | P1-2（`profile` 送 `standard`）+ P1-3（`keep_loaded`） | 一個是修 bug、一個是拿效能；一起驗收 |
| **4** | P1-1（接上 `denoise_audio`） | 有實測背書，但要重測記憶體佔用所以放後面 |
| **5** | P1-8 + P1-10（shot worker 加擬音階段 + seed） | LLM 已經選好 sfxTags 了，這是**把已產生的資料接回來**，改動最小、收益最直接；seed 必須跟著一起做否則跨鏡頭不連續 |
| **6** | P1-4, P1-5, P1-6, P1-9（尺寸 / concat 配樂 / 取消 / mix 參數轉發） | 影策體驗層 |
| **7** | §7.3 的 `dit_layers` 量測 | 最貴、最容易得到壞結論，基礎設施先備好 |
| **8** | §5.2 的引擎選擇統一 | 影響面小，可隨時插隊 |
| **9** | §7.4.3 ChatML wrapper A/B | 成本極低（同 tokenizer 入口改 prompt 字串），且**若成立會影響所有 prompt 的品質** —— 排在最後只是因為它不修任何東西，但值得早做 |

### 驗收原則（分層，不要每項都跑 e2e）

**18 個項目裡只有 3 個需要 GPU。** 每一項都跑生產級 e2e ≈ 12 小時，是浪費。

現成工具：`pytest tests/ -q` = **118 passed / 19.5s / 零 GPU**（每次提交都跑，這是免費守門）。

> ⚠️ **本節只計「機器驗證時間」，不含實作時間。** 後者才是主導成本且從未量過。
> L2 / L4 的時間**尚未實測**，不要當預期值引用。

#### L0 — 單元測試（<1 秒）｜純接線類

`tests/test_shot.py` 已是完整 mock harness：`patch_stages()`（`:37-58`）把 4 個 stage worker 換成 fake 並 stub `render.mux`。

| 項目 | 補哪個測試 |
|---|---|
| P0-6 `mute_source_audio` 語意 | `test_shot.py` 斷言：有 audio ref 時不 mute |
| P1-8 sfx 階段 | `patch_stages` 加 sfx fake + 斷言進了 tracks |
| P1-9 `mix` 參數轉發 | 斷言 `subtitles` / `dialogue_volume` 有傳到 `render.mux` |
| P1-10 seed / 變體指定 | 延伸 `test_p11.py:120` 既有的 mix sfx 測試 |
| §5.2 引擎選擇統一 | `test_compat_openai.py` 斷言兩個 compat 路由推導一致 |

#### L1 — 固定資產回放（實測 0.58 秒）｜音訊 / 混音類，零 GPU

Fixture：`assets/shot_c1c4150e/`（已驗證是完整四階段）

```
image/image.png    787 KB
voice/dialogue.wav 246 KB
video/output.mp4   512×320
music/output.wav   1.9 MB
shot.mp4           512×320 / 1.632s / 有音軌
```

| 項目 | 餵什麼 | 成本 | 驗收 |
|---|---|---|---|
| P1-1 denoise | `voice/dialogue.wav` | 0.3s | `hiss.py -55` |
| P1-8 sfx | `video/output.mp4` → `mix.py` | 秒級 | ffprobe 聲軌數 |
| P0-6 / P1-9 / P1-11 | 只重跑 `render.mux` | 秒級 | ffprobe + `hiss.py` |

#### L2 — 短片 A/B（3–8 分鐘，GPU）｜只有改 h3 渲染參數的

**只有 P1-2（profile / core_reuse）、P1-3（keep_loaded）、§7.3（dit_layers）需要。**

用 draft **512×288 / 39 幀（1.625s）**。實測基準：`shot_c1c4150e` 的 512×320 video 階段約 1 分鐘。
⛔ 不要跑 226 幀 / 544×960（41 分鐘）——那是 P0-2 畫質驗收的用途，不是日常檢查。

#### L3 — 影策層（0 GPU）｜純 Go + DB

P0-1 / P0-2 / P0-3 / P0-4 / P0-5 全部是 Go + SQLite。

**驗收 = SQL 斷言，不是渲染**：建立 20 鏡後 `SELECT COUNT(*) FROM shots` 應為 20（現況 0）；
`shotNumber` 與前端 key 對得上；`renderAllProjectShots` 有前端呼叫端。

#### L4 — 全鏈路 e2e：**每批一次，不是每項一次**

且只在「改動會動到階段間資料流」的批次跑：**P0-5 / P0-6 / P1-8**。
純影策批次（P0-1/2/3/4、P1-4/5/6/9）**不需要 L4** —— 它們不碰 Gateway worker。

一次 e2e 的定義：生圖 → TTS → 視頻 → 混音 → mp4，並用 `hiss.py` 量音軌（同一把尺，門檻從輸入檔算）。

### 總帳（機器驗證時間）

| 層 | 項數 | 機器時間 | 來源 |
|---|---|---|---|
| L0 | 5 | ~20s | ✅ 實測 `pytest tests/ -q` = 19.5s |
| L1 | 4 | **0.58s** | ✅ 實測（mix 0.08 / mux 0.15 / denoise 0.15 / hiss 0.20） |
| L3 | 6 | ~5s | ✅ 實測（SQL 斷言） |
| **GPU 外小計** | **15** | **≈ 26 秒** | |
| L2 | 3 | **未實測** | ⛔ 需 GPU（草稿短片，實測 fixture 512×320 video 階段約 1 分鐘） |
| L4 | 2 批 | **未實測**，且大部分是 GPU | ⛔ 需 GPU |

**驗證成本幾乎免費（約 26 秒），成本在實作程式本身 —— 那部分從未量過，不要估算。**

> 教訓：本節最初寫「L1 ~5 分鐘 / L2 ~20 分鐘 / L4 ~15 分鐘」，全部是估的，實測 L1 是 **0.58 秒**（差 500 倍）。
> ⛔ 未實測的數字不要寫進表格，那正是量測陷阱第 7 條的變種。

### 紅線

- 不要引用任何本文 §7 裡列為 🔴 的數字
- 每層的量測只用該層的尺：L1 用絕對門檻 `hiss.py -55`（相對門檻會雙向失效，見 §7.2）

---

## 9. 本文件與既有文件的關係

| 文件 | 關係 |
|---|---|
| `docs/h3_speed_plan.md` | 速度戰役主文件。本文件**不重複**其內容，只引用。§7.3 的前置條件寫在那裡 |
| `docs/perf_round3_plan.md` | 第 3 輪效能計劃。§7.3 的 draft instrumented run 未執行這點來自它 |
| `docs/flashvsr_knob_series.md` | ⚠️ `:3` 說「series not yet executed」但 `:205` 說「15/15 ACCEPTED」——**文件自相矛盾，未解決** |
| `docs/tools.md` | 生圖引擎權威（Krea2 8 步）。⚠️ `:42` 文件了一個 `steps4/reuse2/core4` 組合，而 `h3.c:574` 會 reject 它 |
| `docs/yingce_gateway_contract.md` | ⚠️ 行號全部過期，且有 10 處實質契約不符（C-1..C-10）。重寫前不要當依據 |
| `docs/architecture.md` | 架構圖，內容仍成立 |