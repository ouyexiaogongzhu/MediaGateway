"""Orchestration dry-run: run() -> check_arm -> summarize -> extract, with no GPU.

run() is the one significant path never executed. A bug in it surfaces only after
real GPU time is spent. This stubs Popen so the monitor loop, the gate, the
acceptance path, the results JSON and the frame extraction all run for real while
the actual inference is faked.
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import flashvsr_bench as fb

PROBE_OUT = Path(__file__).resolve().parents[1] / "data/flashvsr_bench"
REAL_MP4 = Path("/Users/vincent/code/MediaGateway/assets/flashvsr_96723d6d/output.mp4")
REAL_POPEN = subprocess.Popen   # captured BEFORE any stubbing; fb.subprocess is
                                # the same module object as the global one


def fake_log(n_chunks=43, memfree=64.0, saved="/tmp/out.mp4", declared=354):
    """Realistic log: tqdm-glued chunk lines, declaration, then completion markers.

    `saved` must name the file FakePopen actually writes and `declared` must be what
    that file really holds, or the frame check correctly VOIDs the arm and this test
    fails for a real reason rather than a fixture bug.
    """
    out = ["512x288 -> infer 1280x720 -> final 1920x1080", "infer 1280x720 361f",
           f"target frames (8n-3): {declared}"]
    t = 0.0
    for i in range(n_chunks):
        bar = ("\r  0%|          | 0/43 [00:00<?, ?it/s]" if i == 0
               else f"\r{i*100//43:3d}%|      | {i}/43 [00:{int(t)//60:02d}<00:00,  8.00s/it]")
        mf = int(memfree - i * 0.05)
        out.append(f"{bar}chunk {i} t={t:.1f}s loadavg=2.10/1.90/1.70 "
                   f"gpu=99% memfree={max(mf,1)}% top=python:98.0")
        t += 8.0
    out += ["\r100%|██████████| 43/43 [05:49<00:00,  8.14s/it]",
            "inference: 349.0s", f"saved {saved}", "total: 480.8s"]
    return "\n".join(out) + "\n"


STATE = {"memfree": 64.0}   # consulted by the stubbed _mem_free_pct

# What assets/flashvsr_96723d6d/output.mp4 ACTUALLY decodes to: 354, against a
# production log that declared 357. Header and stream agree, so the file is short
# rather than mislabelled -- this is the artifact whose 3 missing frames the bench
# could not see before the frame check existed. Used two ways: declared honestly so
# the happy path passes, and declared 357 so the regression fires.
REAL_FRAMES = 354


class FakePopen:
    """Mimics the Popen surface run_arm actually touches."""
    def __init__(self, cmd, cwd=None, env=None, stdout=None, stderr=None,
                 start_new_session=False):
        self.pid, self.returncode = 4242, None
        self._polls = 0
        # the arm that will hit the memory ceiling: drive it through _mem_free_pct,
        # which is what the monitor actually reads -- not through the log text.
        STATE["memfree"] = 3.0 if env.get("FLASHVSR_TOPK_RATIO") == "1.0" else 64.0
        # cmd[3] is the arm's output path; the log must name THAT file, or the frame
        # check correctly VOIDs the arm and this dry-run fails for a real reason.
        stdout.write(fake_log(memfree=3 if STATE["memfree"] < 8 else 64,
                              saved=cmd[3], declared=REAL_FRAMES))
        stdout.flush()
        # produce a real playable file so extract() is genuinely exercised
        shutil.copy(REAL_MP4, cmd[3])

    def poll(self):
        self._polls += 1
        if self._polls >= 3:          # completes after a couple of monitor polls
            self.returncode = 0
        return self.returncode

    def kill(self):
        self.returncode = -9


def main():
    fails = []
    fb.subprocess.Popen = FakePopen
    fb._mem_free_pct = lambda: STATE["memfree"]
    fb.gate_wait = lambda *a, **k: True
    fb.SETTINGS = [("baseline", {}), ("topk_1.5", {"FLASHVSR_TOPK_RATIO": "1.5"}),
                   ("topk_1.0", {"FLASHVSR_TOPK_RATIO": "1.0"})]
    fb.REPEATS = 2
    fb.MONITOR_POLL_S = 0.01
    fb.OUT = PROBE_OUT / "_orch"
    fb.RUNDIR = Path("/tmp")           # never entered: Popen is stubbed

    print("orchestration dry-run (Popen stubbed, no GPU):")
    results = fb.run(fb.PINNED_INPUT, 0, "orch")   # real path: run() validates it

    # every arm must have a row -- none silently absent
    if len(results) != 6:
        fails.append(f"expected 6 arms, got {len(results)}")

    # the abort arm must be VOID with a reason, not absent and not accepted
    aborts = [r for r in results if "MEM_ABORT" in (r.get("voids") or [])]
    if not aborts:
        fails.append("expected a MEM_ABORT arm; none present")

    # baseline spread must come out as a ratio with n
    base = [r["mean_chunk_s"] for r in results if r.get("ok") and r["setting"] == "baseline"]
    if len(base) != 2:
        fails.append(f"expected 2 accepted baselines, got {len(base)}")

    # results JSON must be on disk and re-loadable (that is what summarize reads)
    jf = fb.OUT / "results_orch.json"
    if not jf.is_file():
        fails.append("results JSON not written")
    else:
        reloaded = json.loads(jf.read_text())
        if len(reloaded) != len(results):
            fails.append(f"JSON has {len(reloaded)} arms, run had {len(results)}")

    # restore the real subprocess so extract genuinely runs ffmpeg
    fb.subprocess.Popen = REAL_POPEN
    print("\nframe extraction over the faked arms:")
    fb.extract(results)
    frames = list((fb.OUT / "frames").glob("*.png"))
    if not frames:
        fails.append("no frames extracted")

    print()
    if fails:
        for f in fails:
            print("FAIL:", f)
        return 1
    print("orchestration dry-run: run -> check -> summarize -> extract all execute")
    return 0


if __name__ == "__main__":
    sys.exit(main())