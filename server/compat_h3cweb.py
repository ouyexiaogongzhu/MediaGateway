"""h3cweb compat layer: the old :8600 API face served from Gateway jobs.

Mounted by server/main.py. Route registration order matters: the include line
sits before main.py's own /info so the old-format /info wins.

Documented diffs vs the frozen h3cweb server:
- job ids are "video_xxxxxxxx" (Gateway) not "h3_xxxxxxxx"
- /info: the video worker unloads the engine after every job, so device/model/
  cache are null / placeholder unless a job is running right now
- output lives at MG_ASSETS/<job_id>/output.mp4; a requested output_path is
  resolved against BASE_DIR and echoed back as requested_output_path but no
  file is written there — fetch via /v1/videos/{id}/content or read
  output_path directly. /files serves historical files only.
- missing ref / bad size return clean 4xx (old server crashed with 500 or
  Flask-style tuples)
"""
from __future__ import annotations

import base64
import json
import os
import re
import urllib.parse
import urllib.request
import uuid
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import core
from .workers import video

BASE_DIR = os.environ.get("H3CWEB_COMPAT_BASE_DIR", "/Users/vincent/code/h3cweb")
OUT_DIR = os.environ.get("H3CWEB_COMPAT_OUT_DIR",
                         os.path.join(BASE_DIR, "projects/p001/output/shots"))
REFS_DIR = os.environ.get("H3CWEB_COMPAT_REFS_DIR",
                          os.path.join(BASE_DIR, "projects/p001/output/refs"))

router = APIRouter()


class Ref(BaseModel):
    kind: str  # image | video | audio | video_audio
    path: str
    audio_path: Optional[str] = None
    include_embedded_audio: bool = False


class JobRequest(BaseModel):
    prompt: str
    refs: List[Ref] = []
    width: int = 864
    height: int = 480
    seconds: Optional[float] = None
    frames: Optional[int] = None
    steps: int = 6
    denoise_reuse: int = 1
    dit_layers: int = 45
    core_reuse: int = 1
    token_reduction: bool = False
    seed: Optional[int] = None
    output_path: Optional[str] = None
    ssd_streaming: bool = False
    reference_image_size: Optional[int] = None
    first_frame: Optional[str] = None
    mute_audio: bool = False


def _resolve(path: str) -> str:
    return path if os.path.isabs(path) else os.path.join(BASE_DIR, path)


def _product_path(job_id: str) -> str:
    return str(core.ASSET_ROOT / job_id / "output.mp4")


# old status vocabulary; gateway "cancelled" surfaces as "failed" for legacy callers
_STATUS = {"queued": "queued", "running": "running", "completed": "completed",
           "failed": "failed", "cancelled": "failed"}


@router.get("/info")
def info():
    eng = video._engine
    device = model = cache = None
    if eng is not None:
        try:
            device, model, cache = eng.device(), eng.model(), eng.cache_info()
        except Exception:  # engine mid-load/close; placeholders are fine
            pass
    return {
        "engine": "h3.c",
        "version": "0.1.0-dev",
        "device": device,
        "model": model or {"dir": video.MODEL_DIR, "loaded": eng is not None},
        "cache": cache,
        # gateway extras (old callers ignore unknown keys)
        "engines": {t: {"mem_gb": m.MEM_GB, "module": m.__name__}
                    for t, m in core.registry().items()},
        "budget_gb": core.BUDGET_GB,
        "asset_root": str(core.ASSET_ROOT),
    }


@router.post("/jobs")
def create_job(req: JobRequest):
    d = req.model_dump()
    requested = d.pop("output_path")
    refs = []
    for r in d["refs"]:
        p = _resolve(r["path"])
        if not os.path.isfile(p):
            raise HTTPException(400, f"ref file not found: {r['path']}")
        r["path"] = p
        refs.append(r)
    d["refs"] = refs
    if requested:
        d["requested_output_path"] = _resolve(requested)
    job = core.create_job("video", d)
    out = {"job_id": job["id"], "status": "queued",
           "output_path": _product_path(job["id"])}
    if requested:
        out["requested_output_path"] = d["requested_output_path"]
    return out


