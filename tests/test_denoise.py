"""denoise_audio() self-check — asserts the measured numbers, not just "it ran".

Ground truth re-measured 2026-10-04 on ~/h3_ab4, **absolute gate −55 dBFS**,
same 100 ms frame indices on both sides (量測陷阱第7次: the relative
"median + 15 dB" gate fails in both directions — on h3 clips it swallows quiet
speech as "floor" and reported 7.5–13.7 dB cuts that were never real):
    ho_heels_rt   floor −61.6 → −70.3 dB,  speech −0.2 dB
    sil_ny2       floor −56.4 → −69.2 dB,  speech −0.6 dB
    2a_heels      floor −60.9 → −74.9 dB,  speech −0.6 dB
    sil_roomtone_raw floor −79.7 (already digital silence; must stay clean)
afftdn baseline for comparison: 4 dB noise removed per 11.4 dB speech lost.

Run: .venv/bin/python tests/test_denoise.py   (or: pytest tests/test_denoise.py)
"""
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
import pytest  # noqa: E402

from server import render  # noqa: E402

SR = 32000


def pcm(path):
    raw = subprocess.run(
        [render._bin("ffmpeg", "FFMPEG_BIN"), "-v", "error", "-i", str(path),
         "-f", "s16le", "-ac", "2", "-ar", str(SR), "-"],
        capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype="<i2").reshape(-1, 2).astype(np.float64).T / 32768.0


ABS_GATE_DB = -55.0     # same absolute gate as ~/h3_ab4/hiss.py -55


def frames_db(x):
    mono = np.sqrt((x ** 2).mean(axis=0))
    return np.array([20 * np.log10(mono[i:i + 3200].mean() + 1e-12)
                     for i in range(0, len(mono) - 3200, 3200)])


def levels(x, mask=None):
    """(speech_median_db, floor_median_db) over 100 ms frames.

    **Absolute** gate at −55 dBFS. The old "clip median + 15 dB" relative gate is
    the documented trap (量測陷阱第7次): on h3 material the median sits near speech
    level, so it labels quiet speech as floor — it reported 7.5–13.7 dB cuts and
    "0.0 dB speech loss" for a filter that was actually hitting speech for 0.5–4 dB.

    mask: pass the SOURCE clip's classification when measuring a processed file.
    Re-deriving it per file reclassifies frames the filter quietened and fakes
    both a cut and a speech loss.
    """
    f = frames_db(x)
    if mask is None:
        mask = f >= ABS_GATE_DB
    return np.median(f[mask]), np.median(f[~mask])


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}{('  ' + detail) if detail else ''}")
    if not cond:
        raise SystemExit(1)


def _mask(x):
    return frames_db(x) >= ABS_GATE_DB


def synthetic():
    """white noise floor + a tone burst: the filter must kill one, keep the other"""
    n = SR * 3
    t = np.arange(n) / SR
    # floor at −60 dBFS so the ABSOLUTE −55 gate can actually separate it from the
    # tone; at −35 dBFS floor and tone sit on the same side of the gate and the
    # measurement reports 無法分級 instead of a number.
    floor = np.random.RandomState(0).randn(n) * 10 ** (-60 / 20)
    tone = np.sin(2 * np.pi * 800 * t) * 10 ** (-5 / 20) * (t < 1.0)
    x = np.vstack([floor + tone, floor + tone])
    raw = (x.T * 32767).astype("<i2").tobytes()
    out = render._denoise_pcm(raw, 2.0, 0.05)
    m = _mask(x)
    if m.sum() == 0 or (~m).sum() == 0:
        print("SKIP  synthetic: absolute gate cannot classify this fixture")
        return
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
    # thresholds = the measured floor (absolute gate, paired frames), with a little
    # slack so encoder jitter doesn't flake. Speech budget is the real claim: the
    # pre-fix filter used 0.5–4.0 dB here.
    real_clip("sil_ny2", min_cut=8.0, max_speech_loss=1.5)
    real_clip("ho_heels_rt", min_cut=6.0, max_speech_loss=1.5)
    real_clip("2a_heels", min_cut=9.0, max_speech_loss=1.5)
    real_clip("sil_roomtone_raw", min_cut=0.0, max_speech_loss=1.5)
    print("\ndenoise_audio holds: floor down, speech untouched")


