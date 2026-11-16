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
BUDGET = int(os.environ.get("MMH3_VRAM_BUDGET", "40"))
PATCH = "minimax_h3_fasth3_vsa_datafree_patch_rank64.safetensors"


def _check_file(label: str, path):
    if not path or not os.path.isfile(path):
        raise ValueError(f"{label} not found: {path}")
    return path


def run(params: dict, job_dir: Path, progress, cancel) -> dict:
    prompt = params.get("prompt")
    if not prompt:
        raise ValueError("prompt is required")
    if params.get("refs"):
        # Ref2VA DiT (34G) 未下載：磁碟不夠。裝好後把 reference 逐個轉成 --reference 即可。
        raise ValueError("mmh3 refs not supported yet (ref2va DiT not downloaded)")

    width = number(params, "width", 544, 256, 2048, int)
    height = number(params, "height", 960, 256, 2048, int)
    if params.get("seconds"):
        frames = max(1, round(float(params["seconds"]) * 24))
    else:
        frames = number(params, "frames", 124, 5, 3600, int)
    steps = number(params, "steps", 4, 1, 50, int)
    seed = seed_of(params, 0)

    cmd = [BIN, "generate", "--models", MODELS, "--prompt", prompt,
           "--width", str(width), "--height", str(height),
           "--frames", str(frames), "--steps", str(steps), "--seed", str(seed),
           "--vram-budget", str(BUDGET),
           "--patch", PATCH,
           "--out", job_output(job_dir, params, "output.mp4")]
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
    # 幀數線性：362f 實測 581s。 上限寬鬆，run_cli 內部已有 2s 取消輪詢。
    timeout = 600 + int(frames) * 1.5

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
    return {"output_path": out, **meta}
