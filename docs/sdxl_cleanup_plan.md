# SDXL 全面退役清理方案（已拍板待執行，本文檔即執行清單）

日期：2026-10-10 ｜ 狀態：**待執行** ｜ 預估：半天（含測試跑綠）

## 背景與依據

SDXL checkpoint（RealVisXL fp16）+ 4 個 ControlNet（openpose/depth/canny/lineart，約 20-25G）**此前已刪**，
生成管線（`StableDiffusionXLPipeline` + ControlNet）已不可能運行。本方案清掉殘餘：

- `~/tool/sdxl-daemon` **2.7G**（.venv 1.5G + annotators 1.2G + 代碼 12K + outputs）
- MediaGateway 生成側代碼與 aux 直通端點
- 影策渠道 2 行

**aux 四件套歸屬結論（全刪依據）**：

| 控制圖 | 未來歸屬 | 說明 |
|---|---|---|
| pose（照片提取） | VACE 定檔的 **ONNX CoreML** 路線 | 本就不走 SDXL daemon |
| pose/depth/normal（3D 場景） | 影策導演台 three.js 原生 renderModes | 零權重，已在 |
| pose 參考圖生成 | 導演台 mannequin 方案（另案） | 方向反轉：生成而非檢測 |
| lineart / 白模（2D 提取） | **無** | 唯一消費者（SDXL ControlNet）已死；sd.cpp 不吃控制圖；VACE depth 已除名 |

`/annotate` 雖可獨立於 checkpoint 運行（只吃 annotators/ 權重），但零下游，屬死功能。

## A. 刪 `~/tool/sdxl-daemon`（2.7G）

```bash
rm -rf ~/tool/sdxl-daemon
```

目錄**無版本控制**，刪後恢復靠下述 recipe。

## B. MediaGateway 代碼（6 處）

執行時先 grep 複核，再逐項刪：

1. `server/workers/sdxl.py` — 整檔刪除
2. `server/compat_openai.py` — images/generations 與 images/edits 的 sdxl 分支路由（model 含 sdxl → daemon）
3. `server/compat_openai.py` — `/v1/aux` 端點（同步直通 daemon `/annotate`）；
   **執行前必查**：`grep -rn "v1/aux" ~/code/open-ai-canvas/`，確認影策無調用方（若畫布有集成按鈕，需連帶刪前端）
4. `server/compat_chat.py` — `_KNOWN_MODELS` 刪 `sdxl-noobai`、`sdxl-realvis` 兩行
5. `server/compat_h3cweb.py` — sdxl 相關 1 處（執行時 grep 定位）
6. 測試：`tests/test_sdxl.py` 整檔刪；`tests/test_compat.py::test_aux_annotate` 刪；
   `tests/test_compat_openai.py` 內 sdxl 路由斷言改寫/刪除

## C. 影策渠道（2 行）

`channel_models` 表刪 `sdxl-noobai`、`sdxl-realvis`（admin API 或直接 SQLite DELETE；
影策「拉取模型目錄」依賴 Gateway `/v1/models`，Gateway 側 `_KNOWN_MODELS` 刪後畫布快照硬刷新即自癒）。

## D. 文檔

- `docs/architecture.md`：IMG 塊刪「SDXL daemon :8187（常駐）」分支；已棄用清單加 SDXL
- `docs/tools.md`：aux 四件套章節標記退役（保留 NormalBae 512px 坑等 recipe 作為恢復依據）
- `docs/plan.md`：歷史文件不改

## E. 恢復 recipe（真需要時半天回來）

- annotators 權重：`lllyasviel/Annotators`（sk_model.pth + sk_model2.pth 各 17M、scannet.pt 290M、
  body_pose_model.pth 200M、hand_pose_model.pth 141M、facenet.pth 147M、dpt_hybrid-midas 470M）
- daemon 代碼 12K 憑本文檔 + memory 重寫；已知坑：**NormalBaeDetector 小圖輸出固定 512×512 需尺寸歸位**、
  detector 惰性常駐、IDLE_UNLOAD_S=600、/generate 需 checkpoint+ControlNet 而 /annotate 不需要
- venv：torch（MPS）+ diffusers + controlnet_aux

## F. 驗收

```bash
# MediaGateway：當前基線 147 passed / 1 skipped
.venv/bin/python -m pytest tests/ -q -p no:cacheprovider
# 影策：build + 測試
cd ~/code/open-ai-canvas/backend && go build ./... && go test ./internal/app/ -count=1
cd ~/code/open-ai-canvas/web && npx tsc --noEmit -p tsconfig.json && bun test
```

基線之外的失敗即回歸，需修復後才算完成。
