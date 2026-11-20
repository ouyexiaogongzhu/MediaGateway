"""OpenAI-compatible image face for 影策 (openai-image).

影策 adapter (backend/internal/protocol/builtin.go):
  openai-image  POST /v1/images/generations  -> {data:[{b64_json}]}   (sync)
We run the local worker through the normal scheduler and wait synchronously —
the image task is short (~70s) and the caller is local.
"""
from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import os
import shutil
import socket
import struct
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from . import core

router = APIRouter()

# Must outlast the worker's own timeout (mmh3_image: 900s per job).
# A 504 here abandons a job the worker is still running, and for images it drops
# the temp dir whose ref PNGs the still-queued jobs have to read — so they fail
# instantly on "load image ... failed" instead of doing their own work.
# ponytail: duplicated constant; bump it with the worker timeout if that changes.
_WAIT_TIMEOUT = 1800 + 120


def _ssrf_check(url: str) -> None:
    """User-supplied fetch URLs: reject LAN / link-local (169.254.169.254
    metadata) / reserved targets. Loopback stays allowed by design — the whole
    flow is local (影策 canvas :8090, SDXL daemon :8187, gateway /files).
    ponytail: resolve-then-fetch is TOCTOU; pin the connection if that matters."""
    if os.environ.get("MG_FETCH_ALLOW_PRIVATE"):
        return
    host = urllib.parse.urlparse(url).hostname or ""
    try:
        addrinfos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        raise HTTPException(400, f"cannot resolve image host: {host}")
    for ai in addrinfos:
        ip = ipaddress.ip_address(ai[4][0])
        if (ip.is_private and not ip.is_loopback) or ip.is_reserved or ip.is_multicast:
            raise HTTPException(400, f"refusing to fetch non-public address: {host}")


