# 影策 ↔ MediaGateway 私有契約

影策（open-ai-canvas Go 後端，`backend/internal/app/gateway_client.go`）直連 Gateway
`http://127.0.0.1:8600`。本文記錄兩倉庫之間的隱式契約——**任何一側改動這些鍵名/枚舉，
必須同步另一側**，否則對方靜默拿到空路徑/卡死輪詢。內容以代碼為準（核對日 2026-09-29）。

## 1. 兩張 API 面

| 面 | 路由 | 誰在用 |
|---|---|---|
| 原生 job 面 | `POST/GET /v1/jobs`、`POST /v1/jobs/{id}/cancel`（`server/main.py:85-104`） | 影策 Go `gatewayClient`（createJob/getJob/wait） |
| OpenAI Sora 風格面 | `POST/GET /v1/videos`、`/v1/videos/{id}/content`、cancel/delete（`server/compat_h3cweb.py:302+`） | 影策 newapi 適配器（分鏡圖走 `input_images` multipart） |

## 2. Job 完成態媒體：snake_case 內部鍵

`GET /v1/jobs/{id}` 返回整行 DB 記錄（`core._job_row`，`server/core.py:123-127`）：

- **`output_path`**（頂層字段）：主產物絕對路徑。worker 返回 dict 裡的 `output_path`
  存進專列（`core.py:273-275`），不在 meta 裡。
- **`meta`**（JSON 對象）：除 `output_path` 外的全部 worker 返回鍵。
  - shot worker（`server/workers/shot.py:103`）寫入 **`last_frame_path`**（尾幀 png，
    下一鏡 first_frame 接力用）與 `stages`（各階段子輸出）。
- 這些是 **snake_case 內部鍵，不是 OpenAI sora 標準字段**。影策
  `gatewayJob`（gateway_client.go:47-57）依賴：頂層 `output_path` +
  `meta.last_frame_path`（`metaString` helper）；`RenderAllProjectShots` 用尾幀做跨鏡續幀，
  用 `output_path` 列表餵 concat job。

**媒體獲取方式**：兩機同機部署，影策直接讀本地路徑（refs 也傳本地絕對路徑）。
HTTP 下載僅兩條：`GET /v1/videos/{id}/content`（sora 面，409 if not completed）、
`GET /files/{name}`（`compat_h3cweb.py:151-159`，**只服務 h3cweb 歷史 mp4**，新 job
產物不在其中）。不要把新產物指到 `/files`。

## 3. 狀態方言（兩套詞表，勿混）

**core 原始詞表**（jobs 表，`core.py:68`；`/v1/jobs` 原樣返回）：

```
queued | running | completed | failed | cancelled
```

注意：是 **`completed` 不是 `succeeded`**（無 succeeded 枚舉）。

**compat_h3cweb 對外映射**（sora 面，`_SORA_STATUS`，`compat_h3cweb.py:164-165`）：

| core | sora 對外 |
|---|---|
| queued | queued |
| running | **in_progress** |
| completed | completed |
| failed | failed |
| cancelled | **failed** |

`/v1/videos/{id}` 未知狀態兜底映射為 `in_progress`（:460）；僅 `completed` 時附帶
`url` → `/v1/videos/{id}/content`（:467-469）。舊 h3cweb 面另有 `_STATUS`（:83-84），
`cancelled` 同樣折疊為 `failed`。

## 4. 影策側依賴點（改動前必查）

`gateway_client.go`：

- :49-57 `gatewayJob` 結構：`id/status/error/progress/phase/output_path/meta`。
- :124-146 `wait()`：僅 `completed` 視為成功；`failed`/`cancelled` 均為終態報錯。
  **core 若新增終態枚舉，影策會無限輪詢到超時。**
- `metaString(job, "last_frame_path")` / `metaString(job, "output_path")`
  （project_shot_render.go:223-227）。

## 5. 相關（非破壞性但值得知道）

- job id 形如 `{type}_{8hex}`（`core.py:131`），如 `shot_a1b2c3d4`、`video_xxxxxxxx`。
- 取消：`POST /v1/jobs/{id}/cancel`（cooperative，running 任務由 worker 查 `cancel()`；
  queued 直接置 cancelled）。sora 面 `POST` 與 `DELETE /v1/videos/{id}` 等價。
- sora 面入口校驗：`input_images` 與 `first/last_frame_image` 互斥（h3 Ref2VA 靜默丟幀，
  入口 fast 400，`compat_h3cweb.py:441-442`）；參考圖長邊 >1536 自動縮圖（:268）。
