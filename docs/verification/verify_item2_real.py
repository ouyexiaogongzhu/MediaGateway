#!/usr/bin/env python3
"""Continuity check on the REAL render-all output (not a synthetic case).

The film is 4.7s from two ~2.35s shots, over one 10s music segment.
  per-shot bed  -> shot 2 gets music[0:2.35] again
  one bed       -> shot 2 gets music[2.35:4.7]
So correlate each half of the film against BOTH candidates. Shot audio is mixed
in, so correlation is imperfect -- the point is which candidate wins.
"""
import array, subprocess

FILM = "assets/concat_f04deed8/final.mp4"
MUSIC = "/var/folders/t3/ghc0jtlx26n9tyj409d6h1bc0000gn/T/canvas-row-audio-421870601.wav"


def pcm(path, stream=None):
    cmd = ["ffmpeg", "-v", "error", "-i", path]
    if stream:
        cmd += ["-map", stream]
    cmd += ["-ac", "1", "-ar", "48000", "-f", "s16le", "-"]
    raw = subprocess.run(cmd, capture_output=True).stdout
    a = array.array("h"); a.frombytes(raw); return a


def pearson(x, y):
    n = min(len(x), len(y))
    if n < 1000:
        return 0.0
    x, y = x[:n], y[:n]
    mx, my = sum(x) / n, sum(y) / n
    sxy = sxx = syy = 0
    for i in range(n):
        a, b = x[i] - mx, y[i] - my
        sxy += a * b; sxx += a * a; syy += b * b
    return sxy / (sxx * syy) ** .5 if sxx and syy else 0.0


film = pcm(FILM, "0:a")
music = pcm(MUSIC)
cut = (len(film) // 2 // 48000) * 48000
half = cut
print(f"film {len(film)/48000:.2f}s, music {len(music)/48000:.2f}s, cut at {cut/48000:.2f}s")

# film[0:half] should match music[0:half]
r_first = pearson(film[:half], music[:half])
# film[half:2*half] vs music[0:half]  (restart) and vs music[half:2*half] (continue)
r_restart = pearson(film[half:half * 2], music[:half])
r_continue = pearson(film[half:half * 2], music[half:half * 2])

print(f"  film[0:{cut/48000:.2f}s]    vs music[0:{cut/48000:.2f}s]    = {r_first:+.3f}")
print(f"  film[{cut/48000:.2f}:{2*half/48000:.2f}s] vs music[0:{cut/48000:.2f}s]    = {r_restart:+.3f}  (restart)")
print(f"  film[{cut/48000:.2f}:{2*half/48000:.2f}s] vs music[{cut/48000:.2f}:{2*half/48000:.2f}s] = {r_continue:+.3f}  (continue)")
print("\nVERDICT: " + ("ONE CONTINUOUS BED" if r_continue > r_restart
                     else "RESTARTS (per-shot bed)"))