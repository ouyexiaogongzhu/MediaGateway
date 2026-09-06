"""h3cweb compat layer tests — TestClient + fake engine. No GPU, no real engine.

Run: .venv/bin/python tests/test_compat.py
"""
import base64
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# isolate env BEFORE importing server.* (core binds DB/ASSET_ROOT at import)
_TMP = tempfile.mkdtemp(prefix="mg_compat_test_")
os.environ["MG_DB"] = os.path.join(_TMP, "gateway.db")
os.environ["MG_ASSETS"] = os.path.join(_TMP, "assets")
os.environ["H3CWEB_COMPAT_BASE_DIR"] = _TMP
os.environ["H3CWEB_COMPAT_OUT_DIR"] = os.path.join(_TMP, "shots")
os.environ["H3CWEB_COMPAT_REFS_DIR"] = os.path.join(_TMP, "refs")
# dead LLM port: the scheduler's video mutex must never see (or kill) the real
# local qwen server on :8000 from inside a test run
os.environ["QWEN_PORT"] = "8899"
for d in ("assets", "shots", "refs"):
    os.makedirs(os.path.join(_TMP, d), exist_ok=True)
Path(_TMP, "shots", "legacy.mp4").write_bytes(b"LEGACY")
Path(_TMP, "in.png").write_bytes(b"PNG")

from fastapi.testclient import TestClient  # noqa: E402
from server import compat_h3cweb as compat  # noqa: E402
from server import core  # noqa: E402
from server.main import app  # noqa: E402
from server.workers import video  # noqa: E402

core.db()  # init SQLite before the scheduler thread starts (lazy init is racy)


class FakeEngine:
    def close(self):
        pass

    def generate(self, prompt, *, output_path, refs=None, on_progress=None, **ov):
        Path(output_path).write_bytes(b"FAKE")
        if on_progress:
            on_progress("denoise", 1, 1)
        return {"width": ov.get("width"), "height": ov.get("height"),
                "frames": ov.get("frames", 48), "fps": 24, "seed": 7}


def wait_done(client, job_id, timeout=5.0):
    end = time.time() + timeout
    r = {}
    while time.time() < end:
        r = client.get(f"/jobs/{job_id}").json()
        if r.get("status") in ("completed", "failed"):
            return r
        time.sleep(0.1)
    raise AssertionError(f"job {job_id} did not finish: {r}")


