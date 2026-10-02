# FlashVSR knob series — diffusion knobs at upscale_cli.py:62-63

Status: **awaiting GPU handoff.** Tooling complete and self-tested; series not yet executed.

> **Completeness is stated in the header, never the footer.** If this report is
> truncated — arms not attempted, or VOID and therefore absent from the results —
> that fact appears at the top, where a reader who stops at the summary still sees
> it. A short table is indistinguishable from a complete one. `flashvsr_bench.py
> summarize` enforces this: it prints `!! INCOMPLETE SERIES: n/15 arms ran !!` with
> the never-attempted arms named, and it prints the row for every arm that ran,
> voided or not.

## Question

Diffusion is 94.9% of a FlashVSR run. Every lever outside it is exhausted (see
*Already dead*). What is left is the diffusion knob block itself:

```python
topk_ratio=SPARSITY * 768 * 1280 / (th * tw),
kv_ratio=KV_RATIO, local_range=LOCAL_RANGE, color_fix=True)
```

Production values: `SPARSITY=2.0`, `KV_RATIO=3.0`, `LOCAL_RANGE=11`.

## Already dead — do not re-measure

| Lever | Verdict |
|---|---|
| Resident server | `init_pipeline` is 4.4s of 480.8s (0.9%). A perfect server saves <1%. Spawn-per-job is fine. |
| 720 vs 1080 | ~3%. Diffusion is capped at 720-class for both. |
| `FLASHVSR_NO_MASK` | Already forced on upstream. |
| Queueing delay | All 11 jobs: `started_at - created_at` = 0.01–0.47s. |
| Crash rate | 2/11 → 0/11, verified by importing the real `snap()` across 630 source×resolution pairs. |

## The 43% question

The run is ~43% slower than reference under some conditions. Established causally
on a gated box with per-chunk telemetry: **r=+0.706 (n=42)**, chunks at
loadavg≥6.5 running 1.60× slower than quieter ones, onset/peak/recovery aligned
with the production signature.

**Progressive, not constant-factor.** Excluded a constant-factor code slowdown
(that would show 1.43× from chunk 1; observed 1.045× at chunk 3). Not excluded:
thermal, contention, memory pressure, mid-run competing workload, progressive
code regression.

This series exists to put a floor under that number. It is not an attempt to
explain the 43% — it is an attempt to make a knob effect distinguishable from it.

## Method

**15 arms, 5 settings × 3 interleaved repeats, repeat-major.** ~128 min.

| # | setting | env |
|---|---|---|
| 1 | baseline | *(production defaults)* |
| 2 | topk_1.5 | `FLASHVSR_TOPK_RATIO=1.5` |
| 3 | kv_2 | `FLASHVSR_KV_RATIO=2` |
| 4 | lr_7 | `FLASHVSR_LOCAL_RANGE=7` |
| 5 | topk_1.0 | `FLASHVSR_TOPK_RATIO=1.0` |

Repeat-major, not setting-major: every setting sees the same thermal and load
drift as its neighbours.

**The baseline runs 3× and its spread *is* the noise floor.** This is the one
number the campaign never established. A single arm is indistinguishable from the
chunks 17–21 excursion (1.60× over ~5 of 43 chunks), which is exactly why n=1
was never enough.

### Input — pinned, do not vary

`assets/video_6b275c2c/output.mp4` — verified 512×288, 362f, 24fps, 15.083s.
Derives infer 1280×720, final 1920×1080. Production 15s shape; matches the source
geometry of the byte-identical pair in the pre-reboot logs. Content does not
change DiT compute at fixed res/frames, but it changes encode time and every
quality comparison.

### Instrumentation

Already in source — `diffsynth/pipelines/flashvsr_tiny.py:390-393`, one line per chunk:

```
chunk 12 t=101.4s loadavg=3.21/2.90/2.40 gpu=99% memfree=64% top=python:98.0
```

`t=` cumulative wall, `loadavg` 1/5/15, `gpu`=IOAccelerator Device Utilization,
`memfree`=memory_pressure free percentage, `top`=highest-CPU process with its
percentage. Per-chunk, not per-arm: a 45% effect concentrated in part of a run is
invisible in an arm-level average.

`top=` is reported alongside every GPU-util reading so an excursion has an
attributable cause rather than being an unexplained number.

