#!/usr/bin/env python3
"""FlashVSR knob-series bench: gate -> run arm -> accept/reject, with per-chunk telemetry.

Targets the only surface left with headroom (diffusion is 94.9% of the run):
    examples/WanVSR/upscale_cli.py:53-54   topk_ratio / kv_ratio / local_range

Subcommands:
    gate            pre-arm gate. PASS/FAIL + the two numbers it gated on.
    check <log>     acceptance check for one arm. VOIDs, never silently reports.
    plan            interleaved arm order (noise-floor design). Prints, runs nothing.
    run             execute the series. REQUIRES the GPU; gates before every arm.
    selftest        negative tests: break a report 4 ways, confirm each is caught.

THE PARSER TRAP: tqdm emits "\\r" immediately before each new line, so every
per-chunk line is glued onto the tail of the progress bar. `grep -c '^chunk'`
returns 0 on a log that holds 43 chunk lines. Fix: never anchor to a line start --
scan the whole text with finditer. A line-anchored parser reads a healthy run as
corrupt and would VOID every arm for the wrong reason.

Stdlib only. No GPU touched by gate/check/plan/selftest.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

HOME = Path(os.environ.get("FLASHVSR_HOME", "/Users/vincent/tool/FlashVSR"))
RUNDIR = HOME / "examples" / "WanVSR"
OUT = Path(os.environ.get("FVSR_BENCH_OUT", str(Path.home() / "code/MediaGateway/data/flashvsr_bench")))

# Pinned by team-lead 2026-10-02: the production 15s shape, matching the source
# geometry of the byte-identical pair in the pre-reboot logs, so results stay
# continuous with everything measured before. Do NOT vary this between arms --
# content does not change DiT compute at fixed res/frames, but it does change
# encode time and every quality comparison.
# Verified: 512x288, 362f, 24fps, 15.083s -> infer 1280x720, final 1920x1080.
PINNED_INPUT = Path(os.environ.get(
    "FVSR_BENCH_SRC",
    str(Path.home() / "code/MediaGateway/assets/video_6b275c2c/output.mp4")))

# Gate thresholds. HARD CONSTRAINT (user, 2026-10-02): cap total memory at 40GiB so
# the system and IDE do not crash. This SUPERSEDES the earlier "free >= 85%" rule --
# 85% free of 48GiB is only ~7GiB used, which is stricter than asked for and would
# refuse arms that are perfectly fine. The stated requirement is used <= 40GiB.
MEM_USED_MAX_GIB = 40.0   # refuse if used > this
GPU_MAX = 25.0            # IOAccelerator "Device Utilization %"; desktop compositing idles 0-15, real compute is 99-100
# In-arm hard abort, per the same instruction: treat memory_pressure free dropping
# below this as "kill our own process, report, do not retry". System stability outranks
# measurement completeness -- a VOID arm is a legitimate result.
MEM_ABORT_PCT = 8.0       # ~44GiB used; the pre-arm gate at 40GiB is tighter, so this only fires if memory climbs during a run
MONITOR_POLL_S = 5.0      # in-arm memory sampling interval
ARM_TIMEOUT_S = 3600.0    # wall-clock ceiling per arm; matches the gateway's flashvsr DEFAULT_TIMEOUT.
                          # The bench calls upscale_cli directly and so bypasses that guard --
                          # without one here a hung arm loops forever with no escape.
FOREIGN_CPU_MIN = 50.0    # a top= process must burn this much CPU before it is reported as a
                          # contention suspect. Desktop compositing idles ~17%; real compute is 99-100%.
# NOTE: swap is deliberately never read. Swap usage is a lagging artifact of a
# pressure event that already happened; gating on it would only veto arms late.

# Per-chunk telemetry line, flashvsr_tiny.py:393
#   chunk 12 t=101.4s loadavg=3.21/2.90/2.40 gpu=99% memfree=64% top=python:98.0
# Scanned with finditer over the whole text -- see THE PARSER TRAP above.
# top= runs to end-of-line, not \S+: `ps -o comm` can hold spaces ("Google Chrome
# Helper"), and truncating it would invent a phantom non-python process.
# memfree= is memory_pressure's own free percentage -- the in-arm abort signal.
CHUNK_RE = re.compile(
    r"chunk (\d+) t=([\d.]+)s "
    r"loadavg=([\d.]+)/([\d.]+)/([\d.]+) "
    r"gpu=(\d+|\?)% memfree=(\d+|\?)% top=(.*)$", re.M)

# infer_mps.py:93 declares what the decoder is CONTRACTED to emit:
#     idx = list(range(total)) + [total - 1] * 4   <- 4 repeats of the last frame, so the
#                                                    sliding window has a valid tail
#     F = largest_8n1_leq(len(idx)); print(f"target frames (8n-3): {F - 4}")
# So saved is compared against F-4 (the DECLARED target), NEVER against the F on the
# `infer WxH Ff` banner. F-4 is smaller than F by design; 361 -> 357 is the contract,
# not a dropped frame. Measured 2026-10-02: both arms of both pairs declared 357 and
# saved 357.
#
# This check did not exist until then, which is why `96723d6d` -- declared 357,
# delivered 354, exit code 0, plausible file size -- passed the bench. It is the exact
# failure the declaration exists to catch, and it was invisible because the check was
# absent rather than wrong.
DECL_RE = re.compile(r"^target frames \(8n-3\): (\d+)", re.M)
SAVED_RE = re.compile(r"^saved (\S+)", re.M)


# ---------------------------------------------------------------- gate

def _mem_free_pct() -> float | None:
    try:
        out = subprocess.run(["memory_pressure"], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return None
    m = re.search(r"System-wide memory free percentage:\s*(\d+)%", out)
    return float(m.group(1)) if m else None


def _total_bytes() -> int | None:
    try:
        out = subprocess.run(["sysctl", "-n", "hw.memsize"],
                             capture_output=True, text=True, timeout=10).stdout
        return int(out.strip())
    except Exception:
        return None


def used_gib() -> float | None:
    """Used memory in GiB, per the stated requirement: (hw.memsize - free_bytes)/2**30.

    free_bytes is memory_pressure's free PERCENTAGE of total -- NOT its "Pages free"
    field and NOT vm_stat's. Those count only genuinely-unallocated pages (~0.7GiB on
    an idle 48GiB box) and would read as 98% consumed on an idle machine, refusing
    every arm. memory_pressure already accounts for inactive/speculative/purgeable.
    """
    pct, total = _mem_free_pct(), _total_bytes()
    if pct is None or total is None:
        return None
    return (total - total * pct / 100.0) / 2 ** 30


def _gpu_pct() -> float | None:
    try:
        out = subprocess.run(["ioreg", "-r", "-d 1", "-c", "IOAccelerator"],
                             capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return None
    m = re.search(r'"Device Utilization %"=(\d+)', out)
    return float(m.group(1)) if m else None


def gate(verbose: bool = True) -> bool:
    """Pre-arm gate: used <= 40GiB AND gpu <= 25%. Never consults swap."""
    used, gpu = used_gib(), _gpu_pct()
    if used is None or gpu is None:
        if verbose:
            print("GATE FAIL: probe unavailable (used=%s gpu=%s) -- refusing to guess"
                  % (used, gpu))
        return False
    ok = used <= MEM_USED_MAX_GIB and gpu <= GPU_MAX
    if verbose:
        print(f"used {used:.1f}GiB (need <={MEM_USED_MAX_GIB:.0f})  "
              f"gpu {gpu:.0f}% (need <={GPU_MAX:.0f})  "
              f"memfree {_mem_free_pct():.0f}%  loadavg {os.getloadavg()[0]:.2f}"
              f"  ->  {'PASS' if ok else 'FAIL'}")
    return ok


def gate_wait(quiet_secs: int = 900, poll: int = 30, verbose: bool = True) -> bool:
    """Block until the gate passes, or give up. Keeps the retry policy in one place."""
    t0 = time.time()
    while True:
        if gate(verbose=verbose):
            return True
        if time.time() - t0 > quiet_secs:
            if verbose:
                print(f"gate: gave up after {quiet_secs}s")
            return False
        if verbose:
            print(f"  ...waiting {poll}s")
        time.sleep(poll)


# ---------------------------------------------------------------- parser

def parse_chunks(text: str) -> list[dict]:
    """Every chunk line in the log, in file order. Line-anchoring would return []."""
    return [{"idx": int(m.group(1)), "t": float(m.group(2)),
             "load1": float(m.group(3)), "load5": float(m.group(4)), "load15": float(m.group(5)),
             "gpu": None if m.group(6) == "?" else int(m.group(6)),
             "memfree": None if m.group(7) == "?" else int(m.group(7)),
             "top": m.group(8).strip()}
            for m in CHUNK_RE.finditer(text)]


def _has(text: str, needle: str) -> bool:
    return needle in text


def count_frames(path: str | Path) -> int | None:
    """Frames ACTUALLY decodable from the saved file.

    -count_frames decodes the stream; the container's nb_frames header is a number the
    muxer wrote down and is not evidence of anything. The 96723d6d class of bug is
    exactly "header says 357, decode yields 354", so only a real count can see it.
    """
    exe = _ffmpeg()
    probe = exe.replace("ffmpeg", "ffprobe")
    try:
        out = subprocess.run([probe, "-v", "error", "-select_streams", "v:0",
                              "-count_frames", "-show_entries",
                              "stream=nb_read_frames", "-of", "csv=p=0", str(path)],
                             capture_output=True, text=True, timeout=300).stdout.strip()
    except Exception:
        return None
    try:
        return int(out.splitlines()[0])
    except (IndexError, ValueError):
        return None


def check_frames(text: str) -> tuple[list[str], int | None, int | None]:
    """(voids, declared, saved) -- saved vs the DECLARED target, not vs the infer banner.

    Only meaningful once the run reached `saved`; a run that never finished is already
    voided by NO_COMPLETE, and probing a file it never wrote would invent a second,
    misleading reason. Once `saved` IS claimed, though, the declaration must be present
    and must agree -- absence of the declaration is not evidence that the count was right.
    """
    m_saved = SAVED_RE.search(text)
    if not m_saved:
        return [], None, None          # not finished; NO_COMPLETE owns this case
    m = DECL_RE.search(text)
    if not m:
        return ["FRAME_UNDECLARED (run saved output but never declared a frame target)"], None, None
    declared = int(m.group(1))
    got = count_frames(m_saved.group(1))
    if got is None:
        return [f"FRAME_UNPROBED (could not count frames in {m_saved.group(1)})"], declared, None
    if got != declared:
        return [f"FRAME_COUNT (declared {declared}, decoded {got})"], declared, got
    return [], declared, got


def _cpu_of(top: str) -> float:
    """%CPU from a `name:pct` telemetry field; 0.0 when unparseable."""
    try:
        return float(top.rsplit(":", 1)[1])
    except (IndexError, ValueError):
        return 0.0


# ---------------------------------------------------------------- acceptance

def check_arm(log_path: str | Path) -> dict:
    """Accept or VOID one arm. A void must mean 'do not trust this number', never
    'the code got slower' -- so every void carries the reason it voided."""
    p = Path(log_path)
    text = p.read_text(errors="replace") if p.is_file() else ""
    ch = parse_chunks(text)
    voids: list[str] = []

    if not p.is_file():
        voids.append("NO_LOG")
    if not ch:
        # The regression this guards: instrumentation silently stops emitting
        # (reverted patch, exception swallowed, stdout redirected) and the arm
        # still produces a plausible-looking wall time from the tail prints.
        voids.append("NO_CHUNKS")

    # Contiguous 0..N-1, in order. A gap means we lost chunks; the run's mean
    # would then be computed over a survivorship-biased subset.
    idxs = [c["idx"] for c in ch]
    if ch and idxs != list(range(len(idxs))):
        missing = sorted(set(range(max(idxs) + 1)) - set(idxs))
        voids.append(f"SEQ (expected 0..{len(idxs)-1}, got {idxs[0]}..{idxs[-1]}"
                     + (f", missing {missing[:6]}" if missing else "") + ")")

    # t= is cumulative wall since chunk 0. Non-monotonic means the clock or the
    # line ordering is corrupt, so every per-chunk delta derived from it is junk.
    if any(b["t"] < a["t"] for a, b in zip(ch, ch[1:])):
        voids.append("T_REGRESS (cumulative t= went backwards)")

    # The run has to have actually finished, or 'faster' just means 'crashed early'.
    if not _has(text, "saved ") or not _has(text, "total:"):
        voids.append("NO_COMPLETE (missing 'saved'/'total:' -- run did not finish)")

    # Saved frame count vs the DECLARED target. Separate from NO_COMPLETE above: that
    # asks "did it finish", this asks "did it finish with the right number of frames".
    # A silently truncated run exits 0, prints `saved`, prints `total:` and writes a
    # plausible-sized file -- it is indistinguishable from a good arm without this.
    fvoids, declared, got = check_frames(text)
    voids.extend(fvoids)

    # Second line of defence behind the in-run monitor: a 5s poll can miss a dip
    # that the per-chunk telemetry caught. A completed arm that breached the
    # memory ceiling is VOID even though the process exited 0.
    memfrees = [c["memfree"] for c in ch if c["memfree"] is not None]
    low = [c for c in ch if c["memfree"] is not None and c["memfree"] < MEM_ABORT_PCT]
    if low:
        voids.append(f"MEM_BREACH (memfree {min(c['memfree'] for c in low)}% "
                     f"< {MEM_ABORT_PCT:.0f}% on chunk {low[0]['idx']})")
    # Chunks present but no memory reading in any of them: the ceiling was never
    # actually observed, so the arm ran with the safety net silently absent.
    # Same principle as NO_CHUNKS -- absence of instrumentation is not evidence.
    if ch and not memfrees:
        voids.append("MEM_UNMONITORED (no chunk carried a memfree reading)")

    durs = [b["t"] - a["t"] for a, b in zip(ch, ch[1:])]
    hot = [c for c in ch if c["gpu"] is not None and c["gpu"] >= 60]
    # Attribution: a speed drop co-occurring with high GPU util has a candidate
    # cause in top=. Without top= alongside, 'gpu' is just a mystery number.
    # Threshold matters: desktop compositing (WindowServer ~17%) and idle agents sit
    # far below real compute, so listing them buries the signal. Our own MPS work
    # often does NOT show as top CPU at all, so "python holds it" is not guaranteed.
    foreign = [c for c in hot
               if not re.match(r"[\w.-]*(python|Python)[\w.-]*:", c["top"])
               and _cpu_of(c["top"]) >= FOREIGN_CPU_MIN]

    return {
        "log": str(p), "ok": not voids, "voids": voids,
        "n_chunks": len(ch),
        "declared_frames": declared, "saved_frames": got,
        "diffusion_s": ch[-1]["t"] if ch else None,
        "mean_chunk_s": round(sum(durs) / len(durs), 2) if durs else None,
        "max_chunk_s": round(max(durs), 2) if durs else None,
        "min_chunk_s": round(min(durs), 2) if durs else None,
        "mean_load1": round(sum(c["load1"] for c in ch) / len(ch), 2) if ch else None,
        "minmem_chunks": min(memfrees) if memfrees else None,
        "hot_gpu_chunks": len(hot),
        "foreign_top": sorted({c["top"] for c in foreign}),
    }


def fmt_check(v: dict) -> str:
    head = "ACCEPT" if v["ok"] else "VOID"
    line = (f"{head} {v['log']}  chunks={v['n_chunks']} "
            f"diffusion={v['diffusion_s']}s mean/chunk={v['mean_chunk_s']}s "
            f"frames={v.get('declared_frames')}/{v.get('saved_frames')} "
            f"minmem_chunks={v['minmem_chunks']}%")
    if v["voids"]:
        line += "\n    VOID: " + "; ".join(v["voids"])
    if v["hot_gpu_chunks"]:
        line += (f"\n    gpu>=60% on {v['hot_gpu_chunks']} chunks; "
                 f"non-python top: {v['foreign_top'] or 'none (python holds it)'}")
    return line


# ---------------------------------------------------------------- plan

# Interleaving is the whole point. Order is settings-outer/repeat-inner with the
# baseline re-measured at the START and END of the series: the end-baseline is the
# drift check. If baseline_start and baseline_end differ by more than the noise
# floor, the box moved under us and every arm between them is uninterpretable.
#
# Why 3 repeats: the reference excursion (chunks 17-21) was a 1.60x band lasting
# ~5 of 43 chunks. Arm-level n=1 cannot separate that from a real effect.
SETTINGS = [
    ("baseline",  {}),                          # production values, verbatim
    ("topk_1.5",  {"FLASHVSR_TOPK_RATIO": "1.5"}),
    ("kv_2",      {"FLASHVSR_KV_RATIO": "2"}),
    ("lr_7",      {"FLASHVSR_LOCAL_RANGE": "7"}),
    ("topk_1.0",  {"FLASHVSR_TOPK_RATIO": "1.0"}),
]
REPEATS = 3


def plan(verbose: bool = True) -> list[dict]:
    seq = []
    for r in range(REPEATS):
        for name, env in SETTINGS:
            seq.append({"repeat": r + 1, "setting": name, "env": env})
    if verbose:
        print(f"{len(seq)} arms = {len(SETTINGS)} settings x {REPEATS} interleaved repeats")
        print(f"settings: {', '.join(n for n, _ in SETTINGS)}")
        print(f"~{len(seq) * 8.5:.0f} min of GPU time (8.5 min/arm baseline)\n")
        for a in seq:
            envs = " ".join(f"{k}={v}" for k, v in a["env"].items()) or "(production defaults)"
            print(f"  r{a['repeat']}  {a['setting']:<10} {envs}")
    return seq


# ---------------------------------------------------------------- run

def run_arm(seq_item: dict, src: Path, seed: int, tag: str) -> dict:
    """One gated arm. Gates, runs upscale_cli.py under memory supervision,
    accepts or voids the log."""
    name, envs = seq_item["setting"], seq_item["env"]
    outdir = OUT / f"{tag}_r{seq_item['repeat']}_{name}"
    outdir.mkdir(parents=True, exist_ok=True)
    log = outdir / "arm.log"
    print(f"\n=== arm {tag} r{seq_item['repeat']} {name} ===", flush=True)

    if not gate_wait():
        return {"setting": name, "repeat": seq_item["repeat"], "ok": False,
                "voids": ["GATE_FAIL"]}

    env = dict(os.environ)
    env.update(envs)
    env["PYTHONPATH"] = os.pathsep.join(
        p for p in (str(HOME), os.environ.get("PYTHONPATH")) if p)
    cmd = [str(HOME / ".venv/bin/python"), "upscale_cli.py", str(src),
           str(outdir / "output.mp4"), "--resolution", "1080", "--seed", str(seed)]

    t0 = time.time()
    min_memfree = 100.0
    monitored = False
    aborted = False
    timed_out = False
    # start_new_session so the whole process group can be killed: upscale_cli
    # shells out to ffmpeg for mux, and killing only the parent orphans it.
    with log.open("w") as fh:
        proc = subprocess.Popen(cmd, cwd=str(RUNDIR), env=env, stdout=fh,
                                stderr=subprocess.STDOUT, start_new_session=True)
        while proc.poll() is None:
            time.sleep(MONITOR_POLL_S)
            if time.time() - t0 > ARM_TIMEOUT_S:
                timed_out = True
                break
            pct = _mem_free_pct()
            if pct is not None:
                monitored = True
                min_memfree = min(min_memfree, pct)
                if pct < MEM_ABORT_PCT:
                    aborted = True
                    break
        if aborted or timed_out:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(os.getpgid(proc.pid), sig)
                except (ProcessLookupError, AttributeError):
                    break
                time.sleep(3)
                if proc.poll() is not None:
                    break
            try:
                proc.kill()
            except Exception:
                pass
    wall = time.time() - t0

    if timed_out:
        v = check_arm(log)
        print(f"TIMEOUT arm exceeded {ARM_TIMEOUT_S:.0f}s at {wall:.0f}s -- killed\n"
              f"{fmt_check(v)}", flush=True)
        return {"setting": name, "repeat": seq_item["repeat"], "ok": False,
                "voids": ["TIMEOUT"], "log": str(log), "wall_s": round(wall, 1),
                "minmem_wall": min_memfree}

    if aborted:
        # Reported, NOT retried. Stability outranks completeness: a VOID arm that
        # aborted at the ceiling is a legitimate result.
        v = check_arm(log)
        print(f"ABORT memfree fell to {min_memfree:.0f}% (<{MEM_ABORT_PCT:.0f}%) at "
              f"{wall:.0f}s -- killed arm, not retrying\n{fmt_check(v)}", flush=True)
        return {"setting": name, "repeat": seq_item["repeat"], "ok": False,
                "voids": ["MEM_ABORT"], "log": str(log),
                "minmem_wall": min_memfree, "wall_s": round(wall, 1)}

    rc = proc.returncode
    base = {"setting": name, "repeat": seq_item["repeat"], "wall_s": round(wall, 1),
            # minmem_wall is sampled every MONITOR_POLL_S across the WHOLE run, so it
            # sees init_pipeline/prepare_input. minmem_chunks comes from per-chunk
            # telemetry, which only begins at chunk 0 -- AFTER model load. Never merge
            # these: the chunk column is structurally blind to the largest memory
            # consumer in the run, and a single "min" would hide that.
            "minmem_wall": min_memfree, "log": str(log)}
    if rc != 0:
        return {**base, "ok": False, "voids": [f"EXIT_{rc}"]}
    if not monitored:
        # Never got a single memory reading: the ceiling was never observed, so
        # the arm completed with the safety net silently absent. Not a pass.
        v = check_arm(log)
        print(fmt_check(v), flush=True)
        return {**base, **v, "ok": False,
                "voids": v["voids"] + ["MEM_UNMONITORED (monitor got no reading)"]}
    v = check_arm(log)
    v.update({**base, "out": str(outdir / "output.mp4")})
    out_mp4 = outdir / "output.mp4"
    if out_mp4.is_file():
        v["bytes"] = out_mp4.stat().st_size
    print(fmt_check(v), flush=True)
    return v


def run(src: Path, seed: int, tag: str) -> list[dict]:
    if not src.is_file():
        sys.exit(f"input not found: {src}")
    OUT.mkdir(parents=True, exist_ok=True)
    seq = plan(verbose=False)
    print(f"running {len(seq)} arms -> {OUT}\n")
    results = []
    for item in seq:
        results.append(run_arm(item, src, seed, tag))
        (OUT / f"results_{tag}.json").write_text(json.dumps(results, indent=2))
    accepted = [r for r in results if r.get("ok")]
    print(f"\n{len(accepted)}/{len(results)} arms ACCEPTED")
    summarize(results)
    return results


# ---------------------------------------------------------------- summarize

def summarize(results: list[dict]) -> None:
    """The results table. EVERY arm appears -- accepted, voided or aborted.

    A missing row is indistinguishable from an arm that was never attempted, which
    is exactly the silent-absence failure the report is meant to rule out. Aborts
    carry their reason and the min memfree that triggered them.
    """
    # A series that dies mid-way leaves fewer rows than planned, and a short table
    # looks exactly like a complete one. Say so instead -- in the HEADER, where a
    # reader who stops at the summary still sees it. Silent absence in the costume
    # of a result is the failure this whole campaign is about.
    expected = len(SETTINGS) * REPEATS
    if len(results) < expected:
        done = {(r.get("setting"), r.get("repeat")) for r in results}
        missing = [(n, rep) for rep in range(1, REPEATS + 1)
                   for n, _ in SETTINGS if (n, rep) not in done]
        banner = (f"!! INCOMPLETE SERIES: {len(results)}/{expected} arms ran !!")
        print("!" * len(banner))
        print(banner)
        print("never attempted: " + ", ".join(f"{n} r{rp}" for n, rp in missing))
        print("!" * len(banner))
        print()

    hdr = (f"{'setting':<10} {'r':>1} {'chunks':>6} {'diffusion':>9} {'mean/ch':>8} "
           f"{'max/ch':>7} {'load1':>6} {'mem_ch':>7} {'mem_wall':>8} {'MB':>7}  verdict")
    print(hdr)
    print("-" * len(hdr))
    for r in results:
        setting, rep = r.get("setting", "?"), r.get("repeat", "?")
        ok = r.get("ok")
        verdict = "ACCEPT" if ok else "VOID: " + ",".join(r.get("voids", []) or ["?"])
        def cell(v, suffix="", width=0, prec=1):
            if v is None:
                return "-".rjust(width)
            return f"{v:.{prec}f}{suffix}".rjust(width)
        print(f"{setting:<10} {rep:>1} {str(r.get('n_chunks', '-')):>6} "
              f"{cell(r.get('diffusion_s'), 's', 9)} "
              f"{cell(r.get('mean_chunk_s'), 's', 8, 2)} "
              f"{cell(r.get('max_chunk_s'), 's', 7, 2)} "
              f"{cell(r.get('mean_load1'), '', 6, 2)} "
              f"{cell(r.get('minmem_chunks'), '%', 7, 0)} "
              f"{cell(r.get('minmem_wall'), '%', 8, 0)} "
              f"{cell((r['bytes'] / 1e6) if r.get('bytes') else None, '', 7, 2)}  {verdict}")

    acc = [r for r in results if r.get("ok")]
    print(f"\n{len(acc)}/{len(results)} ACCEPTED")
    print("mem_ch = free% from per-chunk telemetry (starts at chunk 0, so it EXCLUDES "
          "init_pipeline/prepare_input).\n"
          "       mem_wall = free% sampled every 5s across the whole run, model load "
          "included.\n"
          "       mem_wall is the lower number on every arm. The gap is the model-load "
          "transient; do not merge them.")

    # Settings beating baseline on mean/chunk. Timing alone proves nothing -- a
    # faster knob can damage the latent exactly as h3's core_reuse=4 did -- so these
    # are the frames that must be judged by eye before any of them is called a win.
    base_vals = [r["mean_chunk_s"] for r in acc
                 if r.get("setting") == "baseline" and r.get("mean_chunk_s")]
    if base_vals:
        bmean = sum(base_vals) / len(base_vals)
        faster = [r for r in acc if r.get("mean_chunk_s")
                  and r["mean_chunk_s"] < bmean]
        if faster:
            print(f"\nbaseline mean/chunk {bmean:.2f}s over n={len(base_vals)}; "
                  f"{len(faster)} arm(s) beat it -- JUDGE THESE FRAMES BY EYE:")
            for r in sorted(faster, key=lambda x: x["mean_chunk_s"]):
                pct = 100 * (bmean - r["mean_chunk_s"]) / bmean
                print(f"  {r['setting']:<10} r{r['repeat']}  "
                      f"{r['mean_chunk_s']:.2f}s  ({pct:.1f}% faster)  "
                      f"{r['bytes']/1e6:.2f} MB" if r.get("bytes") else
                      f"  {r['setting']:<10} r{r['repeat']}  {r['mean_chunk_s']:.2f}s "
                      f"({pct:.1f}% faster)")
        else:
            print(f"\nno arm beat baseline ({bmean:.2f}s over n={len(base_vals)})")

    # Noise floor: the baseline spread across repeats is the number the campaign
    # never had. Meaningless unless every baseline repeat was accepted.
    #
    # Reported as a RATIO with its n, never as a bare percentage. A lone "6%" carries
    # no n, reads as a universal constant, and invites exactly the reuse that made
    # the retracted 14-86% figure wrong. "1.08x over n=3" cannot be misread that way.
    base = [r for r in acc if r.get("setting") == "baseline"]
    vals = [r["mean_chunk_s"] for r in base if r.get("mean_chunk_s")]
    if len(vals) > 1:
        lo, hi = min(vals), max(vals)
        ratio = hi / lo
        print(f"\nnoise floor: baseline spread {ratio:.2f}x over n={len(vals)} accepted")
        print(f"  per-chunk mean between {lo:.2f}s (fastest) and {hi:.2f}s (slowest)")
        print(f"  any knob effect under {ratio:.2f}x is inside this spread -- treat as noise")
    else:
        print(f"\nnoise floor: NOT ESTABLISHED "
              f"({len(vals)} accepted baseline repeat(s); need >=2)")
        print("  no baseline spread can be quoted from a single run")


# ---------------------------------------------------------------- extract

def _ffmpeg() -> str:
    """First an ffmpeg on PATH, then the one imageio_ffmpeg bundles. Kept stdlib so
    every subcommand runs under plain `python3` -- only the FlashVSR venv has
    imageio_ffmpeg, and the bench must not require switching interpreters."""
    import shutil
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        raise SystemExit("no ffmpeg found on PATH and imageio_ffmpeg unavailable")


def extract(results: list[dict], frame: int = 180) -> list[str]:
    """One full-resolution frame per setting, for the lead to judge.

    Quality is deliberately NOT scored here: sparsity and local_range both trade
    fidelity for time and that judgement is not the bench's to make. Same frame
    index for every setting so the frames are comparable side by side.
    """
    from PIL import Image
    dest = OUT / "frames"
    dest.mkdir(parents=True, exist_ok=True)
    paths = []
    for r in results:
        if not r.get("ok") or not r.get("out"):
            continue
        dst = dest / f"{r['setting']}_r{r['repeat']}_f{frame}.png"
        rc = subprocess.call([_ffmpeg(), "-v", "error", "-y", "-i", r["out"],
                              "-vf", f"select=eq(n\\,{frame})", "-vframes", "1", str(dst)])
        if rc == 0 and dst.is_file():
            with Image.open(dst) as im:
                print(f"  {dst.name}  {im.size[0]}x{im.size[1]}")
            paths.append(str(dst))
        else:
            print(f"  {r['setting']} r{r['repeat']}: EXTRACT FAILED (rc={rc})")
    return paths


# ---------------------------------------------------------------- selftest

def _fixture(n_chunks: int = 43, low_mem_at: int | None = None,
             n_frames: int = 12, saved: str | None = None) -> str:
    """A byte-shape log identical to a real one, chunk lines GLUED onto the tqdm
    bar tail by a preceding \\r -- the trap, reproduced deliberately.
    low_mem_at=n drives memfree below the abort threshold at chunk n.
    n_frames is what the log DECLARES; `saved` names the file it claims to have written
    (selftest points this at a real clip so the frame check actually executes)."""
    out = ["1024x576 -> infer 1280x720 -> final 1920x1080", "infer 1280x720 361f",
           f"target frames (8n-3): {n_frames}"]
    t = 0.0
    for i in range(n_chunks):
        bar = ("\r  0%|          | 0/43 [00:00<?, ?it/s]" if i == 0
               else f"\r{i*100//43:3d}%|      | {i}/43 [00:{int(t)//60:02d}<00:00,  8.00s/it]")
        mem = 5 if low_mem_at is not None and i == low_mem_at else 64
        out.append(f"{bar}chunk {i} t={t:.1f}s "
                   f"loadavg=2.10/1.90/1.70 gpu=99% memfree={mem}% top=python:98.0")
        t += 8.0
    out.append("\r100%|██████████| 43/43 [05:49<00:00,  8.14s/it]")
    out.append("decode: 38.2s")
    out.append("inference: 349.0s")
    out.append(f"saved {saved or '/tmp/out.mp4'}")
    out.append("total: 480.8s")
    return "\n".join(out) + "\n"


def _tiny(n_frames: int, path: Path) -> str:
    """A real n_frames clip, so the frame check decodes an actual file.

    Mocking the count would test that the comparison operator works, not that the
    comparison is pointed at the right number -- which is precisely the bug that let
    96723d6d through. 16x16 costs nothing.
    """
    subprocess.call([_ffmpeg(), "-v", "error", "-y", "-f", "lavfi",
                    "-i", "color=c=blue:s=16x16:r=24", "-frames:v", str(n_frames),
                    "-pix_fmt", "yuv420p", str(path)], stdout=subprocess.DEVNULL)
    return str(path)


def selftest(verbose: bool = True) -> int:
    """Break the report 4 ways; each break MUST be caught. A check that cannot
    fail is decoration, so each case asserts both the void and the accept path."""
    tmp = OUT / "selftest"
    tmp.mkdir(parents=True, exist_ok=True)
    fails = []

    def case(label: str, text: str, want_void: str | None, n_expect: int | None = None):
        f = tmp / f"{label}.log"
        f.write_text(text)
        v = check_arm(f)
        if want_void is None:
            if not v["ok"]:
                fails.append(f"{label}: expected ACCEPT, got {v['voids']}")
            elif n_expect is not None and v["n_chunks"] != n_expect:
                fails.append(f"{label}: parsed {v['n_chunks']} chunks, want {n_expect}")
        else:
            if v["ok"]:
                fails.append(f"{label}: expected VOID({want_void}), got ACCEPT")
            elif not any(want_void in s for s in v["voids"]):
                fails.append(f"{label}: voided for {v['voids']}, want {want_void}")
        if verbose:
            tag = "ok " if not any(label in x for x in fails) else "FAIL"
            print(f"  [{tag}] {label:<22} {v['voids'] or 'ACCEPT'}")

    print("parser trap:")
    # Real clips on disk: the frame check decodes actual files rather than a mock,
    # because the bug it exists for was "pointed at the wrong number", not "wrong
    # comparison". A mocked count would pass while the real check stayed broken.
    ok12 = _tiny(12, tmp / "ok12.mp4")
    short11 = _tiny(11, tmp / "short11.mp4")
    long13 = _tiny(13, tmp / "long13.mp4")
    good = _fixture(n_frames=12, saved=ok12)
    # The exact failure the trap causes: line-anchored match finds nothing.
    anchored = len([l for l in good.splitlines() if re.match(r"chunk \d+", l)])
    ours = len(parse_chunks(good))
    print(f"  line-anchored parser finds {anchored} chunk lines (the bug)")
    print(f"  this parser finds {ours} chunk lines")
    if anchored != 0:
        fails.append(f"trap fixture not reproducing: anchored found {anchored}, want 0")
    if ours != 43:
        fails.append(f"parser found {ours} chunks, want 43")

    print("acceptance checks (good arm must ACCEPT):")
    case("accept_good", good, None, n_expect=43)

    print("negative tests (each break must VOID):")
    # 1. instrumentation gone -- plausible wall time still printed by the tail.
    case("break_no_chunks", re.sub(r"chunk \d+ t=[\d.]+s [^\n]*", "x", good), "NO_CHUNKS")
    # 2. a chunk lost mid-run -> survivorship-biased mean over the survivors.
    gapped = re.sub(r"chunk 21 t=[\d.]+s [^\n]*", "", good)
    case("break_seq_gap", gapped, "SEQ")
    # 3. cumulative clock went backwards -> every derived delta is junk.
    swapped = good.replace("chunk 20 t=160.0s", "chunk 20 t=11.0s")
    case("break_t_regress", swapped, "T_REGRESS")
    # 4. run never finished -> 'faster' would just mean 'crashed early'.
    case("break_no_complete", good.replace("total: 480.8s\n", ""), "NO_COMPLETE")
    # 5. memory ceiling breached mid-run but process still exited 0 -> the 5s
    #    monitor poll missed it; the per-chunk telemetry must still catch it.
    case("break_mem_breach", _fixture(low_mem_at=20, saved=ok12), "MEM_BREACH")
    # 6. chunks present but no memfree reading anywhere -> the ceiling was never
    #    observed and the arm ran with the safety net silently absent.
    case("break_mem_unmonitored",
         re.sub(r"memfree=\d+%", "memfree=?%", good), "MEM_UNMONITORED")
    # 7-10. the frame count. 96723d6d shape: exits 0, prints `saved`, prints `total:`,
    # writes a plausible file, and is short. Nothing else in this file can see it.
    case("break_frame_short", _fixture(n_frames=12, saved=short11), "FRAME_COUNT")
    case("break_frame_long", _fixture(n_frames=12, saved=long13), "FRAME_COUNT")
    case("break_frame_undeclared",
         re.sub(r"^target frames \(8n-3\): \d+\n", "", good, flags=re.M), "FRAME_UNDECLARED")
    case("break_frame_unprobed",
         _fixture(n_frames=12, saved=str(tmp / "does_not_exist.mp4")), "FRAME_UNPROBED")
    # The good arm must actually have read a matching declared/saved pair -- otherwise
    # every FRAME_COUNT case above could be voiding for the wrong reason entirely.
    v = check_arm(tmp / "accept_good.log")
    if (v["declared_frames"], v["saved_frames"]) != (12, 12):
        fails.append(f"frame check did not read a matching pair: "
                     f"{v['declared_frames']}/{v['saved_frames']}")

    # The gate must refuse when used memory is over the ceiling, and pass under it.
    print("gate threshold:")
    for used, want in ((39.0, True), (40.0, True), (40.1, False), (44.0, False)):
        ok = used <= MEM_USED_MAX_GIB
        if ok != want:
            fails.append(f"gate: used={used}GiB -> {ok}, want {want}")
        if verbose:
            print(f"  [{'ok ' if ok == want else 'FAIL'}] used={used:>5.1f}GiB "
                  f"-> {'accept' if ok else 'REFUSE'} (want {'accept' if want else 'REFUSE'})")

    print()
    if fails:
        for f in fails:
            print(f"FAIL: {f}")
        return 1

    # Orchestration: run() -> check -> summarize -> extract with Popen stubbed.
    # Separate process -- it mutates module state -- but part of selftest, because
    # an orchestration path that is never executed is the same silent-absence
    # failure as an unexecuted check.
    print("orchestration dry-run (no GPU):")
    orch = Path(__file__).resolve().parent / "_flashvsr_orch_test.py"
    rc = subprocess.call([sys.executable, str(orch)], stdout=subprocess.DEVNULL)
    if rc != 0:
        fails.append(f"orchestration dry-run failed (exit {rc})")
        print(f"  [FAIL] orchestration dry-run exit={rc}")
    else:
        print("  [ok ] run -> check -> summarize -> extract")

    if fails:
        for f in fails:
            print("FAIL:", f)
        return 1
    print("selftest: all checks fire, all fire for the right reason")
    return 0


# ---------------------------------------------------------------- cli

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("gate", help="pre-arm gate; exits 0 on PASS")
    c = sub.add_parser("check", help="acceptance check for one arm log")
    c.add_argument("log")
    sub.add_parser("plan", help="print the interleaved arm order")
    r = sub.add_parser("run", help="execute the series (needs the GPU)")
    r.add_argument("src", nargs="?", default=str(PINNED_INPUT),
                   help=f"input video (default: pinned {PINNED_INPUT.name})")
    r.add_argument("--seed", type=int, default=0)
    r.add_argument("--tag", default="series")
    sub.add_parser("selftest", help="negative-test every check")
    s = sub.add_parser("summarize", help="results table incl. voided/aborted arms")
    s.add_argument("results", help="results_<tag>.json from a run")
    e = sub.add_parser("extract", help="pull one full-res frame per setting (needs arm outputs)")
    e.add_argument("results", help="results_<tag>.json from a run")
    e.add_argument("--frame", type=int, default=180, help="frame index (default: midpoint)")
    a = ap.parse_args()

    if a.cmd == "gate":
        return 0 if gate() else 1
    if a.cmd == "check":
        v = check_arm(a.log)
        print(fmt_check(v))
        return 0 if v["ok"] else 2
    if a.cmd == "plan":
        plan()
        return 0
    if a.cmd == "run":
        run(Path(a.src), a.seed, a.tag)
        return 0
    if a.cmd == "selftest":
        return selftest()
    if a.cmd == "extract":
        extract(json.loads(Path(a.results).read_text()), a.frame)
        return 0
    if a.cmd == "summarize":
        summarize(json.loads(Path(a.results).read_text()))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())