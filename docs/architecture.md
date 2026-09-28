# 影策系統架構（2026-09-28）

> 四層：畫布 → 影策控制平面 → Gateway 媒體平面 → 引擎。qwen3.6-27b 已刪除（模型已棄用）。

```mermaid
%%{init:{"theme":"base","themeVariables":{"fontSize":"14px"}}}%%
flowchart TB
    subgraph L1["🖥 用戶端"]
        UI["影策 Web :3000<br/>畫布：分鏡 · 生圖 · 視頻 · 超分按鈕 · 音頻"]
    end

    subgraph L2["🎬 影策 Backend（控制平面）Go :8090"]
        CH["渠道/模型目錄<br/>CHANNEL_000002 → :8600/v1"]
        TASK["任務系統<br/>分鏡串接 P10 · 音頻 P11"]
        TOOLS["/api/tools/upscale"]
        AS[("Asset Store<br/>SQLite")]
    end

    subgraph L3["🚪 MediaGateway（媒體平面）FastAPI :8600"]
        API["OpenAI/newapi 兼容面<br/>chat · videos · images · audio · upscale · aux · models"]
        Q["Job Queue + Scheduler<br/>FIFO · MEM_GB 預算 · LLM↔視頻互斥 · 用完即關<br/>stream：供應商透傳 / 本地模擬回放"]
        RT{{"按模型名分流"}}
        API --> Q --> RT
    end

    subgraph W["⚙ Workers（用完即關）"]
        direction LR
        H3["h3.c 視頻<br/>MiniMax-H3 唯一"]
        FV["FlashVSR 超分唯一<br/>15s→1080 ~8.5min"]
        QI["sd.cpp Metal 生圖<br/>Qwen-Image-2.1 ~1.5min"]
    end

    subgraph D["🔌 常駐守護（手動管理）"]
        direction LR
        OM["omlx :8082<br/>3.8-uncensored"]
        GK["grok2api :8402<br/>grok-chat-fast"]
        QL["qwen MLX :8000<br/>3.8-27b"]
        SD["SDXL :8187<br/>aux 四件套"]
        C2["chatgpt2api :3001<br/>停用"]
    end

    UI -->|"cookie"| L2
    L2 -->|"newapi / openai-* / chat"| L3
    RT -->|"video / upscale / qwen* image"| W
    RT -->|"unc / grok* / 3.8 / sdxl* / tts"| D

    L1 ~~~ L2 ~~~ L3 ~~~ W ~~~ D
```

**分流明細**：video→h3.c｜upscale→FlashVSR｜qwen* image→sd.cpp｜sdxl*/aux→SDXL daemon｜
qwen3.8-uncensored→omlx :8082（模型名重寫為目錄全名）｜grok*→grok2api｜qwen3.8-27b→本地 MLX :8000

**引擎備註**：qwen3.6-27b 已刪除（2026-09-28，模型棄用）；iris.c 已停用（flux-klein-9b 目錄失蹤，
重下 ~30GB 可恢復）；chatgpt2api 停用待帳號；omlx / SDXL daemon 無 launchd，重啟機器後手動拉起