@router.get("/jobs/{job_id}")
def get_job(job_id: str):
    job = core.get_job(job_id)
    if not job:
        raise HTTPException(404, "not found")
    return {
        "job_id": job["id"],
        "status": _STATUS.get(job["status"], job["status"]),
        "phase": job["phase"] or ("queued" if job["status"] == "queued" else ""),
        "progress": job["progress"],
        "output_path": job["output_path"] or _product_path(job["id"]),
        "error": job["error"],
        "created_at": job["created_at"],
        "started_at": job["started_at"],
        "finished_at": job["finished_at"],
        "requested_output_path": job["params"].get("requested_output_path"),
    }


@router.get("/files/{name}")
def get_file(name: str):
    """Historical mp4s only; new job output lives in MG_ASSETS/<job_id>."""
    if "/" in name or "\\" in name or ".." in name or not name.endswith(".mp4"):
        raise HTTPException(400, "invalid file name")
    path = os.path.join(OUT_DIR, name)
    if not os.path.isfile(path):
        raise HTTPException(404, "not found")
    return FileResponse(path, media_type="video/mp4")


# --- OpenAI Sora-style shim (open-storyboard-canvas custom provider) ---

_SORA_STATUS = {"queued": "queued", "running": "in_progress",
                "completed": "completed", "failed": "failed", "cancelled": "failed"}

_DATA_URL = re.compile(r"^data:(image|audio)/([a-z0-9.+-]+);base64,(.*)$", re.S)
_DATA_EXT = {"jpeg": ".jpg", "jpg": ".jpg", "png": ".png", "webp": ".webp", "gif": ".gif",
             "mpeg": ".mp3", "mp3": ".mp3", "wav": ".wav", "x-wav": ".wav",
             "ogg": ".ogg", "flac": ".flac", "aac": ".aac", "m4a": ".m4a", "mp4": ".m4a"}


def _save_data_url(text: str, kind: str = "image") -> Optional[str]:
    """Decode a data:image|audio URL to a file h3 can read. Non-matching values are skipped."""
    m = _DATA_URL.match(text.strip())
    if not m or m.group(1) != kind:
        return None
    ext = _DATA_EXT.get(m.group(2).lower(), ".png" if kind == "image" else ".mp3")
    os.makedirs(REFS_DIR, exist_ok=True)
    path = os.path.join(REFS_DIR, f"ref_{uuid.uuid4().hex[:8]}{ext}")
    with open(path, "wb") as f:
        try:
            f.write(base64.b64decode(m.group(3)))
        except (ValueError, base64.binascii.Error) as e:
            raise HTTPException(400, f"bad data URL payload: {e}")
    return path


_MAX_IMAGE_BYTES = 64 * 1024 * 1024


def _looks_like_image(head: bytes) -> bool:
    """Magic bytes: jpeg / png / gif / bmp / webp / isobox(avif,heif)。"""
    return (head[:3] == b"\xff\xd8\xff"
            or head[:8] == b"\x89PNG\r\n\x1a\n"
            or head[:4] in (b"GIF8", b"BM")
            or (head[:4] == b"RIFF" and head[8:12] == b"WEBP")
            or head[4:8] == b"ftyp")


def _fetch_url(url: str) -> str:
    """Download an http(s) image into REFS_DIR (same naming scheme as data URLs).

    scheme 白名单由调用方保证；此处复查重定向终点，限 64MB，验 magic bytes。"""
    ext = os.path.splitext(urllib.parse.urlparse(url).path)[1] or ".png"
    os.makedirs(REFS_DIR, exist_ok=True)
    path = os.path.join(REFS_DIR, f"ref_{uuid.uuid4().hex[:8]}{ext}")
    try:
        with urllib.request.urlopen(url, timeout=30) as r, open(path, "wb") as f:
            final = str(r.geturl())
            if not final.startswith(("http://", "https://")):
                raise ValueError(f"重定向到非 http(s) 协议: {final}")
            head = r.read(16)
            if not _looks_like_image(head):
                raise ValueError(f"内容不是图片 (Content-Type: "
                                 f"{r.headers.get('Content-Type')})")
            f.write(head)
            total = len(head)
            while chunk := r.read(1 << 20):
                total += len(chunk)
                if total > _MAX_IMAGE_BYTES:
                    raise ValueError(f"图片超过 {_MAX_IMAGE_BYTES >> 20}MB 上限")
                f.write(chunk)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"拉取图片失败: {url}: {e}")
    return path