# --- pytest regressions: each one FAILS on the pre-fix implementation ---

def _pcm_of(x):
    return (np.vstack([x, x]).T * 32767).astype("<i2").tobytes()


def test_istft_tail_is_not_digital_silence():
    """Bug: n_frames used floor division, so the last (hop-1) samples fell outside
    every analysis frame and kept `out`'s initial 0 — 7 ms of fabricated silence at
    the end of every clip (a click at the tail)."""
    n = 165600                       # 5.175 s @ 32 kHz, the real clip length
    gap = n - ((n - render._DENOISE_NFFT) // render._DENOISE_HOP
               * render._DENOISE_HOP + render._DENOISE_NFFT)
    assert 0 < gap < render._DENOISE_HOP, "pick a length that leaves a ragged tail"
    x = np.full(n, 0.1)
    out = render._denoise_pcm(_pcm_of(x), 2.0, 0.05)
    tail = out[0, -gap:]
    assert np.abs(tail).max() > 1e-6, (
        f"last {gap} samples are digital zero — ISTFT never covered them")


def test_continuous_speech_survives_the_noise_gate():
    """Bug: the noise-frame gate was 'median + 15 dB' — the same relative threshold
    already documented as a measurement trap (量測陷阱第7次, fails in both
    directions). On continuous speech the median sits AT speech level, so 69–85% of
    frames (speech included) were labelled noise, the noise PSD filled with speech
    energy, and entire speech frames were driven to the gain floor.

    Measured on real h3 output: 0.5–4.0 dB of speech destroyed while the docstring
    claimed 0.0 dB.  Here: 26 dB of speech destroyed before the fix, 0.6 dB after."""
    n = SR * 5
    t = np.arange(n) / SR
    rng = np.random.RandomState(0)
    floor = rng.randn(n) * 10 ** (-40 / 20)
    env = 0.5 + 0.5 * np.sin(2 * np.pi * 0.7 * t)      # never silent, like real dialogue
    tone = sum(np.sin(2 * np.pi * f * t + p)
               for f, p in ((220, 0), (440, 1), (700, 2))) / 3
    x = floor + tone * env * 10 ** (-8 / 20)
    out = render._denoise_pcm(_pcm_of(x), 2.0, 0.05)[0]

    def rms(sig, i):
        return np.sqrt((sig[i:i + 3200] ** 2).mean())

    idxs = range(0, n, 3200)
    a = np.array([rms(x, i) for i in idxs])
    b = np.array([rms(out, i) for i in idxs])
    loud = np.argsort(a)[-max(1, len(a) // 4):]       # where the speech is
    loss = 20 * np.log10(a[loud].mean() / b[loud].mean())
    assert loss <= 1.5, f"speech lost {loss:.1f} dB — the noise gate ate the speech"


def test_real_hiss_floor_actually_drops():
    """The point of the filter. Uses a real h3 output; skips if absent."""
    src = Path.home() / "h3_ab4" / "ho_heels_rt.mp4"
    if not src.exists():
        pytest.skip(f"no fixture at {src}")
    with tempfile.TemporaryDirectory() as tmp:
        dst = Path(tmp) / "out.mp4"
        render.denoise_audio(str(src), str(dst))
        source = pcm(src)
        f = _mask(source)
        # absolute gate, same frame indices both sides (see module docstring)
        mono = np.sqrt((source ** 2).mean(axis=0))
        fr = np.array([20 * np.log10(mono[i:i + 3200].mean() + 1e-12)
                       for i in range(0, len(mono) - 3200, 3200)])
        gate = fr >= -55.0
        if gate.sum() == 0 or (~gate).sum() == 0:
            pytest.skip("cannot classify at -55 dBFS")
        before = np.median(fr[~gate])
        outp = pcm(dst)
        mo = np.sqrt((outp ** 2).mean(axis=0))
        go = np.array([20 * np.log10(mo[i:i + 3200].mean() + 1e-12)
                       for i in range(0, len(mo) - 3200, 3200)])[:len(gate)]
        after = np.median(go[~gate])
        assert before - after >= 5.0, f"floor only moved {before - after:.1f} dB"


if __name__ == "__main__":
    main()