#!/usr/bin/env python3
"""Item 2 control: build what the OLD per-shot bed would have produced, so
'one continuous bed' is judged against a measured alternative, not a threshold.

old shape: music mixed into each shot separately (each shot's music trimmed to
that shot's duration), then the shots joined  -> music restarts at every cut.
new shape: shots joined, then ONE music segment looped/trimmed to the whole film.
"""
import array, subprocess

MUSIC = "/Users/vincent/code/MediaGateway/assets/music_582c29c2/output.wav"
SHOTS = ["/tmp/cv/s1.mp4", "/tmp/cv/s2.mp4"]
CUT = 3.0


def pcm(path):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-map", "0:a",
                          "-ac", "1", "-ar", "48000", "-f", "s16le", "-"],
                         capture_output=True).stdout
    a = array.array("h"); a.frombytes(raw); return a


def windows(a, sr=48000, win=0.25):
    n = int(sr * win)
    return [(i / sr, 20 * (sum(v * v for v in a[i:i + n]) / n) ** .5 / 32768)
            for i in range(0, len(a) - n, n)]


def report(label, path):
    w = windows(pcm(path))
    at = lambda t: max(x[1] for x in w if abs(x[0] + .125 - t) < .13)
    others = max(x[1] for x in w if abs(x[0] + .125 - CUT) >= .5)
    print(f"{label:28s} peak={max(x[1] for x in w):6.2f} dBFS  "
          f"cut={at(CUT):6.2f}  elsewhere={others:6.2f}  dip={at(CUT)-others:+5.2f} dB")
    print("   " + " ".join(f"{x[0]:.2f}:{x[1]:.1f}" for x in w))
    return at(CUT) - others


# control: per-shot bed (music mixed into each shot, trimmed to that shot)
parts = []
for i, s in enumerate(SHOTS):
    out = f"/tmp/cv/pershot{i}.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", s, "-i", MUSIC,
                    "-filter_complex",
                    f"[0:a]aformat=sample_rates=48000:channel_layouts=stereo[v0];"
                    f"[1:a]aformat=sample_rates=48000:channel_layouts=stereo,"
                    f"volume=-16.5dB[bg];[v0][bg]amix=inputs=2:normalize=0[a]",
                    "-map", "0:v", "-map", "[a]", "-t", "3",
                    "-c:v", "copy", "-c:a", "aac", out], check=True)
    parts.append(out)
lst = "/tmp/cv/list.txt"
open(lst, "w").write("".join(f"file '{p}'\n" for p in parts))
subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0",
                "-i", lst, "-c", "copy", "/tmp/cv/per_shot_bed.mp4"], check=True)

d_old = report("CONTROL per-shot bed", "/tmp/cv/per_shot_bed.mp4")
d_new = report("NEW one bed (endpoint)", "assets/concat_c8192f8e/final.mp4")
print(f"\ncontrol dip {d_old:+.2f} dB vs endpoint dip {d_new:+.2f} dB")
print("VERDICT: " + ("endpoint is the continuous bed, control is not"
                     if d_new > d_old + 1.0 else "INCONCLUSIVE"))