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

# isolate env BEFORE importing server.* (core binds DB/ASSET_ROOT at import).
# setdefault, then re-derive _TMP: when pytest runs several env-isolating test
# files in one process, the first importer's values are already baked into the
# bound server modules — overwriting the env here would strand the fixture
# files below in a directory the app no longer reads (the 404/400 pair).
_TMP = tempfile.mkdtemp(prefix="mg_compat_test_")
os.environ.setdefault("MG_DB", os.path.join(_TMP, "gateway.db"))
os.environ.setdefault("MG_ASSETS", os.path.join(_TMP, "assets"))
os.environ.setdefault("H3CWEB_COMPAT_BASE_DIR", _TMP)
os.environ.setdefault("H3CWEB_COMPAT_OUT_DIR", os.path.join(_TMP, "shots"))
os.environ.setdefault("H3CWEB_COMPAT_REFS_DIR", os.path.join(_TMP, "refs"))
# dead LLM port: the scheduler's video mutex must never see (or kill) the real
# local qwen server on :8000 from inside a test run
os.environ.setdefault("QWEN_PORT", "8899")
_TMP = os.environ["H3CWEB_COMPAT_BASE_DIR"]
for d in ("assets", "shots", "refs"):
    os.makedirs(os.path.join(_TMP, d), exist_ok=True)
Path(_TMP, "shots", "legacy.mp4").write_bytes(b"LEGACY")
Path(_TMP, "in.png").write_bytes(b"PNG")

from fastapi.testclient import TestClient  # noqa: E402
from server import compat_h3cweb as compat  # noqa: E402
from server import core  # noqa: E402
from server.main import app  # noqa: E402

core.db()  # init SQLite before the scheduler thread starts (lazy init is racy)

# /jobs and /v1/videos gate every h3 prompt on Context-IR, so tests that are
# about routing/lifecycle still need a conformant prompt to get past the gate.
CTX_T2VA = ("integrated_multimodal_description: [Shot 1] Live-action, a cat on "
            "a roof.\n\noverall_soundscape: Distant traffic.\n\n"
            "non_diegetic_music: N/A")
CTX_I2VA = ("For the target video, at 0.00 seconds into the target video, "
            "<Picture 1> (from [Shot 1]) is fully referenced.\n\n"
            "integrated_multimodal_description: [Shot 1] Live-action, a cat on "
            "a roof as shown in <Picture 1>.\n\noverall_soundscape: Distant "
            "traffic.\n\nnon_diegetic_music: N/A")


class FakeMmh3:
    """Worker-shaped stand-in for mmh3: writes the output file, one tick.

    Registered into core._REGISTRY so /jobs and /v1/videos never spawn the
    real Metal CLI from inside a test run.
    """

    TYPE = "mmh3"
    MEM_GB = 20.0
    __name__ = "tests.test_compat.FakeMmh3"  # /info lists worker module names

    def run(self, params, job_dir, progress, cancel):
        out = Path(job_dir) / "output.mp4"
        out.write_bytes(b"FAKE")
        progress(1.0, "denoise")
        return {"output_path": str(out), "frames": params.get("frames", 48)}


core.registry()  # populate real workers first — a pre-seeded _REGISTRY is
                 # truthy and would skip auto-discovery for the whole suite
core._REGISTRY["mmh3"] = FakeMmh3()


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
        "prompt": CTX_I2VA, "refs": [{"kind": "image", "path": "in.png"}],
        "width": 864, "height": 480, "seconds": 2, "seed": 5,
        "output_path": "shots/out.mp4",
        "ssd_streaming": True, "core_reuse": 2, "token_reduction": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "queued"
    assert body["output_path"] == str(core.ASSET_ROOT / body["job_id"] / "output.mp4")
    assert body["requested_output_path"] == os.path.join(_TMP, "shots", "out.mp4")
    p = core.get_job(body["job_id"])["params"]
    assert p["prompt"] == CTX_I2VA and p["width"] == 864 and p["height"] == 480
    assert p["seconds"] == 2 and p["seed"] == 5 and p["steps"] == 4
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
    r = client.post("/jobs", json={"prompt": CTX_I2VA,
                                   "refs": [{"kind": "image", "path": "nope.png"}]})
    assert r.status_code == 400, r.text
    assert "nope.png" in r.json()["detail"]


