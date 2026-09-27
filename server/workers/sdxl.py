"""SDXL image worker — local daemon (127.0.0.1:8187), realvis/noobai.

Daemon API: POST /generate {model, prompt, negative_prompt, width, height,
steps, guidance_scale, seed} → {path: "<png絕對路徑>", elapsed_s}。同步 20-90s
（含換載 90s 餘量）。daemon 的 outputs 目錄會被清理，成功後必須轉存 job_dir。
"""
from __future__ import annotations

import json
import os
import shutil
import urllib.error
import urllib.request
from pathlib import Path

TYPE = "sdxl"
MEM_GB = 8.0

DAEMON_URL = os.environ.get("SDXL_DAEMON_URL", "http://127.0.0.1:8187")
DEFAULT_TIMEOUT = 300.0  # 20-90s 生成 + 90s 換載餘量
_MODELS = {"realvis", "noobai"}


def _payload(params: dict) -> dict:
    model = str(params.get("model") or "realvis")
    if model not in _MODELS:
        raise ValueError(f"unknown model: {model} (known: {sorted(_MODELS)})")
    return {
        "model": model,
        "prompt": str(params["prompt"]),
        "negative_prompt": str(params.get("negative_prompt") or ""),
        "width": int(params.get("width", 832)),
        "height": int(params.get("height", 1216)),
        "steps": int(params.get("steps", 30)),
        "guidance_scale": float(params.get("guidance_scale", 5.0)),
        "seed": int(params.get("seed", 0)),
        # ControlNet 透傳（daemon 支持 openpose/depth/canny/lineart/white）
        "control_type": str(params.get("control_type") or ""),
        "control_image_path": params.get("control_image_path"),
        "controlnet_scale": float(params.get("controlnet_scale", 0.8)),
    }


def run(params: dict, job_dir: Path, progress, cancel) -> dict:
    if not params.get("prompt"):
        raise ValueError("params.prompt is required")
    body = json.dumps(_payload(params)).encode()
    if cancel():
        raise Exception("cancelled")
    progress(0.05, "generating")
    req = urllib.request.Request(
        f"{DAEMON_URL.rstrip('/')}/generate", data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(
                req, timeout=float(params.get("timeout", DEFAULT_TIMEOUT))) as r:
            resp = json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise Exception(f"sdxl daemon HTTP {e.code}: {e.read()[:300]}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise Exception(f"sdxl daemon unreachable at {DAEMON_URL}: {e}")
    if not isinstance(resp, dict) or not resp.get("path"):
        raise Exception(f"sdxl daemon returned no path: {str(resp)[:300]}")
    src = Path(resp["path"])
    if not src.is_file():
        raise Exception(f"sdxl daemon png missing: {src}")
    out = job_dir / "output.png"
    progress(0.95, "saving")
    shutil.copyfile(src, out)  # daemon outputs 会被清理，必须转存
    return {"output_path": str(out), "model": resp.get("model") or params.get("model") or "realvis",
            "width": int(params.get("width", 832)), "height": int(params.get("height", 1216))}
