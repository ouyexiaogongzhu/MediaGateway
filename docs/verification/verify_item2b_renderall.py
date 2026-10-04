#!/usr/bin/env python3
"""Item 2, Go side, end to end: drive RenderAllProjectShots (pipeline B) against
the REAL gateway so CreateConcatJob -> POST /v1/concat actually executes.

Item 3 already proved pipeline A (batch). This proves pipeline B, which is the
one whose concat call was switched to the typed endpoint today.
"""
import json, subprocess, time

YC = "http://127.0.0.1:8090"
PROJECT = "d8878c2bbbc5d3e737ed78075554b018"     # 9:16


def sh(*a):
    return subprocess.run(a, capture_output=True, text=True).stdout


def swap():
    return sh("sysctl", "-n", "vm.swapusage").strip()


def api(method, path, body=None, timeout=7200):
    cmd = ["curl", "-s", "--max-time", str(timeout), "-b", "/tmp/yc.jar",
           "-X", method, YC + path]
    if body is not None:
        cmd += ["-H", "Content-Type: application/json", "-d", json.dumps(body)]
    return json.loads(sh(*cmd))


IMG = ("a lone figure under a neon umbrella on a rain-soaked alley at night, "
       "vertical composition, cinematic, no text")
VID = ("slow push in on a lone figure under a neon umbrella in a rain-soaked "
       "alley at night, neon reflections in puddles")

existing = api("GET", f"/api/projects/{PROJECT}/shots")
print("existing shots:", json.dumps(existing, ensure_ascii=False)[:200])

shots = []
for i in (1, 2):
    r = api("POST", f"/api/projects/{PROJECT}/shots", {
        "unitId": "", "title": f"鏡頭{i}", "description": "concat e2e",
        "position": i, "durationMs": 2000, "status": "draft",
        "revision": {"plotDescription": VID, "dialogue": "", "shotSize": "",
                     "cameraAngle": "eye level", "cameraMovement": "push in",
                     "durationMs": 2000, "imagePrompt": IMG,
                     "videoPrompt": VID, "negativePrompt": "",
                     "continuityNotes": "", "actionBeats": []}})
    if r.get("code") != 0:
        print(f"shot {i} create FAILED:", json.dumps(r, ensure_ascii=False)[:400])
        break
    s = r["data"]["shot"]
    shots.append(s["id"])
    print(f"shot {i}: {s['id']}")

print("\nshots:", shots)
if len(shots) < 2:
    raise SystemExit("need 2 shots")

print("\nswap before render-all:", swap())
t0 = time.time()
r = api("POST", f"/api/projects/{PROJECT}/shots/render-all",
        {"draft": False, "musicPrompt": "tense ambient synth pulse",
         "width": 0, "height": 0, "fromShotId": ""}, timeout=7200)
print(f"render-all returned after {time.time()-t0:.0f}s:")
print(json.dumps(r, ensure_ascii=False)[:1500])
print("\nswap after render-all:", swap())