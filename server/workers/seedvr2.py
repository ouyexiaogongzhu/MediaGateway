"""SeedVR2 video upscaling worker — wraps numz/ComfyUI-SeedVR2_VideoUpscaler standalone CLI.

MPS-native on Apple Silicon (SDPA attention, no flash-attn); temporal-consistent
video upscaling 768p-class → 1080p/2160p. Install: ~/tool/seedvr2 (git clone + venv).
"""
from __future__ import annotations

import os
import subprocess
import threading
from pathlib import Path

TYPE = "seedvr2"
MEM_GB = 24.0

DEFAULT_HOME = os.environ.get("SEEDVR2_HOME", "/Users/vincent/tool/seedvr2")
DEFAULT_TIMEOUT = 7200.0
_MODELS = {"3b", "7b"}

# ponytail: global lock — MPS streams 20GB+ per chunk; parallel runs unmeasured
_run_lock = threading.Lock()


def _cli_cmd(video_path: str, out_path: str, resolution: str, model: str) -> list[str]:
    """Standalone CLI: inference_cli.py（安装报告校准）；batch_size 必须 4n+1；cwd=repo 根（模型落 ./models/SEEDVR2）。"""
    python = os.environ.get("SEEDVR2_PYTHON", os.path.join(DEFAULT_HOME, ".venv", "bin", "python"))
    script = os.path.join(DEFAULT_HOME, "inference_cli.py")
    return [python, script, video_path,
            "--dit_model", f"seedvr2_ema_{model}_fp16.safetensors",
            "--resolution", resolution,
            "--batch_size", "33", "--chunk_size", "330", "--temporal_overlap", "3",
            "--output", out_path]


def run(params: dict, job_dir: Path, progress, cancel) -> dict:
    video_path = params.get("video_path")
    if not video_path or not Path(video_path).is_file():
        raise ValueError(f"video_path not found: {video_path}")
    resolution = str(params.get("resolution") or "1080")
    model = str(params.get("model") or "3b")
    if model not in _MODELS:
        raise ValueError(f"unknown model: {model} (known: {sorted(_MODELS)})")

    out = job_dir / "output.mp4"
    if cancel():
        raise Exception("cancelled")
    progress(0.05, "upscaling")
    with _run_lock:
        try:
            proc = subprocess.run(
                _cli_cmd(video_path, str(out), resolution, model),
                cwd=DEFAULT_HOME, capture_output=True, text=True,
                timeout=float(params.get("timeout", DEFAULT_TIMEOUT)))
        except subprocess.TimeoutExpired:
            raise Exception(f"seedvr2 timeout after {params.get('timeout', DEFAULT_TIMEOUT)}s")
    progress(0.95, "saving")
    if proc.returncode != 0:
        raise Exception(f"seedvr2 exited {proc.returncode}: {(proc.stderr or proc.stdout or '')[-500:]}")
    if not out.is_file():
        raise Exception("seedvr2 produced no output")
    return {"output_path": str(out), "resolution": resolution, "model": model}