def _localize_image(val) -> str:
    """first_frame_image/input_images entries: data URL | http(s) URL | local path → path."""
    val = str(val).strip()
    path = _save_data_url(val)
    if path:
        return path
    if val.startswith(("http://", "https://")):
        return _fetch_url(val)
    if os.path.isabs(val) and os.path.isfile(val):
        return val
    raise HTTPException(
        400, f"图片引用必须是 data URL、http(s) URL 或已存在的绝对路径: {val[:80]}")


def _as_bool(v) -> bool:
    if isinstance(v, bool):
        return v
    return str(v or "").strip().lower() in ("true", "1", "yes")


def _input_image_values(entries) -> list:
    """input_images: repeated form fields or one JSON-array string → flat paths.
    空条目（None / 空串 / 纯空白，含数组内部）一律跳过。"""
    vals = [e for e in (entries or []) if e is not None]
    if len(vals) == 1 and str(vals[0]).lstrip().startswith("["):
        try:
            vals = json.loads(str(vals[0]))
        except ValueError:
            raise HTTPException(400, "input_images 不是合法的 JSON 数组")
    if not isinstance(vals, list):
        vals = [vals]
    return [_localize_image(v) for v in vals
            if v is not None and str(v).strip()]


import shutil as _shutil
import subprocess as _subprocess


