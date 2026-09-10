"""LTX-2.5 video generation worker — MLX (dgrauet/ltx-2-mlx), q4 distilled.

22B transformer via MLX on Apple Silicon; native AV generation (video+audio).
Blind-review winner 2026-09-09 (68s/3.7s clip @832x448 vs h3 9B 2.5-3min,
audio clarity decisively better). TTS 暫停路線下，對白由 prompt 攜帶、LTX 原生說話。
"""
from __future__ import annotations

import os
import subprocess
import threading
from pathlib import Path

TYPE = "ltx"
MEM_GB = 26.0

DEFAULT_HOME = os.environ.get("LTX_HOME", "/Users/vincent/tool/ltx-2-mlx")
DEFAULT_TIMEOUT = 3600.0

# ponytail: global lock — MLX 22B 峰值 ~25GB，並行未測
_run_lock = threading.Lock()


def _cli_cmd(prompt: str, image_path: str | None, out: str,
             width: int, height: int, frame_rate: int = 24) -> list[str]:
    entry = os.environ.get("LTX_ENTRY", os.path.join(DEFAULT_HOME, ".venv", "bin", "ltx-2-mlx"))
    cmd = [entry, "generate", "--distilled", "--model", "models/ltx-2.5-mlx-q4",
           "--prompt", prompt,
           "-H", str(height), "-W", str(width), "--frame-rate", str(frame_rate),
           "-o", out]
    if image_path:
        cmd += ["--image", image_path]
    return cmd


def run(params: dict, job_dir: Path, progress, cancel) -> dict:
    prompt = params.get("prompt")
    if not prompt:
        raise ValueError("params.prompt is required")
    width = int(params.get("width", 864))
    height = int(params.get("height", 480))
    out = job_dir / "output.mp4"
    image = params.get("image_path")
    if image and not Path(image).is_file():
        raise ValueError(f"image_path not found: {image}")

    if cancel():
        raise Exception("cancelled")
    progress(0.05, "generating")
    with _run_lock:
        try:
            proc = subprocess.run(
                _cli_cmd(prompt, image, str(out), width, height),
                cwd=DEFAULT_HOME, capture_output=True, text=True,
                timeout=float(params.get("timeout", DEFAULT_TIMEOUT)))
        except subprocess.TimeoutExpired:
            raise Exception(f"ltx timeout after {params.get('timeout', DEFAULT_TIMEOUT)}s")
    progress(0.95, "saving")
    if proc.returncode != 0:
        raise Exception(f"ltx exited {proc.returncode}: {(proc.stderr or proc.stdout or '')[-500:]}")
    if not out.is_file():
        raise Exception("ltx produced no output")
    return {"output_path": str(out), "width": width, "height": height}