def test_jobs_404(client):
    assert client.get("/jobs/video_nope").status_code == 404


def test_v1_videos_json(client):
    data_url = "data:image/png;base64," + base64.b64encode(b"PNGDATA").decode()
    before = set(os.listdir(compat.REFS_DIR))
    r = client.post("/v1/videos", json={
        "prompt": CTX_I2VA.replace("<Picture 1>", "[IMAGE_1] <Picture 1>"),
        "size": "512×288", "seconds": "2",
        "input_reference": [data_url]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == body["task_id"] and body["status"] == "queued"
    new_files = [f for f in os.listdir(compat.REFS_DIR)
                 if f.startswith("ref_") and f.endswith(".png") and f not in before]
    assert new_files, os.listdir(compat.REFS_DIR)
    assert Path(compat.REFS_DIR, new_files[0]).read_bytes() == b"PNGDATA"
    p = core.get_job(body["id"])["params"]
    assert p["prompt"] == CTX_I2VA  # [IMAGE_N] stripped
    assert p["width"] == 512 and p["height"] == 288  # × normalized, already /32
    assert p["seconds"] == 2.0
    assert p["reference_image_size"] == 1
    assert p["refs"][0]["path"] == os.path.join(compat.REFS_DIR, new_files[0])


def test_v1_videos_size_rounding(client):
    r = client.post("/v1/videos", json={"prompt": CTX_T2VA, "size": "520x300"})
    assert r.status_code == 200, r.text
    p = core.get_job(r.json()["id"])["params"]
    assert p["width"] == 512 and p["height"] == 288  # floored to /32


def test_v1_videos_accepts_free_text_prompt(client):
    """Context-IR 是警告不是拒絕（2026-10-04）。

    它曾經是硬 400，而那道 gate 掐死了影策的分鏡批次路徑 —— 影策從來不產生那
    三個欄位。複合 shot 路徑從不經過這裡卻產出正常（−75.8dB 底噪、r=0.999），
    所以非 Context-IR prompt 確實能用。這條測試釘住「不再 400」，否則早晚有人
    把它「修」回去。
    """
    r = client.post("/v1/videos", json={"prompt": "a cat on a roof at dusk"})
    assert r.status_code == 200, r.text


def test_v1_videos_validation(client):
    # CTX_T2VA so a 400 here can only come from the rule under test — a free-text
    # prompt is accepted now (see test above), so every 400 below is the size/ref
    # rule and nothing else.
    for payload, code in (
        ({"prompt": CTX_T2VA, "size": "1280×1792"}, 400),  # cap mmh3 VSA 1024*1792
        ({"prompt": CTX_T2VA, "size": "abc"}, 400),        # not WxH
        ({"prompt": CTX_T2VA, "size": "16x16"}, 400),      # too small
        ({"prompt": CTX_T2VA, "input_reference": ["http://x/y.png"]}, 400),  # not data URL
        ({"prompt": CTX_T2VA, "video_url": "http://x/v.mp4"}, 400),  # unsupported media
        ({"size": "512x288"}, 400),                   # prompt required
        ({"prompt": CTX_T2VA, "size": "1024×1792"}, 200),  # VSA 上限內（1080 級唯一路）
    ):
        r = client.post("/v1/videos", json=payload)
        assert r.status_code == code, (payload, r.status_code, r.text)


def test_v1_videos_multipart(client):
    r = client.post("/v1/videos", data={"prompt": CTX_I2VA, "size": "864x480"},
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
    r = client.post("/v1/videos", data={"prompt": CTX_I2VA},
                    files=[("input_images", (None, d1)), ("input_images", (None, d2))])
    assert r.status_code == 200, r.text
    p = core.get_job(r.json()["id"])["params"]
    assert len(p["refs"]) == 2, p["refs"]
    assert Path(p["refs"][0]["path"]).read_bytes() == b"IMG1"
    assert Path(p["refs"][1]["path"]).read_bytes() == b"IMG2"
    assert p["reference_image_size"] == 1
    # 单字段 JSON 数组字符串形式
    r2 = client.post("/v1/videos",
                     data={"prompt": CTX_I2VA, "input_images": json.dumps([d1, d2])})
    assert r2.status_code == 200, r2.text
    assert len(core.get_job(r2.json()["id"])["params"]["refs"]) == 2


def test_v1_videos_first_frame_and_mute(client):
    """first_frame_image dataURL/本地路径 → params.first_frame（不入 refs）；mute_audio。"""
    r = client.post("/v1/videos", json={
        "prompt": CTX_T2VA, "size": "864x480", "first_frame_image": _jpeg(b"FFDATA"),
        "mute_audio": True})
    assert r.status_code == 200, r.text
    p = core.get_job(r.json()["id"])["params"]
    assert os.path.isfile(p["first_frame"])
    assert Path(p["first_frame"]).read_bytes() == b"FFDATA"
    assert p["mute_audio"] is True
    assert p["refs"] == []
    # 本地绝对路径直接透传；默认 mute_audio=False
    r2 = client.post("/v1/videos", json={
        "prompt": CTX_T2VA, "first_frame_image": os.path.join(_TMP, "in.png")})
    assert r2.status_code == 200, r2.text
    p2 = core.get_job(r2.json()["id"])["params"]
    assert p2["first_frame"] == os.path.join(_TMP, "in.png")
    assert p2["mute_audio"] is False and p2["refs"] == []
    # 不可达 URL → 400（fail loudly）
    r3 = client.post("/v1/videos", json={
        "prompt": CTX_T2VA, "first_frame_image": "http://127.0.0.1:9/x.png"})
    assert r3.status_code == 400, r3.text


def test_v1_videos_last_frame_image(client):
    """last_frame_image（JSON + multipart）→ params.last_frame，FL2VA 尾帧链路。"""
    r = client.post("/v1/videos", json={
        "prompt": CTX_T2VA, "first_frame_image": os.path.join(_TMP, "in.png"),
        "last_frame_image": _jpeg(b"LASTDATA")})
    assert r.status_code == 200, r.text
    p = core.get_job(r.json()["id"])["params"]
    assert Path(p["last_frame"]).read_bytes() == b"LASTDATA"
    assert p["refs"] == []
    # multipart 分支同样解析
    r2 = client.post("/v1/videos", data={"prompt": CTX_T2VA, "last_frame_image": ""})
    assert r2.status_code == 200, r2.text
    assert core.get_job(r2.json()["id"])["params"]["last_frame"] is None


def test_v1_videos_refs_frames_mutual_exclusion(client):
    """input_images 与 first/last_frame_image 并发 → 400（h3 Ref2VA 会静默忽略帧，入口显式拒绝）。"""
    payload = {"prompt": CTX_I2VA, "input_images": [_jpeg(b"R")], "first_frame_image": os.path.join(_TMP, "in.png")}
    r = client.post("/v1/videos", json=payload)
    assert r.status_code == 400, r.text
    r2 = client.post("/v1/videos", data={"prompt": CTX_I2VA, "input_images": _jpeg(b"R"), "last_frame_image": ""})
    assert r2.status_code == 200, r2.text  # 空串 last_frame 不算帧，正常放行


def test_v1_videos_malicious_new_fields(client):
    """新字段信任边界：空白跳过、bool 宽容解析、空条目跳过、坏值 400 不 500。"""
    r = client.post("/v1/videos", json={
        "prompt": CTX_T2VA, "first_frame_image": "   ", "mute_audio": "TRUE",
        "input_images": ["", "  ", None]})
    assert r.status_code == 200, r.text
    wait_done(client, r.json()["id"])  # 必须收尾：video 是排他 job，遗留会堵住后续测试的 FIFO
    p = core.get_job(r.json()["id"])["params"]
    assert p["first_frame"] is None and p["refs"] == []
    assert p["mute_audio"] is True  # "TRUE"/"1"/"yes" 均视为真
    # multipart：mute_audio="1" 宽容解析；空串 first_frame_image 跳过
    r2 = client.post("/v1/videos", data={"prompt": CTX_T2VA, "mute_audio": "1",
                                         "first_frame_image": ""})
    assert r2.status_code == 200, r2.text
    wait_done(client, r2.json()["id"])
    p2 = core.get_job(r2.json()["id"])["params"]
    assert p2["mute_audio"] is True and p2["first_frame"] is None
    # 坏值：400 + 中文 detail，绝不 500
    for payload in ({"prompt": CTX_T2VA, "first_frame_image": 123},
                    {"prompt": CTX_T2VA, "input_images": "[not json"},
                    {"prompt": CTX_T2VA, "input_images": [_jpeg(b"x"), 7]}):
        r3 = client.post("/v1/videos", json=payload)
        assert r3.status_code == 400, (payload, r3.status_code, r3.text)


def test_sora_status_mapping_and_content(client):
    r = client.post("/v1/videos", json={"prompt": CTX_T2VA, "size": "864x480"})
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
    r = client.post("/v1/videos", json={"prompt": CTX_I2VA, "input_images": [big]})
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
    for k in ("engine", "version", "model"):
        assert k in info, k
    assert info["engine"] == "mmh3"
    assert info["model"]["engine"] == "mmh3"
    assert info["model"]["cli"].endswith("mmh3")
    assert info["engines"]["mmh3"]["mem_gb"] == 20.0


def test_aux_annotate(client):
    """SDXL daemon aux 预处理（white/depth/lineart/pose）冒烟；:8187 不在线则跳过。"""
    import struct
    import urllib.request
    import zlib
    try:
        urllib.request.urlopen(
            os.environ.get("SDXL_DAEMON_URL", "http://127.0.0.1:8187") + "/health", timeout=2)
    except Exception:
        print("SKIP test_aux_annotate (daemon :8187 offline)")
        return

    def png(w, h, rgb):  # stdlib minimal RGB PNG
        raw = b"".join(b"\x00" + bytes(rgb) * w for _ in range(h))
        chunk = lambda t, d: (struct.pack(">I", len(d)) + t + d
                              + struct.pack(">I", zlib.crc32(t + d)))
        return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))

    du = "data:image/png;base64," + base64.b64encode(png(128, 128, (128, 110, 96))).decode()
    for t in ("white", "depth", "lineart", "pose"):
        r = client.post("/v1/aux", json={"type": t, "image": du, "width": 128, "height": 128})
        assert r.status_code == 200, (t, r.status_code, r.text[:200])
        out = base64.b64decode(r.json()["data"][0]["b64_json"])
        assert struct.unpack(">II", out[16:24]) == (128, 128), t


def test_plist_does_not_re_enable_dbcache(_c=None):
    # 程式碼預設值在生產上是廢的——launchd 注入的 EnvironmentVariables 蓋掉它。
    # QWEN_IMAGE_DBCACHE=1 就是生產裡偷偷開快取的那個開關，只翻程式碼不會生效。
    import plistlib

    path = Path(os.path.expanduser("~/Library/LaunchAgents/com.aifilm.gateway.plist"))
    if not path.is_file():
        return  # 沒裝 launchd 的機器（CI/容器）
    env = plistlib.loads(path.read_bytes())["EnvironmentVariables"]
    assert env.get("QWEN_IMAGE_DBCACHE") != "1", "plist overrides code default → 512² ghosting"
    # TURBO 分支已刪，這個 key 自 image_upscale 收成單一路徑起就沒人讀
    assert "QWEN_IMAGE_TURBO" not in env, "dead setting, nothing reads it"


