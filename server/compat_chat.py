"""OpenAI chat-completions face for the local qwen LLM (server/llm.py lifecycle).

POST /v1/chat/completions — non-streaming only. While a video/shot job is
running on the GPU the LLM is not admitted: 503 with a retryable message
(front-end retries later; no long blocking wait).
"""
from __future__ import annotations

import os

import httpx
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import List, Optional

from . import core, llm

router = APIRouter()

UPSTREAM_TIMEOUT_S = 900.0  # cold load of the 19GB qwen takes minutes; don't 502 mid-load

# 云端供应商路由（model 前缀 → 本地 api2 守护进程）。未命中 → 本地 qwen MLX。
_PROVIDERS = [
    ("doubao", "http://127.0.0.1:8401", os.environ.get("DOUBAO_API_KEY", "")),
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
    if req.stream:
        return JSONResponse(status_code=400, content={
            "error": {"message": "stream=true 不支持，请用非流式请求", "type": "invalid_request_error"}})
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
        with llm.busy_guard():
            try:
                r = httpx.post(f"{llm.BASE_URL}/v1/chat/completions", json=body,
                               timeout=UPSTREAM_TIMEOUT_S)
            except httpx.HTTPError as e:
                return _upstream_error(f"LLM upstream unreachable: {e}")
    else:
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        try:
            r = httpx.post(f"{base}/v1/chat/completions", json=body,
                           timeout=UPSTREAM_TIMEOUT_S, headers=headers)
        except httpx.HTTPError as e:
            return _upstream_error(f"供应商不可达：{e}")
    try:
        payload = r.json()
    except ValueError:
        return _upstream_error(f"上游返回非 JSON ({r.status_code})")
    return JSONResponse(status_code=r.status_code, content=payload)
