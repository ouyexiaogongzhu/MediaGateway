"""Image super-resolution: UltraSharp 4x + optional low-strength refine.

普通行精修走 turbo 融合檔（4 步）；NSFW 行必須走 UC base（官方/turbo 會把
衣服畫回去）。純放大模式用極小 strength——sd-cli 的 ESRGAN 掛在生圖管線上，
無法完全跳過擴散，只能把去噪壓到近恆等。
"""
from __future__ import annotations

import os
from pathlib import Path

from ._util import number, run_cli, seed_of
from .qwen_image import DBCACHE, TURBO

TYPE = "image_upscale"
MEM_GB = 26.0  # 與 qwen_image 同級：同載 DiT+LLM，1728x960 精修峰值相近
DEFAULT_TIMEOUT = 3600.0

UPSCALER = os.environ.get("SDCPP_UPSCALER",
                          "/Users/vincent/tool/sd.cpp/models/upscalers/4x-UltraSharp.pth")
SDCPP_HOME = os.environ.get("SDCPP_HOME", "/Users/vincent/tool/sd.cpp")


def run(params: dict, job_dir: Path, progress, cancel) -> dict:
    src = params.get("image_path")
    if not src or not Path(src).is_file():
        raise ValueError("image_path is required")
    width = number(params, "width", 0, 0, 4096, int)
    height = number(params, "height", 0, 0, 4096, int)
    if not width or not height:
        raise ValueError("width and height are required")
    unc = bool(params.get("uncensored"))
    strength = number(params, "strength", 0.35 if not unc else 0.3, 0.01, 1.0, float)
    steps = number(params, "steps", 4 if not unc else 12, 1, 50, int)
    seed = seed_of(params, 42)
    timeout = number(params, "timeout", DEFAULT_TIMEOUT, 1.0, 4 * 3600.0, float)

    b = Path(SDCPP_HOME)
    if unc:
        diff, te, mm = ("models/diffusion_models/qwen-image-2.1-UC-Q4_K_M.gguf",
                        "models/text_encoders/qwen3vl_8b_heretic-Q4_K_M.gguf",
                        "models/text_encoders/mmproj-qwen3vl_8b_heretic-f16.gguf")
    else:
        diff = ("models/diffusion_models/qwen_image_2.1_turbo_Q4_K_M.gguf" if TURBO
                else "models/diffusion_models/qwen_image_2.1-Q4_K.gguf")
        te, mm = ("models/text_encoders/Qwen3VL-8B-Instruct-Q4_K_M.gguf",
                  "models/text_encoders/mmproj-Qwen3VL-8B-Instruct-F16.gguf")
    cmd = [
        str(b / "build" / "bin" / "sd-cli"),
        "--diffusion-model", str(b / diff),
        "--vae", str(b / "models/vae/qwen_image_2.1_vae_bf16.safetensors"),
        "--llm", str(b / te),
        "-i", str(src),
        "-p", params.get("prompt") or "same image, enhance detail and sharpness",
        "-W", str(width), "-H", str(height),
        "--strength", str(strength), "--steps", str(steps),
        "--cfg-scale", "1.0", "--scheduler", "discrete",
        "--sampling-method", "euler", "--fa", "--seed", str(seed),
        "--upscale-model", UPSCALER,
        "-o", str(job_dir / "image.png"),
    ]
    if unc:
        cmd += ["--llm_vision", str(b / mm)]
    if DBCACHE:
        cmd += ["--cache-mode", "dbcache", "--cache-option", "threshold=0.25,warmup=4"]
    progress(0.05, "upscaling")
    run_cli(cmd, cwd=SDCPP_HOME, log_path=job_dir / "upscale.log", env=None,
            timeout=timeout, cancel=cancel, engine="image_upscale")
    progress(0.95, "saving")
    out = job_dir / "image.png"
    if not out.is_file():
        raise Exception("upscale produced no output")
    return {"output_path": str(out), "engine": "ultrasharp+refine",
            "uncensored": unc, "steps": steps, "strength": strength,
            "width": width, "height": height}
