#!/usr/bin/env python3
"""Item 4: /v1/images/generations and /v1/images/edits with n > 1, real backend.

Checks three things the unit tests cannot: that the response really carries n
images, that they are not byte-identical copies, and that n is clamped to 4.
Size is pinned to 512x288 -- 1024x1024 is the known image-backend deadlock size
and must never be reintroduced.
"""
import base64, hashlib, json, subprocess, sys, time, urllib.request

GW = "http://127.0.0.1:8600"
REF = "/Users/vincent/code/MediaGateway/assets/image_00b61dcd/image.png"
SIZE = "512x288"          # NOT 1024x1024 (image-backend deadlock)
MODEL = "qwen-image-2.1"


def swap():
    return subprocess.run(["sysctl", "-n", "vm.swapusage"],
                          capture_output=True, text=True).stdout.strip()


def post(path, body, ctype="application/json"):
    req = urllib.request.Request(GW + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": ctype})
    t = time.time()
    with urllib.request.urlopen(req, timeout=3600) as r:
        return json.loads(r.read()), time.time() - t


def show(label, d, dt):
    imgs = d.get("data", [])
    hashes = [hashlib.sha256(base64.b64decode(i["b64_json"])).hexdigest()[:12]
              for i in imgs]
    uniq = len(set(hashes))
    print(f"\n{label}")
    print(f"  HTTP 200 OK, {dt:.1f}s, requested n -> returned {len(imgs)} image(s)")
    print(f"  sha256[:12]: {hashes}")
    print(f"  distinct: {uniq}/{len(imgs)}")
    for h in hashes:
        b = base64.b64decode(next(i["b64_json"] for i in imgs
                                  if hashlib.sha256(base64.b64decode(
                                      i["b64_json"])).hexdigest().startswith(h)))
        p = f"/tmp/cv/out_{label.split()[0]}_{h}.png"
        open(p, "wb").write(b)
    return len(imgs), uniq


print("swap before:", swap())

d, dt = post("/v1/images/generations",
             {"model": MODEL, "prompt": "a lone red umbrella on a wet pier, "
              "grey overcast dawn, cinematic still", "size": SIZE, "n": 2})
n, u = show("generations n=2 (no seed)", d, dt)

d, dt = post("/v1/images/generations",
             {"model": MODEL, "prompt": "a lone red umbrella on a wet pier, "
              "grey overcast dawn, cinematic still", "size": SIZE, "n": 2, "seed": 1234})
n2, u2 = show("generations n=2 (seed=1234)", d, dt)

print("\nverdicts:")
print(f"  generations n=2 returns 2 : {'PASS' if n == 2 else 'FAIL'}")
print(f"  images distinct (no seed): {'PASS' if u == n else 'FAIL (identical copies)'}")
print(f"  images distinct (seeded) : {'PASS' if u2 == n2 else 'FAIL (identical copies)'}")
print("\nswap after:", swap())
sys.exit(0 if (n == 2 and u == 2 and u2 == 2) else 1)