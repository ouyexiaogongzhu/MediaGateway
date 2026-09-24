"""視頻母帶階梯：9:16 竖版源 → 1080 / 2K / 4K 三檔 10-bit HEVC。

用法：.venv/bin/python scripts/master_ladder.py <in.mp4> [out_dir]
Lanczos 放大（無真細節增益）+ HEVC 10-bit 高碼率母帶（平台交付檔）。
"""
import os
import subprocess
import sys

FF = "/opt/homebrew/bin/ffmpeg"
TARGETS = [  # (寬, 高, 檔名) — 9:16 豎版
    (1080, 1920, "1080p"),
    (1440, 2560, "2k"),
    (2160, 3840, "4k"),
]

src = sys.argv[1]
out_dir = sys.argv[2] if len(sys.argv) > 2 else os.path.dirname(os.path.abspath(src)) or "."
base = os.path.splitext(os.path.basename(src))[0]

os.makedirs(out_dir, exist_ok=True)
for width, height, tag in TARGETS:
    dst = os.path.join(out_dir, f"{base}_{tag}_{width}x{height}.mp4")
    vf = (f"scale={width}:{height}:force_original_aspect_ratio=decrease:flags=lanczos,"
          f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black")
    subprocess.run([FF, "-y", "-v", "error", "-i", src,
                    "-vf", vf,
                    "-c:v", "libx265", "-preset", "slow", "-crf", "16",
                    "-tag:v", "hvc1", "-pix_fmt", "yuv420p10le",
                    "-maxrate", f"{int(height/1080*40)}M",
                    "-bufsize", f"{int(height/1080*80)}M",
                    "-c:a", "copy", "-movflags", "+faststart", dst], check=True)
    print(f"{tag}: {dst} ({os.path.getsize(dst)//1024//1024}MB)")
