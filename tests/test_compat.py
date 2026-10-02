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
        "mute_audio": True})
    assert r.status_code == 200, r.text
    p = core.get_job(r.json()["id"])["params"]
    assert os.path.isfile(p["first_frame"])
    assert Path(p["first_frame"]).read_bytes() == b"FFDATA"
    assert p["mute_audio"] is True
    assert p["refs"] == []
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


def test_v1_videos_last_frame_image(client):
    """last_frame_image（JSON + multipart）→ params.last_frame，FL2VA 尾帧链路。"""
    r = client.post("/v1/videos", json={
        "prompt": "lf", "first_frame_image": os.path.join(_TMP, "in.png"),
        "last_frame_image": _jpeg(b"LASTDATA")})
    assert r.status_code == 200, r.text
    p = core.get_job(r.json()["id"])["params"]
    assert Path(p["last_frame"]).read_bytes() == b"LASTDATA"
    assert p["refs"] == []
    # multipart 分支同样解析
    r2 = client.post("/v1/videos", data={"prompt": "lf2", "last_frame_image": ""})
    assert r2.status_code == 200, r2.text
    assert core.get_job(r2.json()["id"])["params"]["last_frame"] is None


def test_v1_videos_refs_frames_mutual_exclusion(client):
    """input_images 与 first/last_frame_image 并发 → 400（h3 Ref2VA 会静默忽略帧，入口显式拒绝）。"""
    payload = {"prompt": "mix", "input_images": [_jpeg(b"R")], "first_frame_image": os.path.join(_TMP, "in.png")}
    r = client.post("/v1/videos", json=payload)
    assert r.status_code == 400, r.text
    r2 = client.post("/v1/videos", data={"prompt": "mix2", "input_images": _jpeg(b"R"), "last_frame_image": ""})
    assert r2.status_code == 200, r2.text  # 空串 last_frame 不算帧，正常放行


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


def test_wait_ceiling_outlasts_image_worker(_c=None):
    # A 504 while the worker still runs orphans it, and drops the temp dir whose
    # ref PNGs the queued jobs still have to read (they then die instantly on
    # "load image ... failed"). The HTTP wait must never undercut the worker.
    from server import compat_openai
    from server.workers import qwen_image

    assert compat_openai._WAIT_TIMEOUT > qwen_image.DEFAULT_TIMEOUT, (
        f"wait {compat_openai._WAIT_TIMEOUT}s <= worker {qwen_image.DEFAULT_TIMEOUT}s")


def test_image_upscale_routing(_c=None):
    # 單一 turbo 4 步路徑。實測推翻舊前提：strength 0.35 / 4 步對 NSFW 輸入
    # 完整保留無衣狀態，所以 uncensored 不再分流到 UC base + heretic 視覺塔——
    # params["uncensored"] 保留但忽略（不動 compat_openai 的介面合約）。
    # UltraSharp 挂在生图管线上，输出经 --upscale-model。
    import tempfile

    from server.workers import image_upscale

    captured = {}

    def fake_run_cli(cmd, **_kw):
        captured["cmd"] = cmd
        Path(cmd[cmd.index("-o") + 1]).write_bytes(b"PNG")

    orig = image_upscale.run_cli
    image_upscale.run_cli = fake_run_cli
    try:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "in.png"
            src.write_bytes(b"PNG")
            image_upscale.run({"image_path": str(src), "width": 1728, "height": 960},
                              Path(tmp), lambda *a: None, lambda: False)
            cmd = captured["cmd"]
            assert "qwen_image_2.1_turbo_Q4_K_M.gguf" in " ".join(cmd)
            assert cmd[cmd.index("--steps") + 1] == "4"
            assert cmd[cmd.index("--strength") + 1] == "0.35"

            # uncensored 必須被忽略：同一條 turbo 路徑，不換底模、不掛 heretic 視覺塔
            image_upscale.run({"image_path": str(src), "width": 1728, "height": 960,
                               "uncensored": True, "steps": 4},
                              Path(tmp), lambda *a: None, lambda: False)
            cmd = captured["cmd"]
            joined = " ".join(cmd)
            assert "qwen_image_2.1_turbo_Q4_K_M.gguf" in joined, "unc must not change model"
            assert "UC-Q4_K_M" not in joined and "heretic" not in joined
            assert "--llm_vision" not in cmd
            assert cmd[cmd.index("--steps") + 1] == "4", "explicit steps must override"

            # 已刪除的檔不能再出現在任何一條路徑上
            assert "qwen_image_2.1-Q4_K.gguf" not in joined
    finally:
        image_upscale.run_cli = orig