## Memory — the binding constraint

**Pre-arm gate: total memory used ≤ 40GiB.** (Supersedes the earlier "free ≥85%"
rule: 85% free of 48GiB is only ~7GiB used, stricter than asked for, and it would
refuse arms that are perfectly fine.)

```
used_gib = (hw.memsize - free_bytes) / 2**30        REFUSE if used_gib > 40
```

**In-arm hard abort: `memfree < 8%` (~44GiB) → kill our own process, report, do
not retry.** The gate is the tighter of the two, so the abort only fires if memory
climbs *during* a run. Monitored by 5s polling; the process group is killed so the
mux ffmpeg child cannot be orphaned.

Stability outranks measurement completeness: a VOID arm that aborted at the ceiling
is a legitimate result. Fifteen of those are cheaper than one crash that costs the
IDE twenty minutes of unsaved work.

### The trap in "free"

`free_bytes` is memory_pressure's free **percentage**, not its `Pages free` field
and not vm_stat's. On an idle 48GiB box `Pages free` reads **0.73GiB** while
memory_pressure reports **68% free** — the unallocated-page count ignores
inactive, speculative and purgeable pages. Using it as `free_bytes` would compute
47.3GiB used and refuse every arm on an idle machine. Recorded here because it is
silent: the gate would simply never pass.

Swap is never read. It is a lagging artifact of a pressure event that already
happened; gating on it only vetoes arms late.

**loadavg is recorded per chunk but is not a gate condition.** It was available as
a filter after the fact without contaminating acquisition.

## Acceptance

An arm is VOID, never silently reported, when instrumentation is missing or
untrustworthy:

| Check | Catches |
|---|---|
| `NO_CHUNKS` | Instrumentation stopped emitting, but the tail prints still yield a plausible wall time |
| `SEQ` | A chunk was lost; the mean would be computed over a survivorship-biased subset |
| `T_REGRESS` | Cumulative clock went backwards; every derived delta is junk |
| `NO_COMPLETE` | Run died early; "faster" would just mean "crashed" |
| `MEM_BREACH` | A completed arm (exit 0) whose per-chunk telemetry dipped below the ceiling — the 5s poll missed it, the telemetry did not |
| `MEM_UNMONITORED` | No memory reading anywhere in the arm: the ceiling was never observed, so it ran with the safety net silently absent |

This exists so *instrumentation broke* is never reported as *code got slower*.
Exit code is not evidence.

Two more enforced at runtime rather than in the log:

- **`TIMEOUT`** — 3600s wall per arm, matching the gateway's flashvsr
  `DEFAULT_TIMEOUT`. The bench calls `upscale_cli` directly and therefore bypasses
  the gateway's guard; without one here a hung arm loops forever with no escape.
- **`GATE_FAIL`** — recorded as a row, not skipped. A refused arm is a result.

`flashvsr_bench.py summarize` prints the table. **Every arm appears** — accepted,
voided or aborted — because a missing row is indistinguishable from an arm never
attempted. It also flags an incomplete series and names the arms that never ran,
and refuses to print a noise floor from fewer than two accepted baselines rather
than quoting a misleading number.

### Both paths are tested

`selftest` negative-tests every check above, then runs a full orchestration
dry-run (`scripts/_flashvsr_orch_test.py`) with `Popen` stubbed: real `run()` →
`check_arm` → `summarize` → `extract`, producing real 1920×1080 frames from faked
arms. The uncached inference is the only thing faked. An orchestration path that
is never executed is the same silent-absence failure as an unexecuted check.

## The parser trap

tqdm emits `\r` immediately before each new line, so every per-chunk line is glued
onto the tail of the progress bar:

```
\r 50%|████     | 21/43 [02:55<00:00, 7.95s/it]chunk 21 t=170.0s loadavg=... gpu=99% top=python:98.0
```

`grep -c '^chunk'` returns **0** on a log holding 43 chunk lines. The parser
scans the whole text with `finditer` and never anchors to a line start. A
line-anchored parser reads a healthy run as corrupt and would VOID every arm for
the wrong reason.

### Validation note

