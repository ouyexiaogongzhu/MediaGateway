"""OpenAI chat-completions face for the local qwen LLM (server/llm.py lifecycle).

POST /v1/chat/completions — non-streaming only. While a video/shot job is
running on the GPU the LLM is not admitted: 503 with a retryable message
(front-end retries later; no long blocking wait).
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time

import httpx
from fastapi import APIRouter
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
from typing import List, Optional

from . import core, llm

router = APIRouter()

# OpenAI 兼容模型清單：影策「拉取模型目錄」與任意 OpenAI 客戶端探測用
_KNOWN_MODELS = [
    "sora-2",            # video（newapi videos 協議）
    "grok-4.7",          # chat（grok2api :8402）
    "qwen3.8-27b",       # chat（本地 MLX）
    "qwen3.8-uncensored",  # chat（本地 MLX，pyros-vault oQ4e-mtp 無審查）
    "qwen3-tts",         # tts（mlx-audio）
    "cosyvoice",         # tts（cosyvoice 音色庫：system/suwan/aila，voice 參數選）
    "iris-image",        # image（本地 iris/sdxl）
    "sdxl-noobai", "sdxl-realvis",  # image（本地 sdxl）
    "qwen-image-2.1",    # image（sd.cpp Metal，GGUF）
]


@router.get("/v1/models")
def list_models():
    return {"object": "list", "data": [
        {"id": m, "object": "model", "owned_by": "mediagateway"} for m in _KNOWN_MODELS
    ]}

UPSTREAM_TIMEOUT_S = 900.0  # cold load of the 19GB qwen takes minutes; don't 502 mid-load

# 云端供应商路由（model 前缀 → 本地 api2 守护进程）。未命中 → 本地 qwen MLX。
_PROVIDERS = [
    ("grok", "http://127.0.0.1:8402", os.environ.get("GROK_API_KEY", "")),
    ("qwen3.8-uncensored", "http://127.0.0.1:8082", ""),  # omlx（pyros-vault oQ4e-mtp；mtplx 對此量化輸出亂碼）
]

# 供應商側模型名重寫（影策 key → 上游 omlx 目錄 id）
_MODEL_REWRITE = {"qwen3.8-uncensored": "pyros-vault_Qwen3.8-27B-Uncensored-oQ4e-mtp"}

# omlx 按需供應商：pyros-vault oQ4e-mtp 只在此服務（mtplx 對該量化輸出亂碼）
OMLX_BASE = "http://127.0.0.1:8082"
OMLX_IDLE_EXIT_S = float(os.environ.get("OMLX_IDLE_EXIT_S", "120"))  # 對齊 qwen3.8 的 120s
OMLX_MEM_GB = 16.0
MEM_GB = OMLX_MEM_GB  # 調度器 face：core._resident_gb 按此計入預算
_omlx_lock = threading.Lock()
_omlx_proc = None
_omlx_last_used = 0.0
_omlx_watchdog_started = False
_omlx_busy = 0           # chat 在飛——watchdog 絕不殺
_omlx_spawning = False   # ensure() 拉起中——計入 resident
_omlx_unload_pending = False  # core 為 video 要內存；下一個非 busy tick 開殺


def busy() -> bool:
    """chat 在飛或正在拉起——調度器視為不可殺。"""
    return _omlx_busy > 0 or _omlx_spawning


def resident() -> bool:
    """16GB 佔用中（進程活著或在拉起）——調度器預算感知。"""
    p = _omlx_proc
    return (p is not None and p.poll() is None) or _omlx_spawning


def request_unload() -> None:
    """core 為 video/shot 讓路：下一個非 busy tick 殺掉自拉進程。"""
    global _omlx_unload_pending
    with _omlx_lock:
        _omlx_unload_pending = True


def _omlx_busy_enter():
    global _omlx_busy, _omlx_last_used
    with _omlx_lock:
        _omlx_busy += 1
        _omlx_last_used = time.time()


def _omlx_busy_exit():
    global _omlx_busy, _omlx_last_used
    with _omlx_lock:
        _omlx_busy = max(0, _omlx_busy - 1)
        _omlx_last_used = time.time()


def _omlx_up() -> bool:
    """裸 socket 探活——urllib 會吃 macOS 系統代理配置，localhost 也可能被劫持。"""
    import socket
    s = socket.socket()
    s.settimeout(2)
    try:
        return s.connect_ex(("127.0.0.1", 8082)) == 0
    finally:
        s.close()


def _omlx_watchdog():
    """qwen3.8(llm.py) 同款 idle-exit：只殺 gateway 自拉的進程；busy/unload_pending 優先。"""
    global _omlx_proc, _omlx_unload_pending
    while True:
        time.sleep(15)
        with _omlx_lock:
            p = _omlx_proc
            if p is None or p.poll() is not None:
                _omlx_unload_pending = False
                continue
            due = _omlx_unload_pending or time.time() - _omlx_last_used > OMLX_IDLE_EXIT_S
            if _omlx_busy > 0 or not due:
                continue
            _omlx_proc = None
            _omlx_unload_pending = False
        try:
            p.terminate()
            p.wait(timeout=10)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass
        print(f"[compat_chat] omlx exited (idle/unload → 用完即关)", flush=True)


def _ensure_omlx():
    """omlx :8082 按需拉起：探活成功即返回；失敗才 spawn 並等就緒。
    閒置 OMLX_IDLE_EXIT_S（或 video 請求讓路）由 watchdog 殺掉，冷啟 ~9s。"""
    global _omlx_proc, _omlx_last_used, _omlx_watchdog_started, _omlx_spawning
    with _omlx_lock:
        _omlx_last_used = time.time()
        if not _omlx_watchdog_started:
            _omlx_watchdog_started = True
            threading.Thread(target=_omlx_watchdog, daemon=True).start()
        if _omlx_up():
            return
        if _omlx_unload_pending and _omlx_proc is not None:
            raise RuntimeError("omlx unloading (video 互斥讓路中)，請稍後重試")
        _omlx_spawning = True
    try:
        if _omlx_up():  # lock 外 double-check
            return
        with _omlx_lock:
            _omlx_last_used = time.time()
            proc = subprocess.Popen(
                ["/opt/homebrew/opt/omlx/bin/omlx", "serve",
                 "--model-dir", "/Users/vincent/tool/qwen/models",
                 "--port", "8082", "--memory-guard", "safe"],
                stdout=open("/tmp/omlx_8082.log", "a"), stderr=subprocess.STDOUT,
                start_new_session=True)
            _omlx_proc = proc
        deadline = time.time() + 300
        while time.time() < deadline:
            if _omlx_up():
                return
            time.sleep(2)
        raise RuntimeError("omlx :8082 未能在 300s 內就緒 (log: /tmp/omlx_8082.log)")
    finally:
        _omlx_spawning = False


def _local_key(model) -> str:
    """本地 MLX 模型選型：非供應商前綴一律回退 qwen3.8-27b。"""
    return llm.DEFAULT_MODEL


def _route(model):
    m = (model or "").lower()
    for prefix, base, key in _PROVIDERS:
        if m.startswith(prefix):
            return base, key
    return None, None


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    model: Optional[str] = None
    messages: List[ChatMessage]
    stream: bool = False
    max_tokens: Optional[int] = None
    temperature: Optional[float] = None
    enable_thinking: Optional[bool] = None


def _upstream_error(message: str, status: int = 502) -> JSONResponse:
    return JSONResponse(status_code=status, content={
        "error": {"message": message, "type": "upstream_error"}})


def _video_running() -> bool:
    rows = core.db().execute(
        "SELECT 1 FROM jobs WHERE status='running' AND type IN ('video','shot') LIMIT 1"
    ).fetchall()
    return bool(rows)


def _busy_response() -> JSONResponse:
    return JSONResponse(status_code=503, content={
        "error": {"message": "视频生成中，LLM 排队稍后重试", "type": "busy",
                  "retryable": True}})


@router.post("/v1/chat/completions")
def chat_completions(req: ChatRequest):
    body = req.model_dump(exclude_none=True)
    base, key = _route(req.model)
    if base is None:
        # 本地 qwen MLX：按需拉起 + 内存互斥（19GB LLM 与 35GB 视频引擎互斥）
        body["model"] = _local_key(req.model)  # 轉發名與常駐服務一致（qwen3.6 等舊名回退 3.8）
        if _video_running():
            return _busy_response()
        try:
            llm.ensure(_local_key(req.model))
        except RuntimeError as e:
            return _upstream_error(f"LLM 不可用：{e}")
        # re-check after the (possibly 10s+) spawn: a video job may have been
        # admitted meanwhile — loading 19GB + 35GB together would OOM
        if _video_running():
            return _busy_response()
        body.setdefault("enable_thinking", False)
        body.pop("stream", None)  # MLX 服務不支持流式：非流式取結果，外層模擬 SSE
        with llm.busy_guard():
            try:
                r = httpx.post(f"{llm.BASE_URL}/v1/chat/completions", json=body,
                               timeout=UPSTREAM_TIMEOUT_S)
            except httpx.HTTPError as e:
                return _upstream_error(f"LLM upstream unreachable: {e}")
    else:
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        m = body.get("model") or ""
        body["model"] = _MODEL_REWRITE.get(m.lower(), m)  # 路由大小寫不敏感，重寫也必須是
        if base == OMLX_BASE:
            _ensure_omlx()  # 按需：探活失敗才 spawn（19GB 進程不常駐）
            _omlx_busy_enter()  # chat 在飛——watchdog 不殺
        if req.stream:
            # 供應商（grok2api/omlx）支持 SSE：逐塊透傳。
            # 上游中途出錯時 HTTP 已是 200，錯誤 JSON 會以原文出現在流裡，由前端解析。
            def sse():
                try:
                    with httpx.stream("POST", f"{base}/v1/chat/completions",
                                      json=body, headers=headers,
                                      timeout=UPSTREAM_TIMEOUT_S) as r:
                        if r.status_code != 200:
                            yield r.read()
                            return
                        for chunk in r.iter_raw():
                            yield chunk
                except httpx.HTTPError as e:
                    # 連不上/中途斷：headers 已發（200），只能把錯誤 JSON 塞進流裡
                    yield json.dumps({"error": {"message": f"供应商不可达：{e}",
                                                "type": "upstream_error"}}).encode()
                finally:
                    if base == OMLX_BASE:
                        _omlx_busy_exit()
            return StreamingResponse(sse(), media_type="text/event-stream")
        try:
            r = httpx.post(f"{base}/v1/chat/completions", json=body,
                           timeout=UPSTREAM_TIMEOUT_S, headers=headers)
        except httpx.HTTPError as e:
            return _upstream_error(f"供应商不可达：{e}")
        finally:
            if base == OMLX_BASE:
                _omlx_busy_exit()
    try:
        payload = r.json()
    except ValueError:
        return _upstream_error(f"上游返回非 JSON ({r.status_code})")

    # 本地 qwen MLX 不支持上游流式：非流式取回結果後模擬 OpenAI SSE 一次性回放，
    # 讓畫布的 stream=true 請求（分鏡等）也能吃到標準格式
    if req.stream and base is None and r.status_code == 200:
        content = ""
        try:
            content = payload["choices"][0]["message"].get("content") or ""
        except (KeyError, IndexError, TypeError):
            pass

        def sse():
            first = {"choices": [{"delta": {"role": "assistant", "content": content},
                                  "finish_reason": None, "index": 0}]}
            yield f"data: {json.dumps(first, ensure_ascii=False)}\n\n"
            done = {"choices": [{"delta": {}, "finish_reason": "stop", "index": 0}]}
            yield f"data: {json.dumps(done, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(sse(), media_type="text/event-stream")
    return JSONResponse(status_code=r.status_code, content=payload)