class _GuardedRedirect(urllib.request.HTTPRedirectHandler):
    """A public URL must not be allowed to redirect into internal targets."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _ssrf_check(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_GuardedRedirect)


def _fetch_bytes(url: str, timeout: float) -> bytes:
    _ssrf_check(url)
    with _OPENER.open(url, timeout=timeout) as r:
        return r.read()


def _post_json(url: str, body: dict, timeout: float,
               headers: dict | None = None) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json",
                                          **(headers or {})})
    with _OPENER.open(req, timeout=timeout) as resp:
        return json.loads(resp.read())


async def _wait_job(job_id: str, timeout: float = _WAIT_TIMEOUT) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = core.get_job(job_id)
        if job is None:
            raise HTTPException(500, "job vanished")
        if job["status"] in ("completed", "failed", "cancelled"):
            if job["status"] != "completed":
                raise HTTPException(500, job.get("error") or f"job {job['status']}")
            return job
        # async 轮询：不占 threadpool 线程（同步版几个并发就占满 40 线程池）
        await asyncio.sleep(1)
    raise HTTPException(504, f"job {job_id} did not finish in {timeout}s "
                             f"(still running; poll GET /v1/jobs/{job_id})")


class ImageGenIn(BaseModel):
    model: Optional[str] = None
    prompt: str
    size: Optional[str] = None
    n: int = 1
    seed: Optional[int] = None
    variant: Optional[str] = None  # 9b | 9b-base
    # 分鏡草稿快車道：畫布=請求尺寸（px²，如 1024x576 → 589824），不吃 2048² 放大政策
    min_generation_area: Optional[int] = None


def _parse_size(size: Optional[str]) -> tuple[int, int]:
    """H3-Image 工作範圍：32 倍數、單邊 512-6400（worker 同規則，2048² 起跳）。"""
    width, height = 2048, 2048
    if size:
        try:
            w, h = size.lower().replace("×", "x").split("x", 1)
            width, height = int(w), int(h)
        except ValueError:
            raise HTTPException(400, "size must be WxH")
        width -= width % 32
        height -= height % 32
        if width < 512 or height < 512:
            raise HTTPException(400, "resolution too small (min side 512)")
        if max(width, height) > 6400:
            raise HTTPException(400, "resolution too large (max side 6400)")
    return width, height


# 生圖模型名守門（匹配 h3-image/mmh3 而非裸 mmh3 的 CHAT 名誤吞由端點隔離保證：
# /v1/images 只收生圖模型名）
_IMAGE_MODEL_KEYS = ("h3-image", "mmh3")


def _is_image_model(model: Optional[str]) -> bool:
    return bool(model) and any(k in model.lower() for k in _IMAGE_MODEL_KEYS)


@router.post("/v1/images/generations")
async def images_generations(req: ImageGenIn):
    n = max(1, min(int(req.n or 1), 4))
    width, height = _parse_size(req.size)
    # grok2api（:8402）：OpenAI 兼容直傳（Bearer = 網關客戶端密鑰）
    if req.model and req.model.lower().startswith("grok"):
        base = os.environ.get("GROK2API_URL", "http://127.0.0.1:8402")
        key = os.environ.get("GROK_API_KEY", "")
        body = {"model": "grok-imagine-image", "prompt": req.prompt, "n": 1}
        try:
            d = await asyncio.to_thread(
                _post_json, base + "/v1/images/generations", body, 600,
                {"Authorization": f"Bearer {key}"} if key else None)
        except urllib.error.HTTPError as e:
            return JSONResponse(status_code=e.code, content={
                "error": {"message": f"grok2api: {e.read()[:200].decode('utf-8', 'replace')}",
                          "type": "upstream_error"}})
        except urllib.error.URLError as e:
            return JSONResponse(status_code=502, content={
                "error": {"message": f"grok2api 不可达：{e}", "type": "upstream_error"}})
        return {"created": int(time.time()), "data": d.get("data", [])}
    # 生圖路由（model 含 h3-image/mmh3）：mmh3_image worker（Metal，16MP 級）
    if _is_image_model(req.model):
        data = []
        for i in range(n):
            params = {"prompt": req.prompt, "width": width, "height": height}
            if req.seed is not None:
                params["seed"] = req.seed + i
            if req.min_generation_area:
                params["min_generation_area"] = req.min_generation_area
            job = await _wait_job(core.create_job("mmh3_image", params)["id"])
            with open(job["output_path"], "rb") as f:
                data.append({"b64_json": base64.b64encode(f.read()).decode()})
        return {"created": int(time.time()), "data": data}
    return JSONResponse(status_code=501, content={
        "error": {"message": "qwen-image/krea2/sdxl/iris 已退役；生圖請用 model=h3-image",
                  "type": "invalid_request_error"}})


_MAGIC_EXTS = ((b"\x89PNG", "png"), (b"\xff\xd8", "jpg"), (b"P5", "ppm"), (b"P6", "ppm"))


def _save_ref(idx: int, data: bytes, tmpdir: str) -> str:
    """iris 只读 8-bit 非隔行 PNG/JPEG/PPM；其余（webp/heic/16bit/隔行...）转成 PNG。"""
    for magic, ext in _MAGIC_EXTS:
        if data.startswith(magic):
            if ext == "png" and (len(data) < 29 or data[24] != 8 or data[28] != 0):
                break  # IHDR: bit depth / interlace unsupported by iris -> normalize
            p = Path(tmpdir) / f"ref_{idx}.{ext}"
            p.write_bytes(data)
            return str(p)
    src = Path(tmpdir) / f"ref_{idx}.raw"
    src.write_bytes(data)
    dst = Path(tmpdir) / f"ref_{idx}.png"
    try:
        proc = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(src), "-pix_fmt", "rgb24", "-y", str(dst)],
            capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        proc = None
    if proc is None or proc.returncode != 0 or not dst.is_file():
        raise HTTPException(400, f"reference image {idx}: unsupported format")
    return str(dst)


@router.post("/v1/images/edits")
async def images_edits(request: Request):
    """影策带参考图生图，双协议端点：
    - multipart（服务端 openai-image 路径）：Form + UploadFile → mmh3_image
    - JSON（影策 openai-images 声明式插件）：{model,prompt,images:[{image_url:dataURL}],...}
    """
    import logging as _logging
    _log = _logging.getLogger(__name__)
    ctype = request.headers.get("content-type", "")
    raw_body = await request.body()
    mask_data: Optional[bytes] = None
    min_generation_area = None
    if ctype.startswith("application/json"):
        payload = await request.json()
        prompt = str(payload.get("prompt") or "")
        model = payload.get("model") or None
        size = payload.get("size") or None
        seed = payload.get("seed")
        seed = int(seed) if seed not in (None, "") else None
        n = int(payload.get("n") or 1)
        min_generation_area = payload.get("min_generation_area")
        images = []
        for it in payload.get("images") or []:
            du = str((it.get("image_url") if isinstance(it, dict) else it) or "")
            if du.startswith("data:"):
                images.append((base64.b64decode(du.split(",", 1)[1]), "ref.png"))
            elif du.startswith("http"):
                # 影策 PurposeProvider：素材以画布签名 URL 形式传入，网关直接拉取
                try:
                    images.append((await asyncio.to_thread(_fetch_bytes, du, 120), "ref.png"))
                except Exception as e:
                    raise HTTPException(400, f"fetch image_url failed: {str(e)[:120]}")
            else:
                _log.warning("images/edits: unsupported image_url: %.160r", du)
    else:
        try:
            form = await request.form()
        except Exception as e:
            _log.warning("images/edits multipart parse failed: %s; ct=%r; body head=%.600r",
                         e, ctype, raw_body[:600])
            raise HTTPException(400, f"multipart parse failed: {str(e)[:120]}; "
                                     f"body head: {raw_body[:300].decode('utf-8', 'replace')}")
        _log.warning("images/edits form keys=%s ct=%r", list(form.keys()), ctype)
        prompt = str(form.get("prompt") or "")
        model = form.get("model") or None
        size = form.get("size") or None
        seed = form.get("seed")
        seed = int(seed) if seed not in (None, "") else None
        n = int(form.get("n") or 1)
        min_generation_area = form.get("min_generation_area")
        images = []
        for k, v in form.multi_items():
            # 影策 openai-images 插件用字段名 images（复数）；OpenAI 官方约定是 image。
            # 值可能是：文件、data URL、或画布签名素材 URL（http://…/api/public/resources/…）
            if k not in ("image", "images"):
                continue
            if hasattr(v, "read"):
                images.append((await v.read(), v.filename or "ref.png"))
            elif isinstance(v, str):
                t = v.strip()
                if t.startswith("data:"):
                    try:
                        images.append((base64.b64decode(t.split(",", 1)[1]), "ref.png"))
                    except Exception:
                        _log.warning("images/edits: bad data URL in %s", k)
                elif t.startswith("http"):
                    try:
                        images.append((await asyncio.to_thread(_fetch_bytes, t, 120), "ref.png"))
                    except Exception as e:
                        _log.warning("images/edits: fetch %s url failed: %s", k, str(e)[:150])
                else:
                    _log.warning("images/edits: unsupported %s value: %.120r", k, t)
        mask_part = form.get("mask")
        mask_data = await mask_part.read() if hasattr(mask_part, "read") else None

    if not images:
        raise HTTPException(400, f"at least one input image is required (parts: {list(form.keys()) if not ctype.startswith('application/json') else list(payload.keys())})")
    if len(images) > 16:
        raise HTTPException(400, "at most 16 reference images (iris MAX_INPUT_IMAGES)")
    for data, fname in images:
        if len(data) > 20 * 1024 * 1024:
            raise HTTPException(413, f"reference image too large (>20MB): {fname}")
    if mask_data is not None:
        raise HTTPException(400, "mask is not supported by the local image model")
    if not prompt.strip():
        raise HTTPException(400, "prompt is required")
    n = max(1, min(n or 1, 4))
    # 生圖路由（model 含 h3-image/mmh3）：图+文编辑（--reference 可重複至 16），走 mmh3_image worker
    if _is_image_model(model):
        width, height = _parse_size(size)
        tmpdir = tempfile.mkdtemp(prefix="mg_edits_h3img_")
        try:
            refs = [_save_ref(i, data, tmpdir) for i, (data, _) in enumerate(images)]
            imgs = []
            for i in range(n):
                params = {"prompt": prompt, "width": width, "height": height, "refs": refs}
                if seed is not None:
                    params["seed"] = seed + i
                if min_generation_area not in (None, ""):
                    params["min_generation_area"] = int(min_generation_area)
                job = await _wait_job(core.create_job("mmh3_image", params)["id"])
                with open(job["output_path"], "rb") as f:
                    imgs.append({"b64_json": base64.b64encode(f.read()).decode()})
            return {"created": int(time.time()), "data": imgs}
        finally:
            # refs 要活到最后一个 job 结束，rmtree 只能在循环之后
            shutil.rmtree(tmpdir, ignore_errors=True)
    # qwen-image/krea2/sdxl/iris/aux 已退役（2026-11-17）；參考圖編輯走 model=h3-image
    return JSONResponse(status_code=501, content={
        "error": {"message": "sdxl/iris/aux 已退役；參考圖編輯請用 model=h3-image",
                  "type": "invalid_request_error"}})


def _wav_duration(path: str) -> float:
    """Duration from a RIFF/WAVE header (same parse as workers/music._wav_info)."""
    with open(path, "rb") as f:
        head = f.read(12)
        if head[:4] != b"RIFF" or head[8:12] != b"WAVE":
            raise ValueError(f"not a WAV: {path}")
        rate = channels = bits = None
        data_size = 0
        while True:
            hdr = f.read(8)
            if len(hdr) < 8:
                break
            cid, size = hdr[:4], struct.unpack("<I", hdr[4:])[0]
            if cid == b"fmt ":
                fmt = f.read(size)
                channels, rate = struct.unpack("<HI", fmt[2:8])
                bits = struct.unpack("<H", fmt[14:16])[0]
            elif cid == b"data":
                data_size = size
                f.seek(size, os.SEEK_CUR)
            else:
                f.seek(size + (size & 1), os.SEEK_CUR)
    if not (rate and channels and bits and data_size):
        raise ValueError(f"incomplete WAV header: {path}")
    return round(data_size / (rate * channels * bits // 8), 2)


# TTS 已全退役（tts_qwen worker 2026-11-17 刪）：/v1/audio/speech 端點一併移除，
# 影策的 openai-audio 渠道 enum 保留但 dormant（enabled=0，無 caller）。

# --- music face (ACE-Step via the scheduler; see workers/music.py) ---

class MusicIn(BaseModel):
    prompt: str
    duration_s: float = 45


@router.post("/v1/music")
async def music_generate(req: MusicIn, request: Request):
    if not req.prompt.strip():
        raise HTTPException(400, "prompt is required")
    if not 10 <= req.duration_s <= 600:  # music worker supported range
        raise HTTPException(400, "duration_s must be 10-600")
    job = await _wait_job(core.create_job(
        "music", {"prompt": req.prompt, "duration_s": req.duration_s})["id"])
    base = str(request.base_url).rstrip("/")
    try:
        duration = _wav_duration(job["output_path"])
    except Exception:  # noqa: BLE001
        duration = None
    return {"url": f"{base}/v1/music/jobs/{job['id']}/content",
            "duration_seconds": duration}


@router.get("/v1/music/jobs/{job_id}/content")
def music_content(job_id: str):
    job = core.get_job(job_id)
    if not job:
        raise HTTPException(404, "music not found")
    op = job["output_path"]
    if job["status"] != "completed" or not op or not os.path.isfile(op):
        raise HTTPException(409, "music not ready")
    return FileResponse(op, media_type="audio/wav")