The 11 pre-reboot production logs VOID with `NO_CHUNKS` — correctly. They are
Sep 27; the instrumentation is from Oct 2. No parser could validate against them.
Validation is against a fixture reproducing the gluing byte-for-byte:
line-anchored finds 0, this parser finds 43. That tests the property that
actually matters, rather than testing stale data.

## Traps

**`kv_len = int(kv_ratio)`** — `flashvsr_tiny.py:559`. `kv_ratio=3.5` silently
computes `kv_len=3`: a no-op that reads as "no effect" and would cost three arms.
Only integers are meaningful. The series uses 2 and 3 only.

**`top=` capture runs to end-of-line, not `\S+`** — `ps -o comm` can contain
spaces ("Google Chrome Helper"). Truncating it would invent a phantom
non-python process and misattribute the excursion.

**Negative tests must fail.** The selftest caught its own bug on first pass — a
`T_REGRESS` fixture targeting `t=161.0` where the fixture emits `160.0`. The
fixture was fixed, not the check. That is the evidence the tests constrain
anything.

## Results

**15/15 ACCEPTED, zero voids.** All arms 43/43 chunks. Run 2026-10-02 16:06 to 17:42.

| setting | r | diffusion s | mean/chunk s | max/chunk s | load1 | mem_ch | mem_wall | MB | md5 |
|---|---|---|---|---|---|---|---|---|---|
| baseline | 1 | 267.6 | 6.37 | 10.70 | 2.16 | 71% | 43% | 19.76 | `9c4fb4dc` |
| topk_1.5 | 1 | 279.8 | 6.66 | 10.90 | 2.41 | 69% | 62% | 19.76 | `9c4fb4dc` |
| kv_2 | 1 | 247.6 | **5.89** | 11.70 | 2.52 | 74% | 47% | 19.92 | `b106da2b` |
| lr_7 | 1 | 276.2 | 6.57 | 12.90 | 1.95 | 71% | 62% | 19.76 | `9c4fb4dc` |
| topk_1.0 | 1 | 278.1 | 6.62 | 11.70 | 1.84 | 69% | 45% | 19.76 | `9c4fb4dc` |
| baseline | 2 | 277.9 | 6.61 | 11.10 | 2.58 | 69% | 46% | 19.76 | `9c4fb4dc` |
| topk_1.5 | 2 | 278.4 | 6.63 | 11.20 | 2.23 | 69% | 46% | 19.76 | `9c4fb4dc` |
| kv_2 | 2 | 251.5 | **5.99** | 11.20 | 1.90 | 73% | 61% | 19.92 | `b106da2b` |
| lr_7 | 2 | 278.6 | 6.63 | 11.50 | 2.08 | 72% | 52% | 19.76 | `9c4fb4dc` |
| topk_1.0 | 2 | 278.3 | 6.62 | 11.20 | 1.95 | 69% | 61% | 19.76 | `9c4fb4dc` |
| baseline | 3 | 278.6 | 6.63 | 11.30 | 2.52 | 69% | 60% | 19.76 | `9c4fb4dc` |
| topk_1.5 | 3 | 279.1 | 6.64 | 11.40 | 2.47 | 72% | 50% | 19.76 | `9c4fb4dc` |
| kv_2 | 3 | 248.8 | **5.92** | 11.50 | 1.99 | 74% | 52% | 19.92 | `b106da2b` |
| lr_7 | 3 | 278.7 | 6.63 | 10.90 | 1.90 | 69% | 61% | 19.76 | `9c4fb4dc` |
| topk_1.0 | 3 | 278.3 | 6.62 | 11.20 | 2.32 | 72% | 52% | 19.76 | `9c4fb4dc` |

`mem_ch` = free% from per-chunk telemetry, which starts at chunk 0 and so EXCLUDES
model load. `mem_wall` = free% sampled every 5s across the whole run, load included.
`mem_wall` is lower on all 15 arms. Do not merge them.

### Noise floor

```
noise floor: baseline spread 1.04x over n=3 accepted
  per-chunk mean between 6.37s (fastest) and 6.63s (slowest)
  any knob effect under 1.04x is inside this spread -- treat as noise
```

The first measured floor for this bench. Baseline sits at **6.537s/chunk** mean,
against 8.14s/it in the Sep-27 production logs. That gap is a different box state
*and* a different infer size (1280x720 vs 1280x768); inheriting the old number
would have imported exactly the confound this campaign spent its length
dismantling.

