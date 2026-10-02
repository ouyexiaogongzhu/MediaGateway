"""Fixtures shared across the suite.

Each test file that touches `server.*` isolates MG_DB/MG_ASSETS *before*
importing it (core binds both at import time), so nothing here may import
server at module scope — that would bind the real DB for the whole session.
Every fixture below imports lazily, at call time.
"""
import threading
from http.server import ThreadingHTTPServer

import pytest


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
def url():
    """Base URL of a live local HTTP server answering with the voice worker's stub.

    The stub class lives in the test module (it records payloads for assertions),
    so import it from whichever module asked for this fixture.
    """
    import sys
    mod = sys.modules[pytest.stack()[1].module]
    stub = getattr(mod, "Stub")
    stub.scenario = "ok"
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