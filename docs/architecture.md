# 影策系統架構（2026-09-28）

> 按功能合併：畫布 → 影策 → Gateway → 5 大能力域，每域列舉 worker/守護與模型。
> 已棄用：qwen3.6-27b（刪除）、iris-image（停用）、chatgpt2api（停用待帳號）、SeedVR2/LTX（刪除）。

```mermaid
%%{init:{"theme":"base","themeVariables":{"fontSize":"14px"}}}%%
flowchart LR
    UI["🖥 影策 Web :3000<br/>分鏡 · 生圖 · 視頻 · 超分 · 音頻"]
    Y["🎬 影策 Backend :8090<br/>渠道/模型目錄 · 任務系統<br/>Asset Store · /api/tools/upscale"]
    GW["🚪 MediaGateway :8600<br/>OpenAI/newapi 兼容面<br/>Job Queue + Scheduler<br/>FIFO · MEM_GB 預算 · LLM↔視頻互斥<br/>stream：供應商透傳/本地模擬"]

    VIDEO["🎬 視頻（用完即關）<br/>── h3.c worker ──<br/>MiniMax-H3 唯一引擎<br/>32網格 · 5+17n · 草稿3.7min/5s"]
    UP["🔍 超分（用完即關）<br/>── flashvsr worker ──<br/>FlashVSR 唯一超分<br/>15s→1080 ~8.5min · NO_MASK"]
    IMG["🖼 生圖+aux<br/>── qwen_image worker（用完即關）──<br/>sd.cpp Metal → Qwen-Image-2.1 ~1.5min<br/>── SDXL daemon :8187（常駐）──<br/>sdxl-noobai · sdxl-realvis · aux 四件套"]
    TXT["💬 文本（3 路守護）<br/>── qwen MLX :8000（按需/idle 120s）──<br/>qwen3.8-27b<br/>── omlx :8082（常駐）──<br/>qwen3.8-uncensored（oQ4e-mtp）<br/>── grok2api :8402（launchd）──<br/>grok-chat-fast（web 帳號池）"]
    AUD["🔊 音頻（按需/守護）<br/>── mlx-audio ── qwen3-tts<br/>── cosyvoice ── C001 · C002"]

    classDef once fill:#dbeafe,stroke:#3b82f6
    classDef daemon fill:#dcfce7,stroke:#16a34a
    class VIDEO,UP once
    class IMG,TXT,AUD daemon

    UI ==>|"cookie / 任務"| Y ==>|"兼容 REST"| GW
    GW --> VIDEO & UP & IMG & TXT & AUD
```

**分流明細**：/v1/videos→h3.c｜/v1/upscale→FlashVSR｜/v1/images qwen-image*→sd.cpp、
sdxl*→SDXL daemon｜/v1/chat qwen3.8-27b→MLX、qwen3.8-uncensored→omlx（名稱重寫）、grok*→grok2api｜
/v1/audio qwen3-tts→mlx-audio、C001/C002→cosyvoice

**已棄用**：iris-image（enabled=0，重下 flux-klein-9b 可恢復）、qwen3.6-27b、chatgpt2api（待帳號）、SeedVR2/LTX
