"""LTX-2.5 video generation worker — MLX (dgrauet/ltx-2-mlx), q4 distilled.

22B transformer via MLX on Apple Silicon; native AV generation (video+audio).
Blind-review winner 2026-09-09 (68s/3.7s clip @832x448 vs h3 9B 2.5-3min,
audio clarity decisively better). TTS 暫停路線下，對白由 prompt 攜帶、LTX 原生說話。
"""
from __future__ import annotations

import os
import signal
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
    if params.get("low_ram"):
        cmd += ["--low-ram"]
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
    # ponytail: 不用 subprocess.run(capture_output) —— CLI 崩溃/被杀时孤儿孙进程
    # 会握住管道 EOF，run() 永远阻塞（job 卡 running 实证两次）。改日志文件+wait。
    log = out.parent / "ltx_render.log"
    with _run_lock:
        proc = subprocess.Popen(
            _cli_cmd(prompt, image, str(out), width, height),
            cwd=DEFAULT_HOME, stdout=open(log, "w"), stderr=subprocess.STDOUT,
            start_new_session=True)
        try:
            proc.wait(timeout=float(params.get("timeout", DEFAULT_TIMEOUT)))
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            raise Exception(f"ltx timeout after {params.get('timeout', DEFAULT_TIMEOUT)}s")
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
        raise Exception(f"ltx exited {proc.returncode}: {tail}")
    if not out.is_file():
        raise Exception("ltx produced no output")
    return {"output_path": str(out), "width": width, "height": height}