def test_qwen_image_refs_trimmed(_c=None):
    # 9-10 refs ballooned sd-cli to ~22GB on 48GB unified memory and stalled at
    # 0.3% CPU forever — same death spiral as 1024x1024, trigger axis = ref count.
    # The worker must trim refs to MAX_REFS.
    import tempfile

    from server.workers import qwen_image

    captured = {}

    def fake_run_cli(cmd, **_kw):
        captured["cmd"] = cmd
        Path(cmd[cmd.index("-o") + 1]).write_bytes(b"PNG")

    orig = qwen_image.run_cli
    qwen_image.run_cli = fake_run_cli
    try:
        with tempfile.TemporaryDirectory() as tmp:
            refs = [f"/tmp/ref_{i}.png" for i in range(10)]
            qwen_image.run({"prompt": "x", "width": 864, "height": 480, "refs": refs},
                           Path(tmp), lambda *a: None, lambda: False)
    finally:
        qwen_image.run_cli = orig
    cmd = captured["cmd"]
    passed = [cmd[i + 1] for i, v in enumerate(cmd) if v == "-r"]
    assert len(passed) == qwen_image.MAX_REFS, f"expected {qwen_image.MAX_REFS} refs, got {len(passed)}"
    # sd.cpp 的 -s 是 --seed 缩写；steps 必须走长旗标，否则步数永远是默认 20
    assert "-s" not in cmd, "-s would set the seed, not steps"


def test_viggle_sigma_count_is_steps_plus_one(_c=None):
    # 最貴的坑：HF model card 給 6 個 sigma，那是 diffusers 格式。sd.cpp 要 steps+1 個、
    # 尾巴補 0——只給 6 個會產出整片鹽胡椒噪點，不是「畫質差」而已。
    from server.workers import qwen_image

    cmd = qwen_image._cli_cmd("x", Path("/tmp/o.png"), 1024, 1024, "viggle", 1)
    steps = int(cmd[cmd.index("--steps") + 1])
    sigmas = cmd[cmd.index("--sigmas") + 1].split(",")
    assert len(sigmas) == steps + 1, f"{steps} steps needs {steps + 1} sigmas, got {len(sigmas)}"
    assert sigmas[-1] == "0", "last sigma must be 0"
    assert cmd[cmd.index("--scheduler") + 1] == "discrete"


def test_engine_selection(_c=None):
    # unc 沿用舊語義當「走 Krea2」的快捷方式（viggle 對 NSFW 零降級，舊的
    # 去審查底模前提已不成立）。優先級：任務 engine 參數 > env > unc 推導。
    import os

    from server.workers import qwen_image

    def cmd_for(**params):
        return qwen_image._cli_cmd("x", Path("/tmp/o.png"), 1024, 1024,
                                   qwen_image._pick_engine(params, None), 1)

    orig_unc = os.environ.pop("QWEN_IMAGE_UNCENSORED", None)
    orig_eng = os.environ.pop("QWEN_IMAGE_ENGINE", None)
    try:
        # 默认 = Viggle 主力
        assert qwen_image._pick_engine({}, False) == "viggle"
        # 向後相容：unc 參數 / QWEN_IMAGE_UNCENSORED 都要能走，不能讓呼叫端炸掉
        assert qwen_image._pick_engine({"uncensored": True}, True) == "krea2"
        os.environ["QWEN_IMAGE_UNCENSORED"] = "1"
        assert qwen_image._pick_engine({}, None) == "krea2"
        # 顯式指定優先於 env
        assert qwen_image._pick_engine({"engine": "viggle"}, True) == "viggle"
        os.environ["QWEN_IMAGE_ENGINE"] = "krea2"
        assert qwen_image._pick_engine({}, False) == "krea2"
        try:
            qwen_image._pick_engine({"engine": "sdxl"}, False)
        except ValueError:
            pass
        else:
            raise AssertionError("unknown engine must raise, not silently fall back")
    finally:
        for k in ("QWEN_IMAGE_UNCENSORED", "QWEN_IMAGE_ENGINE"):
            os.environ.pop(k, None)
        if orig_unc is not None:
            os.environ["QWEN_IMAGE_UNCENSORED"] = orig_unc
        if orig_eng is not None:
            os.environ["QWEN_IMAGE_ENGINE"] = orig_eng


