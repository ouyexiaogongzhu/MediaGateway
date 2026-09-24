"""Tests for workers/sdxl.py + /v1/videos sdxl route — no real daemon. Run: .venv/bin/python tests/test_sdxl.py"""
from __future__ import annotations

import io
import json
import sys
import tempfile
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.workers import sdxl  # noqa: E402


def test_payload_build():
    p = sdxl._payload({"prompt": "a cat"})
    assert p == {"model": "realvis", "prompt": "a cat", "negative_prompt": "",
                 "width": 832, "height": 1216, "steps": 30,
                 "guidance_scale": 5.0, "seed": 0}
    p = sdxl._payload({"prompt": "x", "model": "noobai", "seed": 42, "width": 1024, "height": 1024})
    assert p["model"] == "noobai" and p["seed"] == 42 and p["width"] == 1024
    try:
        sdxl._payload({"prompt": "x", "model": "flux"})
        assert False, "unknown model should raise"
    except ValueError:
        pass


class _FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_run_success_copies_png():
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data)
        captured["timeout"] = timeout
        return _FakeResp(json.dumps({"path": str(png), "elapsed_s": 20.0}).encode())

    d = tempfile.mkdtemp()
    png = Path(d) / "daemon_out.png"
    png.write_bytes(b"PNGDATA")
    job_dir = Path(tempfile.mkdtemp())
    real = sdxl.urlopen_ = __import__("urllib.request", fromlist=["urlopen"]).urlopen
    import urllib.request
    urllib.request.urlopen = fake_urlopen
    try:
        out = sdxl.run({"prompt": "a cat", "seed": 7}, job_dir,
                       lambda *a: None, lambda: False)
    finally:
        urllib.request.urlopen = real
    assert captured["url"].endswith("/generate")
    assert captured["body"]["prompt"] == "a cat" and captured["body"]["seed"] == 7
    assert captured["timeout"] == sdxl.DEFAULT_TIMEOUT
    copied = Path(out["output_path"])
    assert copied.parent == job_dir and copied.name == "output.png"
    assert copied.read_bytes() == b"PNGDATA"


def test_run_unreachable():
    def refused(req, timeout=None):
        raise urllib.error.URLError(ConnectionRefusedError("connection refused"))

    d = tempfile.mkdtemp()
    import urllib.request
    real = urllib.request.urlopen
    urllib.request.urlopen = refused
    try:
        sdxl.run({"prompt": "x"}, Path(d), lambda *a: None, lambda: False)
        assert False, "unreachable daemon should raise"
    except Exception as e:
        assert "unreachable" in str(e) and sdxl.DAEMON_URL in str(e), str(e)
    finally:
        urllib.request.urlopen = real


def test_run_bad_response():
    d = tempfile.mkdtemp()
    import urllib.request
    real = urllib.request.urlopen

    def no_path(req, timeout=None):
        return _FakeResp(b"{}")

    urllib.request.urlopen = no_path
    try:
        sdxl.run({"prompt": "x"}, Path(d), lambda *a: None, lambda: False)
        assert False, "missing path should raise"
    except Exception as e:
        assert "no path" in str(e), str(e)
    finally:
        urllib.request.urlopen = real

    def http_500(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 500, "boom", {}, io.BytesIO(b"err"))

    urllib.request.urlopen = http_500
    try:
        sdxl.run({"prompt": "x"}, Path(d), lambda *a: None, lambda: False)
        assert False, "HTTP 500 should raise"
    except Exception as e:
        assert "HTTP 500" in str(e), str(e)
    finally:
        urllib.request.urlopen = real

    try:
        sdxl.run({}, Path(d), lambda *a: None, lambda: False)
        assert False, "missing prompt should raise"
    except ValueError:
        pass


def test_cancel_before_request():
    called = {"urlopen": False}

    def boom(req, timeout=None):
        called["urlopen"] = True
        raise AssertionError("should not reach daemon")

    d = tempfile.mkdtemp()
    import urllib.request
    real = urllib.request.urlopen
    urllib.request.urlopen = boom
    try:
        try:
            sdxl.run({"prompt": "x"}, Path(d), lambda *a: None, lambda: True)
            assert False, "cancel should raise"
        except Exception as e:
            assert "cancel" in str(e).lower()
        assert not called["urlopen"]
    finally:
        urllib.request.urlopen = real


def test_video_route():
    from fastapi.testclient import TestClient
    from server import core
    from server.main import app

    saved = (core.create_job, core.get_job)

    class Fake:
        def __init__(self): self.params = None
        def create(self, jtype, params, priority=0):
            self.params = (jtype, params)
            return {"id": "sdxl_1", "status": "queued"}
        def get(self, job_id):
            return {"id": job_id, "type": "sdxl", "status": "queued",
                    "progress": 0.0, "error": None}

    fake = Fake()
    core.create_job, core.get_job = fake.create, fake.get
    try:
        client = TestClient(app)
        r = client.post("/v1/videos", json={"model": "realvis", "prompt": "a cat"})
        assert r.status_code == 200, r.text
        assert fake.params[0] == "sdxl"
        # 豎版默認：無 size 時 832x1216，不走視頻 864x480 默認
        assert fake.params[1]["width"] == 832 and fake.params[1]["height"] == 1216
        assert fake.params[1]["prompt"] == "a cat"
        assert "negative_prompt" not in fake.params[1]
        rid = r.json()["id"]
        s = client.get(f"/v1/videos/{rid}")
        assert s.status_code == 200 and s.json()["status"] == "queued"
        # noobai 顯式 size 透傳
        r = client.post("/v1/videos", json={"model": "noobai", "prompt": "x", "size": "1024x1024"})
        assert fake.params[1]["width"] == 1024 and fake.params[1]["height"] == 1024
        # 像素超 h3 上限也不攔（圖片任務面）
        r = client.post("/v1/videos", json={"model": "sdxl", "prompt": "x", "size": "1216x1216"})
        assert r.status_code == 200, r.text
    finally:
        core.create_job, core.get_job = saved


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n{len(fns)} tests passed")
