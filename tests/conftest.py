"""Fixtures shared across the suite.

Each test file that touches `server.*` isolates MG_DB/MG_ASSETS *before*
importing it (core binds both at import time), so nothing here may import
server at module scope — that would bind the real DB for the whole session.
Every fixture below imports lazily, at call time.
"""
import threading
from http.server import ThreadingHTTPServer

import pytest


class _FakeEngine:
    """Stand-in for h3: writes the output file, reports one progress tick.

    Without this the suite loads the real 9B engine and the video job fails
    (or hangs for minutes). test_compat.py's `__main__` block injects its own
    copy; under pytest nothing did, so those tests asserted against a failed job.
    """

    def generate(self, prompt, *, output_path, refs=None, on_progress=None, **ov):
        with open(output_path, "wb") as f:
            f.write(b"FAKE")
        if on_progress:
            on_progress("denoise", 1, 1)
        return {"width": ov.get("width"), "height": ov.get("height"),
                "frames": ov.get("frames", 48), "fps": 24, "seed": 7}

    def close(self):
        pass


@pytest.fixture(autouse=True)
def fake_video_engine(monkeypatch):
    """Never load the real h3 engine in tests.

    Patch the factory, not the global: video.run() nulls `_engine` after every
    job (unload-by-default), so a value assigned once is gone by the next call.
    Also leave `_get_engine` alone when a test has injected its own engine
    (test_video.py sets video._engine = FakeEngine() and asserts on it).
    """
    from server.workers import video
    saved_engine, saved_get = video._engine, video._get_engine
    video._engine = _FakeEngine()
    monkeypatch.setattr(video, "_get_engine", lambda: video._engine or _FakeEngine())
    try:
        yield
    finally:
        video._engine, video._get_engine = saved_engine, saved_get


@pytest.fixture
def tmp_refs(tmp_path):
    """One file per refs kind: image, video_audio (with audio_path), audio.

    Paths, not bytes — `video._build_refs` resolves each entry against disk.
    """
    d = tmp_path / "refs"
    d.mkdir()
    (d / "in.png").write_bytes(b"PNG")
    (d / "clip.mp4").write_bytes(b"MP4")
    (d / "line.wav").write_bytes(b"WAV")
    return {"img": str(d / "in.png"), "vid": str(d / "clip.mp4"), "wav": str(d / "line.wav")}


@pytest.fixture
def client():
    """TestClient over the real app, built lazily so MG_DB/MG_ASSETS are already isolated."""
    from fastapi.testclient import TestClient
    from server.main import app
    with TestClient(app) as c:
        yield c


@pytest.fixture
def url(request):
    """Base URL of a live local HTTP server answering with the voice worker's stub.

    The stub class lives in the test module (it records payloads for assertions),
    so import it from whichever module asked for this fixture.
    `request.node.module` replaces the removed `pytest.stack()` (pytest 8);
    it's already the live module object, no sys.modules lookup needed.

    场景取自 `request.node.name`：4xx → bad400、5xx → err500，其余 ok。
    旧写法无条件重置成 ok，失败路径的测试因此永远走成功分支（报 expected RuntimeError）。
    """
    stub = getattr(request.node.module, "Stub")
    name = request.node.name
    stub.scenario = "bad400" if "4xx" in name else "err500" if "5xx" in name else "ok"
    stub.hits = 0
    stub.last_payload = None
    srv = ThreadingHTTPServer(("127.0.0.1", 0), stub)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()