# 影策系統架構（2026-09-28）

> 按 Worker 分流：畫布 → 影策 → Gateway（兼容面 → Queue）→ 7 個 Worker → 各自的引擎/模型。
> qwen3.6-27b 已刪除；iris-image 已停用（被 qwen-image 頂替）；chatgpt2api 停用待帳號。

```mermaid
%%{init:{"theme":"base","themeVariables":{"fontSize":"14px"}}}%%
flowchart LR
    UI["🖥 影策 Web :3000<br/>分鏡 · 生圖 · 視頻 · 超分 · 音頻"]
    Y["🎬 影策 Backend :8090<br/>渠道/模型目錄 · 任務系統<br/>Asset Store · /api/tools/upscale"]
    GW["🚪 MediaGateway :8600<br/>OpenAI/newapi 兼容面<br/>Job Queue + Scheduler<br/>FIFO · MEM_GB 預算 · LLM↔視頻互斥<br/>stream：供應商透傳/本地模擬"]

    V["video<br/>── h3.c worker ──<br/>引擎：MiniMax-H3（唯一）<br/>草稿 3.7min/5s · 15s ~15min"]
    U["upscale<br/>── flashvsr worker ──<br/>FlashVSR（唯一超分）<br/>15s→1080 ~8.5min · NO_MASK"]
    I["image<br/>── qwen_image worker ──<br/>sd.cpp Metal GGUF<br/>Qwen-Image-2.1 ~1.5min"]
    C["chat 本地<br/>── qwen MLX :8000 ──<br/>qwen3.8-27b<br/>LLM↔視頻互斥 · idle 120s"]
    CU["chat 無審查<br/>── omlx :8082 ──<br/>qwen3.8-uncensored<br/>oQ4e-mtp（mtplx 不兼容）"]
    GK["chat/圖 外部<br/>── grok2api :8402 ──<br/>grok-chat-fast（web 帳號池）"]
    A["audio<br/>── mlx-audio / cosyvoice ──<br/>qwen3-tts · C001 · C002"]
    X["image/aux<br/>── SDXL daemon :8187 ──<br/>sdxl-noobai · sdxl-realvis<br/>aux 四件套"]

    classDef once fill:#dbeafe,stroke:#3b82f6
    classDef daemon fill:#dcfce7,stroke:#16a34a
    class V,U,I once
    class C,CU,GK,A,X daemon

    UI ==>|"cookie / 任務"| Y ==>|"兼容 REST"| GW
    GW --> V & U & I & C & CU & GK & A & X
```

**常駐守護運維**：omlx（run-aeon.sh）/ SDXL daemon 無 launchd——重啟機器後手動拉起；
grok2api 有 launchd；qwen MLX :8000 由 Gateway llm.py 按需拉起。
**已棄用**：qwen3.6-27b（刪除）、iris-image（enabled=0，重下 flux-klein-9b 可恢復）、chatgpt2api（待帳號）、SeedVR2/LTX（已刪）。
