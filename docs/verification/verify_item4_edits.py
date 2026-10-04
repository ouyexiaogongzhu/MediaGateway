#!/usr/bin/env python3
"""Item 4b: /v1/images/edits with n > 1 against the real qwen_image backend."""
import base64, hashlib, json, subprocess, time, urllib.request, uuid

GW = "http://127.0.0.1:8600"
REF = "/Users/vincent/code/MediaGateway/assets/image_00b61dcd/image.png"
SIZE = "512x288"          # NOT 1024x1024 (image-backend deadlock)
MODEL = "qwen-image-2.1"


def swap():
    return subprocess.run(["sysctl", "-n", "vm.swapusage"],
                          capture_output=True, text=True).stdout.strip()


def multipart(fields, files):
    b = "----" + uuid.uuid4().hex
    out = b""
    for k, v in fields.items():
        out += f"--{b}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
    for k, val in files.items():
        fn, data = val[0], val[1]
        out += (f"--{b}\r\nContent-Disposition: form-data; name=\"{k}\"; "
                f"filename=\"{fn}\"\r\nContent-Type: image/png\r\n\r\n").encode() + data + b"\r\n"
    return out + f"--{b}--\r\n".encode(), f"multipart/form-data; boundary={b}"


img = open(REF, "rb").read()
body, ctype = multipart(
    {"model": MODEL, "prompt": "the same street at heavy nightfall, neon signs reflected in rain",
     "size": SIZE, "n": "2"},
    {"image": ("ref.png", img)})

print("swap before:", swap())
req = urllib.request.Request(GW + "/v1/images/edits", data=body,
                             headers={"Content-Type": ctype})
t = time.time()
with urllib.request.urlopen(req, timeout=3600) as r:
    d = json.loads(r.read())
dt = time.time() - t

imgs = d.get("data", [])
h = [hashlib.sha256(base64.b64decode(i["b64_json"])).hexdigest()[:12] for i in imgs]
print(f"\nedits n=2: HTTP 200 OK, {dt:.1f}s, returned {len(imgs)} image(s)")
print(f"  sha256[:12]: {h}")
print(f"  distinct: {len(set(h))}/{len(imgs)}")
for x in h:
    raw = base64.b64decode(next(i["b64_json"] for i in imgs
                                if hashlib.sha256(base64.b64decode(
                                    i["b64_json"])).hexdigest().startswith(x)))
    open(f"/tmp/cv/edit_{x}.png", "wb").write(raw)
print("\nswap after:", swap())
ok = len(imgs) == 2 and len(set(h)) == 2
print("VERDICT: " + ("PASS" if ok else "FAIL"))