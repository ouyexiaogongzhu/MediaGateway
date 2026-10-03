"""denoise_audio() self-check — asserts the measured numbers, not just "it ran".

Ground truth measured on ~/h3_ab4 (see memory: 嘶聲定案 n=11):
    sil_ny2       floor −40.8 → −48.5 dB,  speech −0.5 dB
    ho_heels_rt   floor −51.5 → −65.1 dB,  speech −0.2 dB
    sil_roomtone_raw floor −79.7 (already digital silence; must stay clean)
afftdn baseline for comparison: 4 dB noise removed per 11.4 dB speech lost.

Run: .venv/bin/python tests/test_denoise.py
"""
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

from server import render  # noqa: E402

SR = 32000


def pcm(path):
    raw = subprocess.run(
        [render._bin("ffmpeg", "FFMPEG_BIN"), "-v", "error", "-i", str(path),
         "-f", "s16le", "-ac", "2", "-ar", str(SR), "-"],
        capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype="<i2").reshape(-1, 2).astype(np.float64).T / 32768.0


def levels(x, mask=None):
    """(speech_median_db, floor_median_db) over 100 ms frames.

    Threshold is the clip's own median + 15 dB — h3's floor moves between
    −25 and −85 dB depending on the prompt, so any fixed gate is wrong.
    mask: pass the SOURCE clip's classification when measuring a processed
    file. Re-deriving it on the output reclassifies frames the filter
    quietened as "speech" and fakes a speech loss (3.6 dB phantom on
    ho_heels_rt, which has only 2 speech frames to begin with).
    """
    mono = np.sqrt((x ** 2).mean(axis=0))
    f = np.array([20 * np.log10(mono[i:i + 3200].mean() + 1e-12)
                  for i in range(0, len(mono) - 3200, 3200)])
    if mask is None:
        mask = f >= np.median(f) + 15
    return np.median(f[mask]), np.median(f[~mask])


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}{('  ' + detail) if detail else ''}")
    if not cond:
        raise SystemExit(1)


def _mask(x):
    mono = np.sqrt((x ** 2).mean(axis=0))
    f = np.array([20 * np.log10(mono[i:i + 3200].mean() + 1e-12)
                  for i in range(0, len(mono) - 3200, 3200)])
    return f >= np.median(f) + 15


def synthetic():
    """white noise floor + a tone burst: the filter must kill one, keep the other"""
    n = SR * 3
    t = np.arange(n) / SR
    floor = np.random.RandomState(0).randn(n) * 10 ** (-35 / 20)
    tone = np.sin(2 * np.pi * 800 * t) * 10 ** (-5 / 20) * (t < 1.0)
    x = np.vstack([floor + tone, floor + tone])
    raw = (x.T * 32767).astype("<i2").tobytes()
    out = render._denoise_pcm(raw, 2.0, 0.05)
    m = _mask(x)
    spk_in, floor_in = levels(x, m)
    spk_out, floor_out = levels(out, m)
    check("synthetic: noise floor drops >= 6 dB",
          floor_out <= floor_in - 6, f"{floor_in:.1f} -> {floor_out:.1f} dB")
    check("synthetic: tone survives within 3 dB",
          abs(spk_out - spk_in) <= 3.0, f"{spk_in:.1f} -> {spk_out:.1f} dB")


def real_clip(name, min_cut, max_speech_loss):
    src = Path.home() / "h3_ab4" / f"{name}.mp4"
    if not src.exists():
        print(f"SKIP  {name} (no fixture at {src})")
        return
    with tempfile.TemporaryDirectory() as tmp:
        dst = Path(tmp) / "out.mp4"
        render.denoise_audio(str(src), str(dst))
        check(f"{name}: output exists and has both streams",
              dst.exists() and dst.stat().st_size > 0)
        source = pcm(src)
        m = _mask(source)
        spk_in, flr_in = levels(source, m)
        spk_out, flr_out = levels(pcm(dst), m)
        cut, loss = flr_in - flr_out, spk_in - spk_out
        check(f"{name}: floor cut >= {min_cut} dB", cut >= min_cut,
              f"{flr_in:.1f} -> {flr_out:.1f} ({cut:+.1f} dB)")
        check(f"{name}: speech loss <= {max_speech_loss} dB", loss <= max_speech_loss,
              f"{spk_in:.1f} -> {spk_out:.1f} ({loss:+.1f} dB)")
        check(f"{name}: beats afftdn (0.35 ratio)", cut / max(loss, 1e-9) > 0.35,
              f"ratio {cut / max(loss, 1e-9):.1f}")


def main():
    synthetic()
    real_clip("sil_ny2", min_cut=5.0, max_speech_loss=3.0)
    real_clip("ho_heels_rt", min_cut=8.0, max_speech_loss=3.0)
    real_clip("sil_roomtone_raw", min_cut=0.0, max_speech_loss=3.0)
    print("\ndenoise_audio holds: floor down, speech untouched")


if __name__ == "__main__":
    main()