def test_post_jobs_params_and_lifecycle(client):
    r = client.post("/jobs", json={
        "prompt": "cat", "refs": [{"kind": "image", "path": "in.png"}],
        "width": 864, "height": 480, "seconds": 2, "seed": 5,
        "output_path": "shots/out.mp4",
        "ssd_streaming": True, "core_reuse": 2, "token_reduction": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "queued"
    assert body["output_path"] == str(core.ASSET_ROOT / body["job_id"] / "output.mp4")
    assert body["requested_output_path"] == os.path.join(_TMP, "shots", "out.mp4")
    p = core.get_job(body["job_id"])["params"]
    assert p["prompt"] == "cat" and p["width"] == 864 and p["height"] == 480
    assert p["seconds"] == 2 and p["seed"] == 5 and p["steps"] == 6
    assert p["refs"][0]["path"] == os.path.join(_TMP, "in.png")  # resolved vs BASE_DIR
    assert p["requested_output_path"].endswith("out.mp4")
    done = wait_done(client, body["job_id"])
    assert done["status"] == "completed" and done["progress"] == 1.0
    assert done["output_path"] == body["output_path"]
    for k in ("job_id", "status", "phase", "progress", "output_path", "error",
              "created_at", "started_at", "finished_at"):
        assert k in done, k
    assert os.path.isfile(done["output_path"])


def test_post_jobs_missing_ref_400(client):
    r = client.post("/jobs", json={"prompt": "x",
                                   "refs": [{"kind": "image", "path": "nope.png"}]})
    assert r.status_code == 400, r.text
    assert "nope.png" in r.json()["detail"]


def test_jobs_404(client):
    assert client.get("/jobs/video_nope").status_code == 404


def test_v1_videos_json(client):
    data_url = "data:image/png;base64," + base64.b64encode(b"PNGDATA").decode()
    before = set(os.listdir(compat.REFS_DIR))
    r = client.post("/v1/videos", json={
        "prompt": "a [IMAGE_1] scene", "size": "512×288", "seconds": "2",
        "input_reference": [data_url]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == body["task_id"] and body["status"] == "queued"
    new_files = [f for f in os.listdir(compat.REFS_DIR)
                 if f.startswith("ref_") and f.endswith(".png") and f not in before]
    assert new_files, os.listdir(compat.REFS_DIR)
    assert Path(compat.REFS_DIR, new_files[0]).read_bytes() == b"PNGDATA"
    p = core.get_job(body["id"])["params"]
    assert p["prompt"] == "a scene"  # [IMAGE_N] stripped
    assert p["width"] == 512 and p["height"] == 288  # × normalized, already /32
    assert p["seconds"] == 2.0
    assert p["reference_image_size"] == 1
    assert p["refs"][0]["path"] == os.path.join(compat.REFS_DIR, new_files[0])


def test_v1_videos_size_rounding(client):
    r = client.post("/v1/videos", json={"prompt": "x", "size": "520x300"})
    assert r.status_code == 200, r.text
    p = core.get_job(r.json()["id"])["params"]
    assert p["width"] == 512 and p["height"] == 288  # floored to /32


def test_v1_videos_validation(client):
    for payload, code in (
        ({"prompt": "x", "size": "1280×960"}, 400),   # cap 768*1344
        ({"prompt": "x", "size": "abc"}, 400),        # not WxH
        ({"prompt": "x", "size": "16x16"}, 400),      # too small
        ({"prompt": "x", "input_reference": ["http://x/y.png"]}, 400),  # not data URL
        ({"prompt": "x", "video_url": "http://x/v.mp4"}, 400),  # unsupported media
        ({"size": "512x288"}, 400),                   # prompt required
    ):
        r = client.post("/v1/videos", json=payload)
        assert r.status_code == code, (payload, r.status_code, r.text)


def test_v1_videos_multipart(client):
    r = client.post("/v1/videos", data={"prompt": "mp", "size": "864x480"},
                    files={"input_reference": ("a.png", b"BIN", "image/png")})
    assert r.status_code == 200, r.text
    p = core.get_job(r.json()["id"])["params"]
    assert p["width"] == 864 and p["refs"][0]["path"].endswith(".png")
    assert Path(p["refs"][0]["path"]).read_bytes() == b"BIN"
    assert p["reference_image_size"] == 1


def _jpeg(b: bytes) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(b).decode()


def test_v1_videos_multipart_input_images(client):
    """影策协议：input_images 多值（重复字段名 / 单字段 JSON 数组字符串）全进 refs。"""
    d1, d2 = _jpeg(b"IMG1"), _jpeg(b"IMG2")
    r = client.post("/v1/videos", data={"prompt": "multi"},
                    files=[("input_images", (None, d1)), ("input_images", (None, d2))])
    assert r.status_code == 200, r.text
    p = core.get_job(r.json()["id"])["params"]
    assert len(p["refs"]) == 2, p["refs"]
    assert Path(p["refs"][0]["path"]).read_bytes() == b"IMG1"
    assert Path(p["refs"][1]["path"]).read_bytes() == b"IMG2"
    assert p["reference_image_size"] == 1
    # 单字段 JSON 数组字符串形式
    r2 = client.post("/v1/videos",
                     data={"prompt": "arr", "input_images": json.dumps([d1, d2])})
    assert r2.status_code == 200, r2.text
    assert len(core.get_job(r2.json()["id"])["params"]["refs"]) == 2


def test_v1_videos_first_frame_and_mute(client):
    """first_frame_image dataURL/本地路径 → params.first_frame（不入 refs）；mute_audio。"""
    r = client.post("/v1/videos", json={
        "prompt": "ff", "size": "864x480", "first_frame_image": _jpeg(b"FFDATA"),
        "mute_audio": True, "input_images": [_jpeg(b"REFDATA")]})
    assert r.status_code == 200, r.text
    p = core.get_job(r.json()["id"])["params"]
    assert os.path.isfile(p["first_frame"])
    assert Path(p["first_frame"]).read_bytes() == b"FFDATA"
    assert p["mute_audio"] is True
    assert len(p["refs"]) == 1
    assert Path(p["refs"][0]["path"]).read_bytes() == b"REFDATA"
    # 本地绝对路径直接透传；默认 mute_audio=False
    r2 = client.post("/v1/videos", json={
        "prompt": "ff2", "first_frame_image": os.path.join(_TMP, "in.png")})
    assert r2.status_code == 200, r2.text
    p2 = core.get_job(r2.json()["id"])["params"]
    assert p2["first_frame"] == os.path.join(_TMP, "in.png")
    assert p2["mute_audio"] is False and p2["refs"] == []
    # 不可达 URL → 400（fail loudly）
    r3 = client.post("/v1/videos", json={
        "prompt": "ff3", "first_frame_image": "http://127.0.0.1:9/x.png"})
    assert r3.status_code == 400, r3.text


def test_v1_videos_malicious_new_fields(client):
    """新字段信任边界：空白跳过、bool 宽容解析、空条目跳过、坏值 400 不 500。"""
    r = client.post("/v1/videos", json={
        "prompt": "edge", "first_frame_image": "   ", "mute_audio": "TRUE",
        "input_images": ["", "  ", None]})
    assert r.status_code == 200, r.text
    p = core.get_job(r.json()["id"])["params"]
    assert p["first_frame"] is None and p["refs"] == []
    assert p["mute_audio"] is True  # "TRUE"/"1"/"yes" 均视为真
    # multipart：mute_audio="1" 宽容解析；空串 first_frame_image 跳过
    r2 = client.post("/v1/videos", data={"prompt": "mb", "mute_audio": "1",
                                         "first_frame_image": ""})
    assert r2.status_code == 200, r2.text
    p2 = core.get_job(r2.json()["id"])["params"]
    assert p2["mute_audio"] is True and p2["first_frame"] is None
    # 坏值：400 + 中文 detail，绝不 500
    for payload in ({"prompt": "b1", "first_frame_image": 123},
                    {"prompt": "b2", "input_images": "[not json"},
                    {"prompt": "b3", "input_images": [_jpeg(b"x"), 7]}):
        r3 = client.post("/v1/videos", json=payload)
        assert r3.status_code == 400, (payload, r3.status_code, r3.text)


def test_sora_status_mapping_and_content(client):
    r = client.post("/v1/videos", json={"prompt": "smoke", "size": "864x480"})
    jid = r.json()["id"]
    assert compat._SORA_STATUS == {"queued": "queued", "running": "in_progress",
                                   "completed": "completed", "failed": "failed",
                                   "cancelled": "failed"}
    assert compat._STATUS["cancelled"] == "failed"
    done = wait_done(client, jid)
    assert done["status"] == "completed"
    s = client.get(f"/v1/videos/{jid}").json()
    assert s["status"] == "completed" and s["progress"] == 1.0 and s["error"] is None
    # 影策/newapi 协议依赖 completed 响应携带结果地址
    assert s["url"].endswith(f"/v1/videos/{jid}/content"), s
    c = client.get(f"/v1/videos/{jid}/content")
    assert c.status_code == 200 and c.content == b"FAKE"
    # gateway "cancelled" surfaces as failed for legacy callers
    core.create_job("video", {"prompt": "c"})
    j = core.get_job(jid)
    core._update(jid, status="cancelled", error="bye")
    s = client.get(f"/v1/videos/{jid}").json()
    assert s["status"] == "failed" and s["error"] == "bye"
    assert client.get("/v1/videos/video_nope").status_code == 404
    # 影策把分镜图编码成 >1MB 的 input_images 文本字段：
    # starlette 非文件 part 默认 1MB 上限曾 400 "Part exceeded maximum size"
    big = "data:image/png;base64," + base64.b64encode(
        b"\x89PNG\r\n\x1a\n" + os.urandom(1_200_000)).decode()
    r = client.post("/v1/videos", json={"prompt": "big ref", "input_images": [big]})
    assert r.status_code == 200, f"{r.status_code} {r.text[:120]}"
    # 影策 newapi 上游取消走 DELETE；重复取消/已结束也 200 + 当前状态
    r = client.post(f"/v1/videos/{jid}/cancel")
    assert r.status_code == 200 and r.json()["id"] == jid, r.text
    r = client.delete(f"/v1/videos/{jid}")
    assert r.status_code == 200 and "status" in r.json(), r.text
    assert client.get("/v1/videos/video_nope/content").status_code == 404
    assert j  # silence unused


def test_content_not_ready(client):
    jid = core.create_job("video", {"prompt": "pending"})["id"]
    r = client.get(f"/v1/videos/{jid}/content")
    assert r.status_code == 409, r.text  # queued/running => not ready


def test_files_traversal_and_serving(client):
    ok = client.get("/files/legacy.mp4")
    assert ok.status_code == 200 and ok.content == b"LEGACY"
    for name in ("a%2Fb.mp4", "a..b.mp4", "..%2Fx.mp4", "a%5Cb.mp4", "x.png",
                 "nope.mp4"):
        r = client.get(f"/files/{name}")
        assert r.status_code in (400, 404), (name, r.status_code)
    # %2F is rejected by starlette routing itself (404) — never reaches the handler
    assert client.get("/files/a%2Fb.mp4").status_code == 404
    assert client.get("/files/a..b.mp4").status_code == 400   # contains ".."
    assert client.get("/files/a%5Cb.mp4").status_code == 400  # decoded "a\\b"
    assert client.get("/files/x.png").status_code == 400      # mp4 only


def test_info_compat_shape(client):
    info = client.get("/info").json()
    for k in ("engine", "version", "device", "model", "cache"):
        assert k in info, k
    assert info["engine"] == "h3.c"
    assert info["device"] is None  # engine not resident between jobs (documented diff)
    assert info["model"]["dir"].endswith("MiniMax-H3")


if __name__ == "__main__":
    video._get_engine = lambda: FakeEngine()  # inject fake; scheduler runs it for real
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    with TestClient(app) as c:
        for t in tests:
            try:
                t(c)
                print(f"PASS {t.__name__}")
            except AssertionError as e:
                failures += 1
                print(f"FAIL {t.__name__}: {e}")
    print(f"\n{len(tests) - failures}/{len(tests)} tests passed")
    sys.exit(1 if failures else 0)
