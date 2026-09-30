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

# Viggle Qwen-Image-2.1-viggle-turbo（DMD 蒸餾）：40 次模型評估 → 4 次，免 CFG。
# QWEN_IMAGE_TURBO=1 啟用（乾淨實測 48s/張 vs base 12 步 132s，畫質難分）。
# scheduler 實測 discrete 優於官方 README 建議的 simple（35.5s vs 41.4s，畫質同級）。
# ⚠️ 僅用於官方 base——UC 去審查底模疊 turbo LoRA 實測燒圖（綠灰噪塊），已移除；
# uncensored 提速走 QWEN_IMAGE_DBCACHE=1（dbcache 塊級快取 1.4×，畫質無損）。
# turbo 按 1-3 張參考圖訓練，REF_CAP 同步裁到 3。
TURBO = os.environ.get("QWEN_IMAGE_TURBO") == "1"
DBCACHE = os.environ.get("QWEN_IMAGE_DBCACHE") == "1"
TURBO_DIFF = "models/diffusion_models/qwen_image_2.1_turbo_Q4_K_M.gguf"

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
             steps: int, cfg: float, seed: int, refs=(), unc: bool | None = None) -> list[str]:
    b = Path(DEFAULT_HOME)
    # unc：UC 去審查檔。優先級 = 任務參數（compat 按模型名分流）> env 全局開關
    if unc is None:
        unc = os.environ.get("QWEN_IMAGE_UNCENSORED") == "1"
    if TURBO and not unc:
        # 融合 turbo 檔：官方 base 專用（UC 底模疊蒸餾 LoRA 會燒圖，不走此徑）
        diff, steps, cfg = TURBO_DIFF, 4, 1.0
    else:
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
        # 多 ref 的視覺 KV 是峰值 RSS 大頭（25GB/8ref）。8-bit 前綴快取省一半，
        # 換取 swap 壓力消失；KV 量化對畫質影響遠小於 swap 拖慢（docs/qwen_image_2.1.md）
        cmd += ["--model-args", "qwen_image_2_1_prefix_cache_type=q8_0"]
    if TURBO and not unc:
        cmd += ["--scheduler", "discrete"]
    if DBCACHE:
        cmd += ["--cache-mode", "dbcache", "--cache-option", "threshold=0.25,warmup=4"]
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
    unc = bool(params.get("uncensored")) or os.environ.get("QWEN_IMAGE_UNCENSORED") == "1"
    if TURBO and not unc:
        steps, cfg = 4, 1.0  # 與 _cli_cmd 的 turbo 配方同步，result 元數據才不說謊
    seed = seed_of(params, int.from_bytes(os.urandom(4), "little"))
    timeout = number(params, "timeout", DEFAULT_TIMEOUT, 1.0, 4 * 3600.0, float)

    out = job_dir / "image.png"
    refs = [r for r in (params.get("refs") or []) if r]
    cap = 3 if TURBO else MAX_REFS  # turbo 按 1-3 張參考圖訓練
    if len(refs) > cap:
        # 前面的是角色/場景（上游連線順序），截尾丟道具——記憶體死鎖比丟道具參考貴。
        refs = refs[:cap]
    progress(0.05, "generating")
    with _run_lock:
        run_cli(_cli_cmd(prompt, out, width, height, steps, cfg, seed, refs, unc=unc),
                cwd=DEFAULT_HOME, log_path=job_dir / "qwen_image.log",
                env=None,  # inherit environ
                timeout=timeout, cancel=cancel, engine="qwen_image")
    progress(0.95, "saving")
    if not out.is_file():
        raise Exception("qwen_image produced no output")
    return {"output_path": str(out), "engine": "qwen-image-2.1",
            "steps": steps, "cfg_scale": cfg, "seed": seed,
            "width": width, "height": height}
