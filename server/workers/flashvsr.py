"""FlashVSR video upscaling worker — wraps OpenImagingLab/FlashVSR v1.1 tiny (Wan2.1 1.3B, DMD 1-step).

MPS-native on Apple Silicon. Recipe (validated 2026-09-27): FLASHVSR_NO_MASK dense SDPA,
always infer at 720p-class, lanczos finish to 1080 — 15s clip ≈ 5.2 min. Install: ~/tool/FlashVSR
(venv torch+imageio+imageio-ffmpeg; weights under examples/WanVSR/FlashVSR-v1.1/, sha256-verified).
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

from ._util import number, run_cli, seed_of

TYPE = "flashvsr"
MEM_GB = 20.0

DEFAULT_HOME = os.environ.get("FLASHVSR_HOME", "/Users/vincent/tool/FlashVSR")
DEFAULT_TIMEOUT = 3600.0
_RESOLUTIONS = {"576", "720", "1080"}

# ponytail: global lock — single Metal device; parallel runs unmeasured
_run_lock = threading.Lock()


def _cli_cmd(video_path: str, out_path: str, resolution: str, seed: int) -> list[str]:
    python = os.environ.get("FLASHVSR_PYTHON", os.path.join(DEFAULT_HOME, ".venv", "bin", "python"))
    return [python, "upscale_cli.py", video_path, out_path,
            "--resolution", resolution, "--seed", str(seed)]


def run(params: dict, job_dir: Path, progress, cancel) -> dict:
    video_path = params.get("video_path")
    if not video_path or not Path(video_path).is_file():
        raise ValueError(f"video_path not found: {video_path}")
    resolution = str(params.get("resolution") or "1080")
    if resolution not in _RESOLUTIONS:
        raise ValueError(f"unknown resolution: {resolution} (known: {sorted(_RESOLUTIONS)})")
    seed = seed_of(params, 0)
    timeout = number(params, "timeout", DEFAULT_TIMEOUT, 1.0, 4 * 3600.0, float)

    out = job_dir / "output.mp4"
    progress(0.05, "upscaling")
    env = dict(os.environ)
    # prepend, never clobber an inherited PYTHONPATH
    env["PYTHONPATH"] = os.pathsep.join(
        p for p in (DEFAULT_HOME, os.environ.get("PYTHONPATH")) if p)
    with _run_lock:
        run_cli(_cli_cmd(video_path, str(out), resolution, seed),
                cwd=os.path.join(DEFAULT_HOME, "examples", "WanVSR"),
                log_path=job_dir / "flashvsr.log", env=env,
                timeout=timeout, cancel=cancel, engine="flashvsr")
    progress(0.95, "saving")
    if not out.is_file():
        raise Exception("flashvsr produced no output")
    return {"output_path": str(out), "resolution": resolution,
            "engine": "flashvsr-v1.1-tiny",
            "recipe": f"{resolution}p-final" + ("" if resolution == "576" else "+lanczos")}
