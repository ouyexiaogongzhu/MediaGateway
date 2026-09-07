"""768x1344 → 1080x1920 lanczos 放大 + 高码率重封装（测试用）。

用法：.venv/bin/python scripts/upscale_1080.py <in.mp4> [out.mp4]
注意：lanczos 是插值放大，真实细节不增加；码率拉高只为平台校验。
"""
import os
import subprocess
import sys

src = sys.argv[1]
dst = sys.argv[2] if len(sys.argv) > 2 else os.path.splitext(src)[0] + "_1080.mp4"

subprocess.run(["ffmpeg", "-y", "-v", "error",
                "-i", src,
                # 等比缩放适配 1080x1920 后居中补边：768x1344 是 4:7，硬 scale 成 9:16 会变形
                "-vf", "scale=1080:1920:force_original_aspect_ratio=decrease:flags=lanczos,"
                       "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black",
                "-c:v", "libx264", "-preset", "slow", "-crf", "15",
                "-maxrate", "12M", "-bufsize", "24M",
                "-pix_fmt", "yuv420p", "-c:a", "copy",
                "-movflags", "+faststart", dst], check=True)

for f in (src, dst):
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                            "format=duration,bit_rate", "-show_entries",
                            "stream=width,height", "-of", "csv=p=0", f],
                           capture_output=True, text=True).stdout.strip()
    print(f"{os.path.basename(f)}: {probe} | {os.path.getsize(f) // 1024}KB")
