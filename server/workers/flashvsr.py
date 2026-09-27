"""FlashVSR video upscaling worker — wraps OpenImagingLab/FlashVSR v1.1 tiny (Wan2.1 1.3B, DMD 1-step).

MPS-native on Apple Silicon. Recipe (validated 2026-09-27): FLASHVSR_NO_MASK dense SDPA,
always infer at 720p-class, lanczos finish to 1080 — 15s clip ≈ 5.2 min. Install: ~/tool/FlashVSR
(venv torch+imageio+imageio-ffmpeg; weights under examples/WanVSR/FlashVSR-v1.1/, sha256-verified).
"""
from __future__ import annotations

import os
import signal
import subprocess
import threading
from pathlib import Path

TYPE = "flashvsr"
MEM_GB = 20.0

DEFAULT_HOME = os.environ.get("FLASHVSR_HOME", "/Users/vincent/tool/FlashVSR")
DEFAULT_TIMEOUT = 3600.0
_RESOLUTIONS = {"576", "720", "1080"}

# ponytail: global lock — single Metal device; parallel runs unmeasured
_run_lock = threading.Lock()


def _cli_cmd(video_path: str, out_path: str, resolution: str, seed: int) -> list[str]:
    python = os.environ.get("FLASHVSR_PYTHON", os.path.join(DEFAULT_HOME, ".venv", "bin", "python"))
    wd = os.path.join(DEFAULT_HOME, "examples", "WanVSR")
    return [python, "upscale_cli.py", video_path, out_path,
            "--resolution", resolution, "--seed", str(seed)]


def run(params: dict, job_dir: Path, progress, cancel) -> dict:
    video_path = params.get("video_path")
    if not video_path or not Path(video_path).is_file():
        raise ValueError(f"video_path not found: {video_path}")
    resolution = str(params.get("resolution") or "1080")
    if resolution not in _RESOLUTIONS:
        raise ValueError(f"unknown resolution: {resolution} (known: {sorted(_RESOLUTIONS)})")
    seed = int(params.get("seed", 0))

    out = job_dir / "output.mp4"
    if cancel():
        raise Exception("cancelled")
    progress(0.05, "upscaling")
    env = dict(os.environ, PYTHONPATH=DEFAULT_HOME)
    # ponytail: log file + wait + killpg, never capture_output (OOM'd child holds the pipe)
    log = out.parent / "flashvsr.log"
    with _run_lock:
        proc = subprocess.Popen(
            _cli_cmd(video_path, str(out), resolution, seed),
            cwd=os.path.join(DEFAULT_HOME, "examples", "WanVSR"),
            stdout=open(log, "w"), stderr=subprocess.STDOUT,
            env=env, start_new_session=True)
        try:
            proc.wait(timeout=float(params.get("timeout", DEFAULT_TIMEOUT)))
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            raise Exception(f"flashvsr timeout after {params.get('timeout', DEFAULT_TIMEOUT)}s")
        if cancel():
            os.killpg(proc.pid, signal.SIGKILL)
            raise Exception("cancelled")
    progress(0.95, "saving")
    if proc.returncode != 0:
        tail = ""
        try:
            tail = open(log, errors="ignore").read()[-500:]
        except OSError:
            pass
        raise Exception(f"flashvsr exited {proc.returncode}: {tail}")
    if not out.is_file():
        raise Exception("flashvsr produced no output")
    return {"output_path": str(out), "resolution": resolution,
            "engine": "flashvsr-v1.1-tiny", "recipe": "720p-diffusion" + ("+lanczos" if resolution == "1080" else "")}
