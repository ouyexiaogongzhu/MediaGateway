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
# 实测 sd-cli 峰值 RSS 22-27GB（8 refs 编辑上下文），不是 10——按 10 算账时
# BUDGET_GB=40 会一次放行 3 个 job，全部堵在 _run_lock 上，started_at 又在拿到
# 锁之前写入，锁等待全被记成工时（影策外层超时照跳，「超预算 4 倍」假象）。
# 同一笔错账也曾放 3 个 sd-cli 同时进 48GB → 记忆体死锁。26 保证同时只跑一个。
MEM_GB = 26.0

DEFAULT_HOME = os.environ.get("SDCPP_HOME", "/Users/vincent/tool/sd.cpp")
DEFAULT_TIMEOUT = 1800.0

# 每張參考圖的視覺編碼常駐統一記憶體。實測 M5 Pro 48GB：864x480 + 8 refs 完成
# （579s），9-10 refs 時 RSS 衝上 ~22GB、swap 打滿、進程 0.3% CPU 停滯永不返回
# ——和 1024x1024 的像素死鎖同一機制，觸發軸換成 ref 數。硬截是硬體保護；
# 產品級的 maxImages（=4）由影策能力聲明與畫布提交校驗管。
# ponytail: 硬上限 8 是單機實測值，換機器/換模型需重測。
MAX_REFS = 8

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
        # 注意：sd.cpp 的 -s 是 --seed 的缩写，steps 只有长旗标——用 -s 传步数
        # 实际改的是 seed（随后还被 --seed 覆盖），步数永远是默认 20。
        "--steps", str(steps), "--cfg-scale", str(cfg),
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
    # 草稿默認 12 步（euler）：864x480 每張省 ~100s，20 步與 12 步草稿肉眼差異可忽略。
    # 需要成片品質的呼叫端自己傳 steps=20。
    steps = number(params, "steps", 12, 1, 150, int)
    cfg = number(params, "cfg_scale", 6.0, 0.0, 30.0, float)
    seed = seed_of(params, int.from_bytes(os.urandom(4), "little"))
    timeout = number(params, "timeout", DEFAULT_TIMEOUT, 1.0, 4 * 3600.0, float)

    out = job_dir / "image.png"
    refs = [r for r in (params.get("refs") or []) if r]
    if len(refs) > MAX_REFS:
        # 前面的是角色/場景（上游連線順序），截尾丟道具——記憶體死鎖比丟道具參考貴。
        refs = refs[:MAX_REFS]
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
