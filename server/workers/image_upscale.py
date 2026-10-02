"""Image super-resolution: UltraSharp 4x + optional low-strength refine.

單一 turbo 路徑（4 步 cfg1）。純放大模式用極小 strength——sd-cli 的 ESRGAN 掛在
生圖管線上，無法完全跳過擴散，只能把去噪壓到近恆等。

⚠️ 舊 docstring 寫「NSFW 行必須走 UC base（官方/turbo 會把衣服畫回去）」——
   實測推翻：生產設定 strength 0.35 / 4 步的非 unc 路徑對 NSFW 輸入完整保留
   無衣狀態，沒有任何衣物畫回來（view/up_A_turbo.png 肉眼核對）。所以不需要分流到
   去審查底模、不需要 heretic 視覺塔。params["uncensored"] 保留但忽略——為了不動
   compat_openai 介面（會外溢到影策合約）。
   ⚠️ strength >0.5 未測：超過就不是放大語義了，會把參考圖重畫掉。
"""
from __future__ import annotations

import os
from pathlib import Path

from ._util import number, run_cli, seed_of
from .qwen_image import DBCACHE, DBCACHE_OPTION

TYPE = "image_upscale"
MEM_GB = 26.0  # 與 qwen_image 同級：同載 DiT+LLM，1728x960 精修峰值相近
DEFAULT_TIMEOUT = 3600.0

# ⚠️ 這個 worker 沒跟著生圖那邊切到 Viggle/Krea2——圖+文精修走 UltraSharp +
#    低強度重繪，換底模要另外實測 1728x960 的記憶體峰值才能動 MEM_GB。
DIFF = "models/diffusion_models/qwen_image_2.1_turbo_Q4_K_M.gguf"
TE = "models/text_encoders/Qwen3VL-8B-Instruct-Q4_K_M.gguf"

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
    # params["uncensored"] 刻意忽略（見 docstring）：單一 turbo 路徑對 NSFW 零降級，
    # 保留參數只是為了不動 compat_openai 的介面合約。
    strength = number(params, "strength", 0.35, 0.01, 1.0, float)
    steps = number(params, "steps", 4, 1, 50, int)
    seed = seed_of(params, 42)
    timeout = number(params, "timeout", DEFAULT_TIMEOUT, 1.0, 4 * 3600.0, float)

    b = Path(SDCPP_HOME)
    cmd = [
        str(b / "build" / "bin" / "sd-cli"),
        "--diffusion-model", str(b / DIFF),
        "--vae", str(b / "models/vae/qwen_image_2.1_vae_bf16.safetensors"),
        "--llm", str(b / TE),
        "-i", str(src),
        "-p", params.get("prompt") or "same image, enhance detail and sharpness",
        "-W", str(width), "-H", str(height),
        "--strength", str(strength), "--steps", str(steps),
        "--cfg-scale", "1.0", "--scheduler", "discrete",
        "--sampling-method", "euler", "--fa", "--seed", str(seed),
        "--upscale-model", UPSCALER,
        "-o", str(job_dir / "image.png"),
    ]
    if DBCACHE:
        cmd += ["--cache-mode", "dbcache", "--cache-option", DBCACHE_OPTION]
    progress(0.05, "upscaling")
    run_cli(cmd, cwd=SDCPP_HOME, log_path=job_dir / "upscale.log", env=None,
            timeout=timeout, cancel=cancel, engine="image_upscale")
    progress(0.95, "saving")
    out = job_dir / "image.png"
    if not out.is_file():
        raise Exception("upscale produced no output")
    return {"output_path": str(out), "engine": "ultrasharp+refine",
            "steps": steps, "strength": strength,
            "width": width, "height": height}
