"""Qwen-Image-2.1 image worker — stable-diffusion.cpp (Metal, GGUF).

7B DiT + Qwen3-VL-8B Q4_K_M text encoder; dims must be divisible by 32
(compat_openai._parse_size already enforces). Install: ~/tool/sd.cpp
(build/bin/sd-cli) + models/{diffusion_models,vae,text_encoders}.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

from ._util import number, run_cli, seed_of

TYPE = "qwen_image"
MEM_GB = 10.0

DEFAULT_HOME = os.environ.get("SDCPP_HOME", "/Users/vincent/tool/sd.cpp")
DEFAULT_TIMEOUT = 1800.0

# ponytail: global lock — single Metal device; parallel runs unmeasured
_run_lock = threading.Lock()


def _round32(v: int) -> int:
    return max(32, v // 32 * 32)  # sd.cpp Qwen-Image requires dims % 32 == 0


def _cli_cmd(prompt: str, out: Path, width: int, height: int,
             steps: int, cfg: float, seed: int, refs=()) -> list[str]:
    b = Path(DEFAULT_HOME)
    # QWEN_IMAGE_UNCENSORED=1 → abenzerps UC 擴散 + pottokao Heretic 文本編碼器（去審查檔）
    unc = os.environ.get("QWEN_IMAGE_UNCENSORED") == "1"
    diff = ("models/diffusion_models/qwen-image-2.1-UC-Q4_K_M.gguf" if unc
            else "models/diffusion_models/qwen_image_2.1-Q4_K.gguf")
    te = ("models/text_encoders/qwen3vl_8b_heretic-Q4_K_M.gguf" if unc
          else "models/text_encoders/Qwen3VL-8B-Instruct-Q4_K_M.gguf")
    cmd = [
        str(b / "build" / "bin" / "sd-cli"),
        "--diffusion-model", str(b / diff),
        "--vae", str(b / "models/vae/qwen_image_2.1_vae_bf16.safetensors"),
        "--llm", str(b / te),
    ]
    if unc:
        cmd += ["--llm_vision", str(b / "models/text_encoders/mmproj-qwen3vl_8b_heretic-f16.gguf")]
    if refs:
        for r in refs:  # 圖+文編輯：-r 參考圖；編輯模式必須給視覺塔
            cmd += ["-r", str(r)]
        if not unc:
            cmd += ["--llm_vision", str(b / "models/text_encoders/mmproj-Qwen3VL-8B-Instruct-F16.gguf")]
    cmd += [
        "-p", prompt,
        "-W", str(width), "-H", str(height),
        "-s", str(steps), "--cfg-scale", str(cfg),
        "--sampling-method", "euler", "--offload-to-cpu", "--fa",
        "--seed", str(seed),
        "-o", str(out),
    ]
    return cmd


def run(params: dict, job_dir: Path, progress, cancel) -> dict:
    prompt = params.get("prompt")
    if not prompt:
        raise ValueError("prompt is required")
    width = _round32(number(params, "width", 1024, 32, 4096, int))
    height = _round32(number(params, "height", 1024, 32, 4096, int))
    steps = number(params, "steps", 20, 1, 150, int)
    cfg = number(params, "cfg_scale", 6.0, 0.0, 30.0, float)
    seed = seed_of(params, int.from_bytes(os.urandom(4), "little"))
    timeout = number(params, "timeout", DEFAULT_TIMEOUT, 1.0, 4 * 3600.0, float)

    out = job_dir / "image.png"
    refs = [r for r in (params.get("refs") or []) if r]
    progress(0.05, "generating")
    with _run_lock:
        run_cli(_cli_cmd(prompt, out, width, height, steps, cfg, seed, refs),
                cwd=DEFAULT_HOME, log_path=job_dir / "qwen_image.log",
                env=None,  # inherit environ
                timeout=timeout, cancel=cancel, engine="qwen_image")
    progress(0.95, "saving")
    if not out.is_file():
        raise Exception("qwen_image produced no output")
    return {"output_path": str(out), "engine": "qwen-image-2.1",
            "steps": steps, "cfg_scale": cfg, "seed": seed,
            "width": width, "height": height}