### Two of the three knobs are dead -- proven, not inferred

`topk_ratio` at 1.5 and 1.0, and `local_range` at 7, produce output that is
**byte-for-byte identical to production**: `md5 9c4fb4dc`, matching baseline across
all three repeats of each. Not close -- the same file.

Their timing differences (6.61-6.64s vs baseline 6.54s) are therefore noise by
construction, and the apparent 1-1.5% *slowdown* carries no meaning either.

**Mechanism: unresolved, and my first hypothesis was wrong.** An earlier draft
claimed `topk` was inert because the computed selection exceeded the token count.
Checking it on the real geometry: latent 1280x720 -> VAE /8 -> 160x90 ->
`patchify` /2 -> grid **h=80, w=45**; `window_size = 2*80*45//128 = 56`;
`square_num = 3136`; `topk = int(3136 * ratio) - 1` = **6271 / 4703 / 3135** for
ratios 2.0 / 1.5 / 1.0. Those are not trivially non-binding, and I did not verify
the per-chunk token count, so the claim does not stand.

For `local_range` the mask demonstrably *does* change and still changes nothing.
Verified on CPU against the real builder: the mask is built on `h//8 x w//8` =
**10 x 5** (the patchified grid is divided by 8 again), and

| local_range | visible mask entries |
|---|---|
| 11 | 2000 / 2500 |
| 7 | 1334 / 2500 |

The masks are **not** equal -- `torch.equal` is False. Yet the outputs are
byte-identical. So the mask is constructed differently and changes nothing, which
means it is not binding in this execution path (`is_full_block=False`,
`if_buffer=True` streaming). Settling it needs a trace of which attention route
runs, not more timing arms. **Both knobs are inert in practice; neither mechanism
is confirmed.**

### kv_ratio=2 is the only knob that moved

| | baseline | kv_2 | ratio |
|---|---|---|---|
| mean/chunk | 6.537s | 5.933s | **1.10x faster** |
| own spread | 1.04x (n=3) | 1.02x (n=3) | -- |
| diffusion | 274.7s | 249.3s | **-25.4s/run** |
| output MB | 19.76 | 19.92 | **+0.8%** |

1.10x sits outside the 1.04x floor, and kv_2's own repeats (1.02x) are tighter than
the baseline's. The effect is real and reproducible -- bit-identical output across
all three repeats, so it is deterministic, not luck.

### Quality: UNPROVEN, not damaged

Lead compared `baseline_r1` and `kv_2_r1` at native 1920x1080, then cropped an
identical region over the eyes/eyelashes/hair of both subjects -- the
highest-frequency area, where a quality difference would appear first. Viewed back
to back: **visually indistinguishable.** Same eye detail, eyelash rendering, hair
strands, skin texture and tone, same catchlights. The 0.8% size difference is not
visible where a difference would show first.

That is a qualified no, not a clearance. One clip, one frame, one region is not a
quality verdict -- the T4 `core_reuse=4` draft conclusion came from exactly one
frame and reversed at production resolution. Nor does it make the +0.8% encode
variance.

**The shippable claim:** kv_2's 1.10x speedup is real and reproducible (outside a
1.04x floor, bit-identical across 3 repeats, own spread tighter than baseline's),
and its quality is **unproven rather than damaged**. That is materially different
from "it is safe," and it is the one to ship.

### The size signal

**kv_2's output is 0.8% LARGER than baseline.** Per h3's T4 finding, larger is
*plausible evidence of worse*: smear costs bits. 0.8% is small and could be encode
variance, but it is the only inverse-quality signal available and it does not
reassure.

**This is not called a win.** Frames for the lead's eye, native 1920x1080,
frame 180 of 362:

- `data/flashvsr_bench/frames/baseline_r1_f180.png` -- reference
- `data/flashvsr_bench/frames/kv_2_r1_f180.png` -- the candidate
- r2 and r3 of both are identical to r1 within their setting

No gradient-based sharpness proxy is used anywhere in this tooling, because per
h3's lesson it ranks the damaged artifact as *sharper*.

---

Rebuild after 2026-10-02 reboot wiped `/tmp`. Findings above survived; scripts
were rebuilt at `scripts/flashvsr_bench.py`.