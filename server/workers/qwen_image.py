"""Qwen-Image-2.1 image worker — stable-diffusion.cpp (Metal, GGUF).

7B DiT + Qwen3-VL-8B Q4_K_M text encoder; dims must be divisible by 32
(compat_openai._parse_size already enforces). Install: ~/tool/sd.cpp
(build/bin/sd-cli) + models/{diffusion_models,vae,text_encoders}.
"""
from __future__ import annotations

import os
import signal
import subprocess
import threading
from pathlib import Path

TYPE = "qwen_image"
MEM_GB = 10.0

DEFAULT_HOME = os.environ.get("SDCPP_HOME", "/Users/vincent/tool/sd.cpp")
DEFAULT_TIMEOUT = 1800.0

# ponytail: global lock — single Metal device; parallel runs unmeasured
_run_lock = threading.Lock()


def _cli_cmd(prompt: str, out: Path, width: int, height: int,
             steps: int, cfg: float, seed: int) -> list[str]:
    b = Path(DEFAULT_HOME)
    return [
        str(b / "build" / "bin" / "sd-cli"),
        "--diffusion-model", str(b / "models/diffusion_models/qwen_image_2.1-Q4_K.gguf"),
        "--vae", str(b / "models/vae/qwen_image_2.1_vae_bf16.safetensors"),
        "--llm", str(b / "models/text_encoders/Qwen3VL-8B-Instruct-Q4_K_M.gguf"),
        "-p", prompt,
        "-W", str(width), "-H", str(height),
        "-s", str(steps), "--cfg-scale", str(cfg),
        "--sampling-method", "euler", "--offload-to-cpu", "--fa",
        "--seed", str(seed),
        "-o", str(out),
    ]


def run(params: dict, job_dir: Path, progress, cancel) -> dict:
    prompt = params.get("prompt")
    if not prompt:
        raise ValueError("prompt is required")
    width = int(params.get("width") or 1024)
    height = int(params.get("height") or 1024)
    steps = int(params.get("steps") or 20)
    cfg = float(params.get("cfg_scale") or 6.0)
    seed = int(params.get("seed") or int.from_bytes(os.urandom(4), "little"))

    out = job_dir / "image.png"
    if cancel():
        raise Exception("cancelled")
    progress(0.05, "generating")
    env = dict(os.environ)
    # ponytail: log file + wait + killpg, never capture_output (dead child holds the pipe)
    log = out.parent / "qwen_image.log"
    with _run_lock:
        proc = subprocess.Popen(
            _cli_cmd(prompt, out, width, height, steps, cfg, seed),
            cwd=DEFAULT_HOME,
            stdout=open(log, "w"), stderr=subprocess.STDOUT,
            env=env, start_new_session=True)
        try:
            proc.wait(timeout=float(params.get("timeout", DEFAULT_TIMEOUT)))
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            raise Exception(f"qwen_image timeout after {params.get('timeout', DEFAULT_TIMEOUT)}s")
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
        raise Exception(f"qwen_image exited {proc.returncode}: {tail}")
    if not out.is_file():
        raise Exception("qwen_image produced no output")
    return {"output_path": str(out), "engine": "qwen-image-2.1",
            "steps": steps, "cfg_scale": cfg, "seed": seed,
            "width": width, "height": height}
