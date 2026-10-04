#!/usr/bin/env python3
"""Item 2, correct instrument: does the music RESTART at the cut?

RMS-dip-at-the-cut is a bad test (the music's own envelope dominates it).
The structural question is: is the audio after the cut the SAME material as
before it?  A per-shot bed re-plays segment [0,3s] at t=3s -> the two halves
are near-identical.  One continuous bed plays different material in the second
half -> the halves decorrelate.

Measured as normalised cross-correlation of the two halves (Pearson on the
raw s16 samples, mean removed).
"""
import array, subprocess

MUSIC = "/Users/vincent/code/MediaGateway/assets/music_582c29c2/output.wav"


def pcm(path):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-map", "0:a",
                          "-ac", "1", "-ar", "48000", "-f", "s16le", "-"],
                         capture_output=True).stdout
    a = array.array("h"); a.frombytes(raw); return a


def pearson(x, y):
    n = min(len(x), len(y))
    x, y = x[:n], y[:n]
    mx, my = sum(x) / n, sum(y) / n
    sxy = sxx = syy = 0
    for i in range(n):
        a, b = x[i] - mx, y[i] - my
        sxy += a * b; sxx += a * a; syy += b * b
    return sxy / (sxx * syy) ** .5 if sxx and syy else 0.0


def best_lag_corr(a, b, maxlag=48000):
    """max |corr| over small lags, to tolerate container padding."""
    best = 0.0
    for lag in range(-2400, 2401, 200):
        if lag >= 0:
            c = pearson(a[lag:], b[:len(b) - lag]) if lag < len(b) else 0.0
        else:
            c = pearson(a[:len(a) + lag], b[-lag:])
        best = max(best, abs(c))
    return best


print("source music duration:", subprocess.run(
    ["ffprobe", "-v", "error", "-show_entries", "format=duration",
     "-of", "csv=p=0", MUSIC], capture_output=True, text=True).stdout.strip(), "s")

src = pcm(MUSIC)
print("source music [0,3s] vs [3,6s] self-similarity:",
      round(best_lag_corr(src[:144000], src[144000:288000]), 4),
      "  (reference: the source itself is not a 3s loop)")

for label, path in [("CONTROL per-shot bed ", "/tmp/cv/per_shot_bed.mp4"),
                    ("NEW /v1/concat 1 bed ", "assets/concat_c8192f8e/final.mp4")]:
    a = pcm(path)
    half = (len(a) // 2 // 48000) * 48000
    c = best_lag_corr(a[:half], a[half:half * 2])
    verdict = "RESTARTS at cut (per-shot bed)" if c > 0.8 else "CONTINUOUS (one bed)"
    print(f"{label}: 1st half vs 2nd half corr = {c:.4f}  -> {verdict}")