def _shrink_ref_for_h3(path: str, max_side: int = 1536) -> str:
    """h3 的 Qwen3 prefill 序列含参考图 vision tokens（随分辨率暴涨）：
    参考图长边 >1536px 会撑爆 threadgroup memory（实测 2848x1600 直接失败）。
    超限图原地缩到长边 1536（短边按比例取 32 倍数），返回新路径；失败回退原图。"""
    probe_bin = _shutil.which("ffprobe") or "ffprobe"
    try:
        proc = _subprocess.run(
            [probe_bin, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0", path],
            capture_output=True, timeout=30)
    except (OSError, _subprocess.TimeoutExpired):
        return path
    try:
        w_str, h_str = proc.stdout.decode().strip().split(",")[:2]
        w, h = int(w_str), int(h_str)
    except ValueError:
        return path
    if max(w, h) <= max_side:
        return path
    scale = max_side / max(w, h)
    nw = max(32, int(w * scale) // 32 * 32)
    nh = max(32, int(h * scale) // 32 * 32)
    dst = f"{path}.s{nw}x{nh}.png"
    ffmpeg_bin = _shutil.which("ffmpeg") or "ffmpeg"
    try:
        proc = _subprocess.run(
            [ffmpeg_bin, "-v", "error", "-i", path, "-vf",
             f"scale={nw}:{nh}", "-y", dst],
            capture_output=True, timeout=120)
    except (OSError, _subprocess.TimeoutExpired):
        return path
    return dst if os.path.isfile(dst) else path


@router.post("/v1/videos")
async def openai_create_video(request: Request):
    """Accept JSON (Sora API style) and multipart (canvas OpenAI preset)."""
    ct = request.headers.get("content-type", "")
    ref_paths: list[str] = []
    first_frame: Optional[str] = None
    mute_audio = False
    if ct.startswith("multipart/") or ct.startswith("application/x-www-form"):
        # 影策把分镜图整张编码成 input_images 文本字段（>1MB）：
        # starlette 对非文件 part 默认 1MB 上限，会 400 "Part exceeded maximum size"
        form = await request.form(max_part_size=64 << 20)
        for key in ("audios", "reference_audios", "audio_url", "videos",
                    "reference_videos", "video_url"):
            if form.get(key):
                raise HTTPException(400, f"{key} references not supported (images only)")
        raw = {k: form.get(k) for k in ("prompt", "size", "seconds")}
        up = form.get("input_reference")
        if up is not None and hasattr(up, "read"):
            ext = os.path.splitext(getattr(up, "filename", "") or "")[1] or ".png"
            os.makedirs(REFS_DIR, exist_ok=True)
            path = os.path.join(REFS_DIR, f"ref_{uuid.uuid4().hex[:8]}{ext}")
            with open(path, "wb") as f:
                f.write(await up.read())
            ref_paths.append(path)
        # 影策/newapi: input_images 多值（重复字段名或单字段 JSON 数组字符串）
        ref_paths += _input_image_values(form.getlist("input_images"))
        ffi = form.get("first_frame_image")
        if ffi is not None and str(ffi).strip():
            first_frame = _localize_image(ffi)
        mute_audio = _as_bool(form.get("mute_audio"))
        ref_paths = [_shrink_ref_for_h3(p) for p in ref_paths]
        if first_frame:
            first_frame = _shrink_ref_for_h3(first_frame)
    else:
        try:
            raw = await request.json()
        except Exception:
            raise HTTPException(400, "body must be JSON or form")
        if not isinstance(raw, dict):
            raise HTTPException(400, "body must be a JSON object")
        # canvas sends one of these names depending on provider hints
        imgs = raw.get("input_reference")
        if imgs is None:
            imgs = raw.get("reference_images")
        if imgs is None:
            imgs = raw.get("images")
        if isinstance(imgs, str):
            imgs = [imgs]
        offered = len(imgs) if isinstance(imgs, list) else 0
        for item in imgs or []:
            if isinstance(item, str):
                path = _save_data_url(item)
                if path:
                    ref_paths.append(path)
        if offered and not ref_paths:
            raise HTTPException(400, "reference images not data URLs")
        ref_paths += _input_image_values(raw.get("input_images"))
        ffi = raw.get("first_frame_image")
        if ffi is not None and str(ffi).strip():
            first_frame = _localize_image(ffi)
        mute_audio = _as_bool(raw.get("mute_audio"))
        ref_paths = [_shrink_ref_for_h3(p) for p in ref_paths]
        if first_frame:
            first_frame = _shrink_ref_for_h3(first_frame)
    prompt = re.sub(r"\s*\[IMAGE_\d+\]", "", str(raw.get("prompt") or "")).strip()
    if not prompt:
        raise HTTPException(400, "prompt is required")
    # video refs unsupported: fail loudly, never silently drop
    for key in ("videos", "reference_videos", "video_url"):
        if raw.get(key):
            raise HTTPException(400, f"{key} references not supported (images/audio only)")
    # audio refs: data URL or existing absolute local path
    audio_paths: list[str] = []
    for key in ("audios", "reference_audios", "audio_url"):
        items = raw.get(key)
        if not items:
            continue
        if isinstance(items, str):
            items = [items]
        for item in items:
            item = str(item)
            path = _save_data_url(item, "audio")
            if not path and os.path.isabs(item) and os.path.isfile(item):
                path = item
            if not path:
                raise HTTPException(400,
                                    f"{key}: need a data URL or existing absolute path")
            audio_paths.append(path)
    size = str(raw.get("size") or "")
    seconds_raw = raw.get("seconds")
    try:
        seconds = float(seconds_raw) if seconds_raw else None
    except (TypeError, ValueError):
        seconds = None

    width, height = 864, 480
    if size:
        # canvas sends typographic ×, e.g. "512×288"
        size = size.strip().replace("×", "x").replace("X", "x")
        try:
            w, h = size.split("x", 1)
            width, height = int(w), int(h)
        except ValueError:
            raise HTTPException(400, "size must be WxH")
        # h3.c requires multiples of 32; canvas presets include 720x1280 etc.
        width -= width % 32
        height -= height % 32
    if width < 32 or height < 32:
        raise HTTPException(400, "resolution too small")
    if width * height > 768 * 1344:
        raise HTTPException(400, "resolution exceeds h3 768*1344 pixel limit")
    resp = create_job(JobRequest(
        prompt=prompt, width=width, height=height, seconds=seconds,
        refs=[Ref(kind="image", path=p) for p in ref_paths]
             + [Ref(kind="audio", path=p) for p in audio_paths],  # 先圖後音頻
        # keep reference conditioning at native res (up to 2048px), not
        # stretched down to the render canvas
        reference_image_size=1 if ref_paths else 0,
        first_frame=first_frame, mute_audio=mute_audio))
    return {"id": resp["job_id"], "task_id": resp["job_id"], "status": "queued"}


@router.get("/v1/videos/{job_id}")
def openai_video_status(job_id: str, request: Request):
    job = core.get_job(job_id)
    if not job:
        raise HTTPException(404, "video not found")
    status = _SORA_STATUS.get(job["status"], "in_progress")
    out = {
        "id": job_id,
        "status": status,
        "progress": job["progress"],
        "error": job["error"],
    }
    # 影策/newapi 协议在 succeeded 时从这里取结果地址；本地回环可达
    if status == "completed":
        out["url"] = str(request.base_url).rstrip("/") + f"/v1/videos/{job_id}/content"
    return out


@router.post("/v1/videos/{job_id}/cancel")
def openai_video_cancel(job_id: str):
    """影策 newapi 适配器的上游取消（BuildCancel）。任务已结束也返回 200 + 当前状态。"""
    job = core.get_job(job_id)
    if not job:
        raise HTTPException(404, "video not found")
    core.cancel_job(job_id)
    job = core.get_job(job_id)
    return {"id": job_id, "status": _SORA_STATUS.get(job["status"], "in_progress")}


@router.delete("/v1/videos/{job_id}")
def openai_video_delete(job_id: str):
    """newapi 上游取消用 DELETE（复用影策 deleteProviderTask helper）。"""
    return openai_video_cancel(job_id)


@router.get("/v1/videos/{job_id}/content")
def openai_video_content(job_id: str):
    job = core.get_job(job_id)
    if not job:
        raise HTTPException(404, "video not found")
    op = job["output_path"]
    if job["status"] != "completed" or not op or not os.path.isfile(op):
        raise HTTPException(409, "video not ready")
    return FileResponse(op, media_type="video/mp4")


class MixIn(BaseModel):
    video_path: Optional[str] = None
    video_job_id: Optional[str] = None
    tracks: list[dict] = []


@router.post("/v1/mix")
def create_mix(req: MixIn):
    """把 sfx/voice 軌混上視頻（mix worker）。tracks 見 workers/mix.py。"""
    video = req.video_path
    if not video and req.video_job_id:
        job = core.get_job(req.video_job_id)
        if not job or job["status"] != "completed" or not job.get("output_path"):
            raise HTTPException(404, "video job not found or not completed")
        video = job["output_path"]
    if not video:
        raise HTTPException(400, "video_path or video_job_id required")
    resp = core.create_job("mix", {"video_path": video, "tracks": req.tracks})
    return {"id": resp["id"], "job_id": resp["id"], "status": resp["status"]}


@router.get("/v1/mix/jobs/{job_id}/content")
def mix_content(job_id: str):
    job = core.get_job(job_id)
    if not job:
        raise HTTPException(404, "mix not found")
    op = job["output_path"]
    if job["status"] != "completed" or not op or not os.path.isfile(op):
        raise HTTPException(409, "mix not ready")
    return FileResponse(op, media_type="video/mp4")


class ConcatIn(BaseModel):
    shots: list[str]
    music_segments: Optional[list[dict]] = None
    bgm_gain_db: float = -6.0


@router.post("/v1/concat")
def create_concat(req: ConcatIn):
    """按序拼接行視頻（可選段級配樂 acrossfade 墊底），見 workers/concat.py。"""
    if len(req.shots) < 2:
        raise HTTPException(400, "shots must be a list of ≥2 video paths")
    missing = [p for p in req.shots if not os.path.isfile(p)]
    if missing:
        raise HTTPException(400, f"shot files not found: {missing[:3]}")
    resp = core.create_job("concat", {"shots": req.shots,
                                      "music_segments": req.music_segments,
                                      "bgm_gain_db": req.bgm_gain_db})
    return {"id": resp["id"], "job_id": resp["id"], "status": resp["status"]}
