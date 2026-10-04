#!/usr/bin/env python3
"""Item 3: storyboard batch must derive 9:16 from the project aspect ratio.

Sends the batch with NO width/height, the case the fix covers. Before the fix
the request went out with size="" and /v1/videos fell back to its 864x480
default (landscape) for a vertical project.
"""
import json, subprocess, time

YC = "http://127.0.0.1:8090"
GW = "http://127.0.0.1:8600"
DB = "/Users/vincent/code/MediaGateway/data/gateway.db"
PROJECT = "d8878c2bbbc5d3e737ed78075554b018"   # smoke-storyboard-test, 9:16


def sh(*a):
    return subprocess.run(a, capture_output=True, text=True).stdout


def swap():
    return sh("sysctl", "-n", "vm.swapusage").strip()


def api(method, path, body=None):
    cmd = ["curl", "-s", "-b", "/tmp/yc.jar", "-X", method, YC + path]
    if body is not None:
        cmd += ["-H", "Content-Type: application/json", "-d", json.dumps(body)]
    return json.loads(sh(*cmd))


PROMPT = (
    "integrated_multimodal_description: A lone figure stands under a neon umbrella "
    "on a rain-soaked alley at night, full body framed vertically. The camera "
    "performs a slow push in. No one speaks.\n\n"
    "overall_soundscape: Steady rain falling on pavement, distant traffic hum, "
    "water dripping from a gutter.\n\n"
    "non_diegetic_music: N/A"
)

row = {"shotNumber": 1, "title": "竖屏冒烟",
       "visualPrompt": PROMPT,
       "videoPrompt": PROMPT,
       "durationSeconds": 2, "dialogue": "", "shotSize": "",
       "sfxTags": [], "characterIds": [], "assetRefs": []}

print("swap before:", swap())
r = api("POST", "/api/storyboard-video-batch",
        {"projectId": PROJECT, "rows": [row],
         "firstFrameResourceIds": {}, "width": 0, "height": 0})
print("create response:", json.dumps(r, ensure_ascii=False)[:400])
task = r.get("data") or {}
tid = task.get("id") or task.get("taskId")

deadline, final = time.time() + 3000, None
# 影策 reports "succeeded", not "completed" — polling only for the latter spins
# to the deadline and then crashes on the final print (exit code still 0 when
# piped). Both vocabularies accepted; 2026-10-04 first run died exactly here.
TERMINAL = ("succeeded", "completed", "failed", "cancelled")
while time.time() < deadline:
    t = (api("GET", f"/api/tasks/{tid}").get("data") or {})
    if t.get("status") in TERMINAL:
        final = t
        break
    print(f"  ...{t.get('status')} {t.get('progress')}")
    time.sleep(10)

if final is None:
    raise SystemExit(f"task {tid} never reached a terminal state {TERMINAL}")

print("\nfinal:", json.dumps({k: final.get(k) for k in
      ("status", "progress", "error", "result")}, ensure_ascii=False)[:1200])

print("\n--- gateway jobs from this batch ---")
out = sh("sqlite3", "-separator", "|", DB,
         "SELECT id,type,status FROM jobs WHERE created_at > "
         "strftime('%s','now')-10800 ORDER BY created_at;")
print(out)
print("--- video job dimensions ---")
for line in out.strip().splitlines():
    jid, jtype, jstat = line.split("|")
    if jtype != "video":
        continue
    j = json.loads(sh("curl", "-s", f"{GW}/v1/jobs/{jid}"))
    p = j.get("params") or {}
    w, h = p.get("width"), p.get("height")
    print(f"  {jid} {jstat}: width={w} height={h} -> "
          f"{'PORTRAIT ✓' if (h or 0) > (w or 0) else 'LANDSCAPE ✗'}")
print("\nswap after:", swap())