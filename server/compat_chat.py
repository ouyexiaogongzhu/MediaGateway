"""OpenAI chat-completions face for the local qwen LLM (server/llm.py lifecycle).

POST /v1/chat/completions — non-streaming only. While a video/shot job is
running on the GPU the LLM is not admitted: 503 with a retryable message
(front-end retries later; no long blocking wait).
"""
from __future__ import annotations

import json
import os

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
    "gpt-5",             # chat（chatgpt2api :3001）
    "chatgpt",           # chat（chatgpt2api 別名）
    "qwen3.8-27b",       # chat（本地 MLX）
    "qwen3-tts",         # tts（mlx-audio）
    "C001", "C002",      # tts（cosyvoice 音色）
    "iris-image",        # image（本地 iris/sdxl）
    "sdxl-noobai", "sdxl-realvis",  # image（本地 sdxl）
]


@router.get("/v1/models")
def list_models():
    return {"object": "list", "data": [
        {"id": m, "object": "model", "owned_by": "mediagateway"} for m in _KNOWN_MODELS
    ]}

UPSTREAM_TIMEOUT_S = 900.0  # cold load of the 19GB qwen takes minutes; don't 502 mid-load

# 云端供应商路由（model 前缀 → 本地 api2 守护进程）。未命中 → 本地 qwen MLX。
_PROVIDERS = [
    ("gpt-5", "http://127.0.0.1:3001", os.environ.get("CHATGPT2API_KEY", "local-chatgpt2api")),
    ("chatgpt", "http://127.0.0.1:3001", os.environ.get("CHATGPT2API_KEY", "local-chatgpt2api")),
    ("grok", "http://127.0.0.1:8402", os.environ.get("GROK_API_KEY", "")),
]


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
        if _video_running():
            return _busy_response()
        try:
            llm.ensure()
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
        if req.stream:
            # 供應商（grok2api/chatgpt2api）支持 SSE：逐塊透傳。
            # 上游中途出錯時 HTTP 已是 200，錯誤 JSON 會以原文出現在流裡，由前端解析。
            def sse():
                with httpx.stream("POST", f"{base}/v1/chat/completions",
                                  json=body, headers=headers,
                                  timeout=UPSTREAM_TIMEOUT_S) as r:
                    if r.status_code != 200:
                        yield r.read()
                        return
                    for chunk in r.iter_raw():
                        yield chunk
            return StreamingResponse(sse(), media_type="text/event-stream")
        try:
            r = httpx.post(f"{base}/v1/chat/completions", json=body,
                           timeout=UPSTREAM_TIMEOUT_S, headers=headers)
        except httpx.HTTPError as e:
            return _upstream_error(f"供应商不可达：{e}")
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
