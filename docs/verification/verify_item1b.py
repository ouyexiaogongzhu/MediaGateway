#!/usr/bin/env python3
"""Item 1 (take 2): the discriminating version.

The first attempt submitted the concat 0.57s after the video job went
`running` -- while the 35GB engine was still loading, so video._engine was
still None and the OLD buggy _resident_gb would also have charged 0.
That test could not tell old code from new code.

This version waits for gateway RSS to prove the engine is genuinely resident
(that is exactly the state the old code double-charged), then submits the concat.
"""
import json, subprocess, sys, time, urllib.request

GW = "http://127.0.0.1:8600"
DB = "/Users/vincent/code/MediaGateway/data/gateway.db"
RSS_GATE_GB = 20.0   # engine is 35GB; 20GB proves it is loaded and resident
T0 = time.time()
log = lambda m: print(f"[{time.time()-T0:7.2f}s] {m}", flush=True)


def post(p, b):
    r = urllib.request.Request(GW + p, data=json.dumps(b).encode(),
                               headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=30) as x:
        return json.loads(x.read())


def get(p):
    with urllib.request.urlopen(GW + p, timeout=30) as x:
        return json.loads(x.read())


def gateway_pid():
    return subprocess.run(["lsof", "-tiTCP:8600", "-sTCP:LISTEN"],
                          capture_output=True, text=True).stdout.strip()


def rss_gb():
    out = subprocess.run(["ps", "-o", "rss=", "-p", gateway_pid()],
                         capture_output=True, text=True).stdout.strip()
    return int(out) / 1024 / 1024 if out else 0.0


def q():
    return subprocess.run(["sqlite3", DB,
        "SELECT id||' '||type||' '||status FROM jobs "
        "WHERE status IN ('queued','running');"],
        capture_output=True, text=True).stdout.strip()


GEN_PHASES = ("precompute AdaLN", "load transformer core", "denoise",
              "video VAE", "sampling", "decode")


def engine_loaded(st):
    """Gate on a generation phase.

    /info.model.loaded is NOT usable: it only exists in the *fallback* branch
    (engine NOT loaded). Once the engine loads, model becomes eng.model() and
    the key vanishes -- measured `loaded=False` when idle, key absent while
    generating. RSS is not usable either: peak RSS for a full 35GB-engine
    video job measured 5.9GB (macOS does not count Metal / mmap'd unified
    memory weights in RSS).

    A non-null, non-'FFmpeg' phase can only be emitted from inside
    engine.generate(), which video.py calls strictly after _get_engine() set
    the module-global `_engine` -- i.e. exactly the state _resident_gb() tests.
    """
    ph = st.get("phase")
    return bool(ph) and ph != "FFmpeg" and any(p in ph for p in GEN_PHASES)


info = get("/info")
budget = info["budget_gb"]
log(f"BUDGET_GB={budget} (from /info). Engine gate = generation phase")
if q():
    sys.exit(f"ABORT: queue not empty: {q()}")

rss_before = rss_gb()
log(f"gateway RSS before video = {rss_before:.2f} GB")

v = post("/v1/jobs", {"type": "video", "params": {
    "prompt": "A slow push-in on an empty rain-slicked street at night, neon "
              "reflections in puddles, no dialogue, N/A",
    "width": 512, "height": 288, "seconds": 2, "profile": "draft"}})
vid = v["id"]
log(f"video submitted {vid}")

# wait until the engine is ACTUALLY loaded (the discriminating state)
peak = 0.0
t_resident = None
while time.time() - T0 < 240:
    st = get(f"/v1/jobs/{vid}")
    if st["status"] in ("failed", "completed", "cancelled"):
        sys.exit(f"video ended before engine became resident: {st}")
    peak = max(peak, rss_gb())
    if engine_loaded(st):
        t_resident = time.time() - T0
        log(f"ENGINE LOADED: video._engine is not None "
            f"(status={st['status']}, phase={st.get('phase')!r}, progress={st.get('progress')})")
        raw = get("/info")
        log(f"/info while loaded: model={json.dumps(raw['model'])} "
            f"device={'set' if raw['device'] else 'None'} "
            f"cache={'set' if raw['cache'] else 'None'}  "
            f"-> model.loaded key present? {'loaded' in raw['model']}")
        break
    time.sleep(0.25)

if t_resident is None:
    sys.exit(f"engine never became resident (peak RSS {peak:.2f} GB)")

# This is the state the bug was about: video._engine is NOT None, job running.
#   new code: running_gb = 35 + resident(0, video in running) = 35; +1 = 36 <= 36 -> admit
#   old code: running_gb = 35 + resident(35) = 70; +1 = 71 > 36 -> STALL
c = post("/v1/jobs", {"type": "concat", "params": {
    "shots": ["/Users/vincent/code/MediaGateway/assets/video_fc045178/output.mp4",
              "/Users/vincent/code/MediaGateway/assets/video_cef5e97e/output.mp4"]}})
cid = c["id"]
log(f"concat {cid} submitted while engine loaded (RSS {rss_gb():.2f} GB)")

t_admit = None
while time.time() - T0 < 240:
    cs = get(f"/v1/jobs/{cid}")
    vs = get(f"/v1/jobs/{vid}")
    if cs["status"] == "running" and t_admit is None:
        t_admit = time.time() - T0
        log(f"*** concat ADMITTED at {t_admit:.2f}s; video={vs['status']} ***")
    if cs["status"] in ("completed", "failed", "cancelled"):
        log(f"concat -> {cs['status']}  out={cs.get('output_path')} err={cs.get('error')}")
        break
    time.sleep(0.25)

log(f"video -> {get(f'/v1/jobs/{vid}')['status']}, peak RSS {peak:.2f} GB")
log(f"DB active now: {q()!r}")
log("RESULT: " + ("PASS (admitted)" if t_admit else "FAIL (stalled)"))