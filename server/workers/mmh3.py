"""Video worker: mmh3 (MiniMax-H3, Rust/Metal CLI) — spawned per job.

Same MiniMax-H3 model family as h3.c (video.py), 4.0× faster on this box
(544×960/15s: 9.7 min vs 38.7 min, see docs/mmh3_report.md). Runs the CLI
with --vram-budget 40: without it mmh3 fills Metal's whole working set and
macOS kills it mid-run (two crashed runs before this was pinned).

Keep_loaded does not apply — the CLI loads one model per invocation (~10 s,
already inside the measured time). Frames follow the same 17n+5 grid at
24 fps; out-of-grid values are rounded up by the engine itself.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from ._util import job_output, number, run_cli, seed_of

TYPE = "mmh3"
MEM_GB = 20.0  # 实测峰值 RSS 18.5-19.4GiB（40G 上限内）；+omlx chat 19G = 39G < 48G

BIN = os.environ.get("MMH3_BIN", "/Users/vincent/tool/mmh3/target/release/mmh3")
MODELS = os.environ.get("MMH3_MODELS", "/Users/vincent/tool/mmh3/models")
MODELS_ROOT = Path(MODELS)
BUDGET = int(os.environ.get("MMH3_VRAM_BUDGET", "40"))
PATCH = "minimax_h3_fasth3_vsa_datafree_patch_rank64.safetensors"  # 退役，留檔名參照
PIH3_PATCH = "minimax_h3_pih3_2step.safetensors"
REF2VA_TURBO = "minimax_h3_ref2v_turbo_8step_v1.0_768p_comfyui_bf16.safetensors"
# 生產配方（2026-11-18 目檢定案）：DMAD 4步，8步 turbo 的 -53%（400s vs 843s @768×1344/120f）
REF2VA_LORA = "minimax_h3_dmad_4step_full_critic_rank128_bf16.safetensors"


def _check_file(label: str, path):
    if not path or not os.path.isfile(path):
        raise ValueError(f"{label} not found: {path}")
    return path


def run(params: dict, job_dir: Path, progress, cancel) -> dict:
    prompt = params.get("prompt")
    if not prompt:
        raise ValueError("prompt is required")

    # refs（圖/音）→ Ref2VA DiT 路徑：--reference ≤9 張、--reference-audio ≤3 條，
    # 官方配方（docs/ref2va.md）= Turbo LoRA 8步 + shift 6/3 + sol attention + sparse-start 0。
    # FastH3 patch 是 fl2va DiT 的，此路不帶。Metal 無 INT8/FP8 attention → 不傳 precision。
    ref_files = []
    audio_files = []
    for r in params.get("refs") or []:
        path = r["path"] if isinstance(r, dict) else r
        kind = r.get("kind", "image") if isinstance(r, dict) else "image"
        if kind == "image":
            ref_files.append(_check_file("reference", path))
        elif kind == "audio":
            audio_files.append(_check_file("reference audio", path))
        else:
            raise ValueError(f"ref kind not supported by mmh3: {kind}")
    if len(ref_files) > 9:
        raise ValueError(f"at most 9 reference images (got {len(ref_files)})")
    if len(audio_files) > 3:
        raise ValueError(f"at most 3 reference audios (got {len(audio_files)})")
    ref2va = bool(ref_files or audio_files)
    if ref2va and not (MODELS_ROOT / "loras" / REF2VA_LORA).is_file():
        raise ValueError(f"ref2va needs the DMAD LoRA (loras/{REF2VA_LORA}) — not downloaded")

    width = number(params, "width", 544, 256, 2048, int)
    height = number(params, "height", 960, 256, 2048, int)
    if params.get("seconds"):
        frames = max(1, round(float(params["seconds"]) * 24))
    else:
        frames = number(params, "frames", 124, 5, 3600, int)
    if ref2va:
        steps = 4  # DMAD 蒸餾配方；JobRequest 預設 4 會被 API 層塞進 params，不可信
    else:
        steps = 2  # pi-H3 蒸餾配方（docs/pih3.md，實測 -40% 於 FastH3 4步VSA）
    seed = seed_of(params, 0)

    cmd = [BIN, "generate", "--models", MODELS, "--prompt", prompt,
           "--width", str(width), "--height", str(height),
           "--frames", str(frames), "--steps", str(steps), "--seed", str(seed),
           "--vram-budget", str(BUDGET),
           "--out", job_output(job_dir, params, "output.mp4")]
    if ref2va:
        for f in ref_files:
            cmd += ["--reference", f]
        for f in audio_files:
            cmd += ["--reference-audio", f]
        # docs/dmad.md 蒸餾配方：4步 shift 12/2 + renoise sampler
        cmd += ["--lora", REF2VA_LORA, "--shift-video", "12", "--shift-audio", "2",
                "--sampler", "renoise",
                "--attention", "sol", "--sparse-start", "0"]
    else:
        # docs/pih3.md：2步 pih3 schedule（shifts 用預設 12/3）+ sol；FastH3 patch 退役
        if not (MODELS_ROOT / "patches" / PIH3_PATCH).is_file():
            raise ValueError(f"pi-H3 patch missing (patches/{PIH3_PATCH})")
        cmd += ["--schedule", "pih3", "--attention", "sol", "--sparse-start", "0",
                "--patch", PIH3_PATCH]
    if params.get("first_frame"):
        cmd += ["--first-frame", _check_file("first_frame", params["first_frame"])]
    if params.get("last_frame"):
        cmd += ["--last-frame", _check_file("last_frame", params["last_frame"])]
    if params.get("lora"):
        cmd += ["--lora", params["lora"], "--lora-mode", "adapter"]
    if params.get("draft_steps"):
        # 64 倍數畫布限定；4 步模型 ≤2。 upscaler 權重須已在 models/latent_upscale_models/
        cmd += ["--draft-steps", str(int(params["draft_steps"])),
                "--latent-upscaler", "minimax_h3_latent_upscaler_3d_conv_v1_bf16.safetensors"]

    log = Path(job_dir) / "generate.log"
    # 幀數線性：T2VA 4步 362f 實測 581s；ref2va turbo 8步 120f 走到解碼就要 ~800s。
    # 上限寬鬆，run_cli 內部已有 2s 取消輪詢。
    timeout = 900 + int(frames) * 2.5 if ref2va else 600 + int(frames) * 1.5

    step_re = re.compile(r"step (\d+)/(\d+)")
    done_flags = {"decoded and submitted": 0.9, "decoded the audio": 0.95}

    def watch(chunk: str) -> None:
        for line in chunk.splitlines():
            m = step_re.search(line)
            if m:
                progress(0.1 + 0.7 * int(m.group(1)) / int(m.group(2)), "generating")
            else:
                for text, ratio in done_flags.items():
                    if text in line:
                        progress(ratio, "decoding")

    run_cli(cmd, cwd=os.path.dirname(BIN) or ".", log_path=log, env=None,
            timeout=timeout, cancel=cancel, engine="mmh3", on_log=watch)

    out = job_output(job_dir, params, "output.mp4")
    if not os.path.isfile(out):
        tail = Path(log).read_text(errors="replace")[-400:]
        raise RuntimeError(f"mmh3 produced no output; log tail:\n{tail}")
    meta = {"engine": "mmh3", "width": width, "height": height,
            "frames": frames, "steps": steps, "seed": seed}
    if ref2va:
        meta["mode"] = "ref2va-dmad"
        meta["refs"] = len(ref_files) + len(audio_files)
    return {"output_path": out, **meta}