def test_krea2_tae_and_refs(_c=None):
    # --tae(TAEHV) 是 Krea2 的 2 倍提速來源：wan VAE 真 decode 84s / 207s。
    # 少了它 Krea2 直接慢一半以上。Krea2 走 --diffusion-fa 不是 --fa，且不開 sigmas。
    from server.workers import qwen_image

    cmd = qwen_image._cli_cmd("x", Path("/tmp/o.png"), 1024, 1024, "krea2", 1)
    assert cmd[cmd.index("--tae") + 1].endswith("taew2_1.safetensors")
    assert cmd[cmd.index("--steps") + 1] == "8"
    assert cmd[cmd.index("--cfg-scale") + 1] == "1.0"
    assert "--diffusion-fa" in cmd and "--sigmas" not in cmd
    # Krea2 用 4B + wan VAE，不是 Qwen 系那套
    assert "Qwen3VL-4B" in " ".join(cmd) and "wan_2.1_vae" in " ".join(cmd)

    # Krea2 參考圖路徑 2026-10-02 實測通過（view/krea_r4bmmproj.png），
    # 所以要 refs 時**留在 Krea2**，不再退回 Viggle。
    assert qwen_image.ENGINES["krea2"]["refs"]
    import tempfile

    captured = {}

    def fake_run_cli(c, **_kw):
        captured["cmd"] = c
        Path(c[c.index("-o") + 1]).write_bytes(b"PNG")

    orig = qwen_image.run_cli
    qwen_image.run_cli = fake_run_cli
    try:
        with tempfile.TemporaryDirectory() as tmp:
            res = qwen_image.run({"prompt": "x", "width": 512, "height": 512,
                                  "uncensored": True, "refs": ["/tmp/r.png"]},
                                 Path(tmp), lambda *a: None, lambda: False)
    finally:
        qwen_image.run_cli = orig
    assert res["engine"] == "krea2"
    assert captured["cmd"][captured["cmd"].index("--steps") + 1] == "8"
    # ⚠️ 視覺塔必須是 4B 那顆。配 8B 的會靜默失效（rc=0 但無視 -r，
    #    llm.hpp:386 "vision projector output size ... does not match"）。
    assert "mmproj-Qwen3VL-4B-Instruct-F16" in " ".join(captured["cmd"])


def test_dbcache_is_opt_in_and_ghosts_at_512(_c=None):
    # 生產預設 nocache。dbcache(th=0.1) 在 1024² 目檢乾淨，但在 512² 實測明顯鬼影：
    # 左側半透明重複人形 + 主體周圍 2~3 張幽靈臉。短劇草稿檔就是 512x288/512²，
    # 髒的剛好是產量最大那一級，省 9.1% 買不到。開回來：QWEN_IMAGE_DBCACHE=1。
    from server.workers import qwen_image

    assert qwen_image.DBCACHE_OPTION == "threshold=0.1", "ghosting/re-exposure bug"
    assert "warmup" not in qwen_image.DBCACHE_OPTION
    for eng in qwen_image.ENGINES:
        cmd = qwen_image._cli_cmd("x", Path("/tmp/o.png"), 1024, 1024, eng, 1)
        assert "--cache-mode" not in cmd, f"{eng} must not ship cache by default"
        # --offload-to-cpu 去掉反而慢，别動
        assert "--offload-to-cpu" in cmd
        # --mmap 統一記憶體下是負收益(+8%)，--eager-load 淨虧 4s，都不要加
        assert "--mmap" not in cmd and "--eager-load" not in cmd
    # 門檻/warmup 那兩個坑是「萬一開回來」的前提，別趁改預設時一併放寬
    orig = qwen_image.DBCACHE
    qwen_image.DBCACHE = True
    try:
        cmd = qwen_image._cli_cmd("x", Path("/tmp/o.png"), 1024, 1024, "viggle", 1)
    finally:
        qwen_image.DBCACHE = orig
    assert cmd[cmd.index("--cache-mode") + 1] == "dbcache"
    assert cmd[cmd.index("--cache-option") + 1] == "threshold=0.1"


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


def test_krea2_model_files_exist(_c=None):
    # M5 有已知 bug（sd.cpp issue 1990）會靜默產出純白 PNG，路徑打錯時 sd-cli
    # 有時不報錯。模型檔不存在就在跑圖前擋掉，不要等一張白圖回來。
    from server.workers import qwen_image

    root = Path(qwen_image.DEFAULT_HOME)
    if not (root / "build" / "bin" / "sd-cli").is_file():
        return  # 沒裝 sd.cpp 的機器（CI）不檢查模型檔
    for eng, e in qwen_image.ENGINES.items():
        for key in ("diff", "vae", "tae", "llm", "vision"):
            p = e.get(key)
            if p:
                assert (root / p).is_file(), f"{eng}.{key} missing: {root / p}"


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
