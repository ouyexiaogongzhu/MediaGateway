"""Image worker: MiniMax H3-Image via the mmh3 CLI (Rust/Metal, no sd.cpp).

6-step distilled DiT (no CFG), 4-20MP working range, best ~16MP. Requests
under --min-generation-area (2048² default) are generated on a 2048² canvas
at the same aspect and Lanczos-resized down, so small sizes still cost a
2048² generation. Edits take up to 16 --reference images; the prompt refers
to them as <Picture 1>, <Picture 2> ...

Peak RSS measured 21-22 GiB → run with --vram-budget 40 (same rationale as
mmh3.py: unbounded, the process gets SIGKILLED when the IDE competes).
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from ._util import job_output, number, run_cli, seed_of

TYPE = "mmh3_image"
MEM_GB = 22.0  # 實測峰值 RSS 21.1-22.2GiB

BIN = os.environ.get("MMH3_BIN", "/Users/vincent/tool/mmh3/target/release/mmh3")
MODELS = os.environ.get("MMH3_MODELS", "/Users/vincent/tool/mmh3/models")
BUDGET = int(os.environ.get("MMH3_VRAM_BUDGET", "40"))


def _check_file(label: str, path):
    if not path or not os.path.isfile(path):
        raise ValueError(f"{label} not found: {path}")
    return path


def run(params: dict, job_dir: Path, progress, cancel) -> dict:
    prompt = params.get("prompt")
    if not prompt:
        raise ValueError("prompt is required")
    # H3-Image 期待詳細英文全圖描述，prompt 原樣進文本編碼器（無模板）。
    width = number(params, "width", 2048, 512, 6400, int)
    height = number(params, "height", 2048, 512, 6400, int)
    seed = seed_of(params, 0)

    refs = []
    for r in params.get("refs") or []:
        path = r["path"] if isinstance(r, dict) else r
        refs.append(_check_file("reference", path))

    out = job_output(job_dir, params, "output.png")
    cmd = [BIN, "image", "--models", MODELS, "--prompt", prompt,
           "--width", str(width), "--height", str(height),
           "--seed", str(seed), "--vram-budget", str(BUDGET),
           "--out", out]
    for path in refs:  # 編輯模式：--reference 可重複（至 16）
        cmd += ["--reference", path]
    if params.get("min_generation_area"):
        # 分鏡草稿快車道：畫布=請求尺寸（默認政策是放大到 2048² 級再縮）。
        # 無 refs 時成本全隨畫布：1024×576 = 19s vs 預設政策 52s。
        # 帶 refs 時前 3 步是 ref token 綁定、縮畫布只省 ~17%——別用它。
        cmd += ["--min-generation-area", str(int(params["min_generation_area"]))]

    log = Path(job_dir) / "image.log"
    # 4096² 文檔參考 M6 140s；M5 Pro 預算寬鬆。小尺寸會先在 2048² 畫布生成再縮。
    timeout = 900

    step_re = re.compile(r"step (\d+)/(\d+)")

    def watch(chunk: str) -> None:
        for line in chunk.splitlines():
            m = step_re.search(line)
            if m:
                progress(0.1 + 0.8 * int(m.group(1)) / int(m.group(2)), "generating")

    progress(0.1, "generating")
    run_cli(cmd, cwd=os.path.dirname(BIN) or ".", log_path=log, env=None,
            timeout=timeout, cancel=cancel, engine="mmh3-image", on_log=watch)

    if not os.path.isfile(out):
        tail = Path(log).read_text(errors="replace")[-400:]
        raise RuntimeError(f"mmh3 image produced no output; log tail:\n{tail}")
    return {"output_path": out, "engine": "mmh3-image",
            "width": width, "height": height, "seed": seed,
            "refs": len(refs)}
