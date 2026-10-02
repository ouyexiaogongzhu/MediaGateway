#!/usr/bin/env python3
"""ITEM 1 driver: baseline vs kv_ratio=2 across 3 visually different clips, interleaved.

Thin wrapper over scripts/flashvsr_bench.py -- run_arm/check_arm/gate are reused
unchanged so the gate, the memory abort, the chunk parser and the VOID rules stay
the ones already validated 15/15 last round. What this adds is only what the
single-clip series could not do:

  * three different source clips instead of one
  * interleaved per clip (baseline, kv_2, baseline, kv_2...) so thermal drift
    lands on both arms equally
  * md5 of every output -- if an arm is byte-identical to its baseline the knob
    was INERT and its timing measured nothing
  * parse the new `decode:` line so the VAE phase is timed without a second run
  * extract 3 native-resolution frames PER ARM the moment the arm lands, so the
    lead can judge while later arms are still running

Frames are extracted after every arm, not at the end: quality is judged by eye and
the lead is waiting on pictures, not on a table.

Usage:  python3 _flashvsr_kvclips.py [arm_index ...]   (default: all, in order)
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import flashvsr_bench as fb  # noqa: E402

ASSETS = Path.home() / "code/MediaGateway/assets"

# Chosen by eye from a contact sheet of all 35 unique 362f clips, then
# luminance-measured (meanY = mean of scale=1:1 gray bytes over the whole clip).
# The baseline clip (video_6b275c2c, Y56.0) is a single face close-up -- the hardest
# case for a supercaler -- so none of these repeats that content.
#
#   wide shot .... video_44f422ee  Y110.7  3 women full-body in a modern living room,
#                                         deep focus, large camera move across the clip
#   high-freq .... video_cd1a9740  Y160.6  beach at sunset, full body, wet sand + water +
#                                         polka-dot fabric + windblown hair, backlit
#   low light ... video_70d63db0  Y47.7   two people on a bed, sheer black fabric,
#                                         dark interior -- darkest clip in the pool
#
# Luminance spread 47.7 -> 160.6 (3.4x) so "does kv_2 survive dark frames" is
# actually being asked. All three are 362f / 15.08s.
#
# SHAPE DEVIATION (deliberate, flagged for the lead): the brief said 512x288. The
# entire 512x288 landscape pool in this library is one content family -- soft-focus
# skin close-ups -- so that shape cannot supply a wide shot or a high-texture shot.
# Content diversity was the actual point of the test, so variety won over shape.
# 44f422ee and cd1a9740 are 288x512 (the production vertical draft shape), 70d63db0
# is 512x288. Diffusion boxes therefore differ per clip and CROSS-CLIP TIMINGS ARE
# NOT COMPARABLE; only baseline-vs-kv_2 WITHIN a clip is.
CLIPS = [
    {"key": "wide",   "id": "video_44f422ee", "shape": "288x512", "meanY": 110.7,
     "note": "wide interior, 3 people full-body, deep focus, camera move"},
    {"key": "texture", "id": "video_cd1a9740", "shape": "288x512", "meanY": 160.6,
     "note": "beach sunset, full body, sand/water/fabric/hair, backlit"},
    {"key": "dark",   "id": "video_70d63db0", "shape": "512x288", "meanY": 47.7,
     "note": "two people on a bed, sheer black fabric, dark interior"},
]

ARMS = [("baseline", {}), ("kv_2", {"FLASHVSR_KV_RATIO": "2"})]
FRAMES = (30, 180, 330)
SEED = 0
TAG = "kvclips"

DECODE_RE = re.compile(r"^decode: ([\d.]+)s", re.M)


def plan() -> list[dict]:
    """Interleaved: clip1 base, clip1 kv2, clip2 base, clip2 kv2, ..."""
    seq = []
    for c in CLIPS:
        for i, (name, env) in enumerate(ARMS, start=1):
            seq.append({"clip": c, "setting": name, "env": env, "idx": i})
    return seq


def md5(p: Path) -> str | None:
    if not p.is_file():
        return None
    h = hashlib.md5()
    with p.open("rb") as fh:
        for b in iter(lambda: fh.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def extract(out: Path, clip_key: str, setting: str, dest: Path) -> list[str]:
    """Native-resolution frames, no downscaling -- judging happens at 1:1 or not at all."""
    dest.mkdir(parents=True, exist_ok=True)
    got = []
    for n in FRAMES:
        dst = dest / f"{clip_key}_{setting}_f{n}.png"
        rc = subprocess.call([fb._ffmpeg(), "-v", "error", "-y", "-i", str(out),
                              "-vf", f"select=eq(n\\,{n})", "-vframes", "1", str(dst)])
        if rc == 0 and dst.is_file():
            got.append(str(dst))
        else:
            print(f"    EXTRACT FAILED {dst.name} rc={rc}", flush=True)
    return got


def main() -> int:
    want = {int(a) for a in sys.argv[1:]} if len(sys.argv) > 1 else None
    seq = plan()
    fb.OUT.mkdir(parents=True, exist_ok=True)
    rp = fb.OUT / f"results_{TAG}.json"
    # Arms may be run in batches; keep earlier ones so the paired report stays whole.
    results = json.loads(rp.read_text()) if rp.is_file() else []

    for i, item in enumerate(seq, start=1):
        if want and i not in want:
            continue
        c = item["clip"]
        src = ASSETS / c["id"] / "output.mp4"
        if not src.is_file():
            print(f"!! missing {src}")
            continue
        print(f"\n=== [{i}/{len(seq)}] {c['key']} ({c['id']} {c['shape']} Y{c['meanY']}) "
              f"{item['setting']} ===", flush=True)

        r = fb.run_arm({"setting": item["setting"], "env": item["env"], "repeat": item["idx"]},
                       src, SEED, f"{TAG}_{c['key']}")
        r["clip"] = c["key"]
        r["clip_id"] = c["id"]
        r["clip_shape"] = c["shape"]
        r["clip_meanY"] = c["meanY"]
        log = Path(r.get("log", ""))
        if log.is_file():
            m = DECODE_RE.search(log.read_text(errors="replace"))
            r["decode_s"] = float(m.group(1)) if m else None
            inf = re.search(r"^inference: ([\d.]+)s", log.read_text(errors="replace"), re.M)
            r["inference_s"] = float(inf.group(1)) if inf else None
        r["md5"] = md5(Path(r["out"])) if r.get("out") else None
        r["frames"] = extract(Path(r["out"]), c["key"], item["setting"],
                              fb.OUT / TAG / "frames") if r.get("out") and r.get("ok") else []
        results = [x for x in results
                   if not (x.get("clip") == c["key"] and x.get("setting") == item["setting"])]
        results.append(r)
        results.sort(key=lambda x: (x.get("clip", ""), x.get("setting", "")))
        rp.write_text(json.dumps(results, indent=2))
        print(f"    decode={r.get('decode_s')}s inference={r.get('inference_s')}s "
              f"md5={r['md5']} frames={len(r['frames'])}", flush=True)

    report(results)
    return 0


def report(results: list[dict]) -> None:
    print("\n" + "=" * 100)
    hdr = (f"{'clip':<8}{'setting':<10}{'chunks':>7}{'mean/ch':>9}{'min/ch':>8}{'max/ch':>8}"
           f"{'infer':>9}{'decode':>8}{'dec%':>7}{'MB':>8}{'mem_ch':>8}{'mem_wall':>9}  {'md5':<10} verdict")
    print(hdr)
    print("-" * len(hdr))
    for r in results:
        inf, dec = r.get("inference_s"), r.get("decode_s")
        pct = (100 * dec / inf) if (inf and dec) else None
        def c(v, suf="", w=0, p=2):
            return "-".rjust(w) if v is None else f"{v:.{p}f}{suf}".rjust(w)
        print(f"{r.get('clip','?'):<8}{r.get('setting','?'):<10}"
              f"{str(r.get('n_chunks','-')):>7}{c(r.get('mean_chunk_s')):>9}"
              f"{c(r.get('min_chunk_s')):>8}{c(r.get('max_chunk_s')):>8}"
              f"{c(inf,'s',9,1)}{c(dec,'s',8,1)}{c(pct,'%',7,1)}"
              f"{c((r['bytes']/1e6) if r.get('bytes') else None,'',8,2)}"
              f"{c(r.get('minmem_chunks'),'%',8,0)}{c(r.get('minmem_wall'),'%',9,0)}  "
              f"{(r.get('md5') or '-')[:10]:<10} "
              f"{'ACCEPT' if r.get('ok') else 'VOID: '+','.join(r.get('voids',[]) or ['?'])}")

    # Inert-knob check: identical md5 across arms means the setting changed nothing,
    # so its timing is not evidence of anything.
    print()
    for key in {r.get("clip") for r in results}:
        rs = [r for r in results if r.get("clip") == key and r.get("md5")]
        if len(rs) == 2:
            same = rs[0]["md5"] == rs[1]["md5"]
            print(f"  {key:<8} {'INERT -- baseline and kv_2 are BYTE-IDENTICAL' if same else 'outputs differ (knob is live)'}")
    for key in {r.get("clip") for r in results}:
        rs = {r.get("setting"): r for r in results if r.get("clip") == key and r.get("ok")}
        if "baseline" in rs and "kv_2" in rs:
            b, k = rs["baseline"], rs["kv_2"]
            if b.get("mean_chunk_s") and k.get("mean_chunk_s"):
                d = 100 * (k["mean_chunk_s"] - b["mean_chunk_s"]) / b["mean_chunk_s"]
                mb = (k["bytes"] - b["bytes"]) / b["bytes"] * 100 if b.get("bytes") and k.get("bytes") else None
                print(f"  {key:<8} kv_2 vs baseline: {d:+.1f}% mean/chunk"
                      + (f", output {mb:+.2f}% bytes" if mb is not None else "")
                      + "   (TIMING ONLY -- judge the frames)")


if __name__ == "__main__":
    sys.exit(main())