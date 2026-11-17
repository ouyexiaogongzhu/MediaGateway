"""Fixtures shared across the suite.

Each test file that touches `server.*` isolates MG_DB/MG_ASSETS *before*
importing it (core binds both at import time), so nothing here may import
server at module scope — that would bind the real DB for the whole session.
Every fixture below imports lazily, at call time.
"""
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