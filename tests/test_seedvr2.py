"""Tests for workers/seedvr2.py + /v1/upscale route — no real model. Run: .venv/bin/python tests/test_seedvr2.py"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.workers import seedvr2  # noqa: E402


def test_cli_cmd_build():
    cmd = seedvr2._cli_cmd("/tmp/in.mp4", "/tmp/out.mp4", "2160", "7b")
    assert cmd[1].endswith("inference_cli.py") and cmd[2] == "/tmp/in.mp4"
    assert cmd[cmd.index("--dit_model") + 1] == "seedvr2_ema_7b_fp16.safetensors"
    assert cmd[cmd.index("--resolution") + 1] == "2160"
    assert cmd[cmd.index("--output") + 1] == "/tmp/out.mp4"
    assert cmd[cmd.index("--batch_size") + 1] == "33"  # 4n+1


def test_run_validates(tmp_path=None):
    import tempfile
    d = tempfile.mkdtemp()
    try:
        seedvr2.run({"video_path": str(Path(d) / "nope.mp4")}, Path(d),
                    lambda *a: None, lambda: False)
        assert False, "missing video should raise"
    except ValueError:
        pass
    try:
        seedvr2.run({"video_path": "/dev/null", "model": "99b"}, Path(d),
                    lambda *a: None, lambda: False)
        assert False, "bad model should raise"
    except ValueError:
        pass


def test_upscale_route():
    from fastapi.testclient import TestClient
    from server import core
    from server.main import app

    saved = (core.create_job, core.get_job)

    class Fake:
        def __init__(self): self.n = 0
        def create(self, jtype, params, priority=0):
            self.n += 1
            self.params = params
            return {"id": f"ups_{self.n}", "status": "queued"}
        def get(self, job_id):
            return {"id": job_id, "type": "seedvr2", "status": "queued",
                    "progress": 0.0, "error": None}

    fake = Fake()
    core.create_job, core.get_job = fake.create, fake.get
    try:
        client = TestClient(app)
        with tempfile.TemporaryDirectory() as d:
            v = Path(d) / "in.mp4"
            v.write_bytes(b"fake")
            r = client.post("/v1/upscale", files={"video": ("in.mp4", v.read_bytes(), "video/mp4")},
                            data={"resolution": "2160"})
            assert r.status_code == 200, r.text
            assert fake.params["resolution"] == "2160"
            assert fake.params["video_path"].endswith(".mp4")
            rid = r.json()["id"]
            s = client.get(f"/v1/videos/{rid}")
            assert s.status_code == 200 and s.json()["status"] == "queued"
        r = client.post("/v1/upscale", json={"video_path": "/nope.mp4"})
        assert r.status_code == 400
    finally:
        core.create_job, core.get_job = saved


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n{len(fns)} tests passed")
