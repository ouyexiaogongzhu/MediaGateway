"""Tests for server/compat_openai.py — mocked jobs, no GPU.

Run: .venv/bin/python tests/test_compat_openai.py
"""
from __future__ import annotations

import base64
import json
import os
import struct
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 隔离：绝不碰真实 data/gateway.db 与真实 ：8000 LLM（与 test_compat 同款）
os.environ.setdefault("MG_DB", os.path.join(tempfile.mkdtemp(prefix="mgtest_"), "gateway.db"))
os.environ.setdefault("QWEN_PORT", "8899")

from fastapi.testclient import TestClient  # noqa: E402

from server import compat_openai, core  # noqa: E402
from server.main import app  # noqa: E402


def _png_bytes() -> bytes:
    def chunk(tag, data):
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c))
    import zlib
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00")) + chunk(b"IEND", b""))


class FakeJob:
    """Replaces core.create_job with an instantly-completed job."""

    def __init__(self, output_file):
        self.output_file = output_file
        self.calls = []
        self.counter = 0

    def create(self, jtype, params, priority=0):
        self.counter += 1
        self.calls.append((jtype, params))
        jid = f"fake_{self.counter:04d}"
        if not Path(self.output_file).exists():  # don't clobber caller-provided files
            Path(self.output_file).write_bytes(_png_bytes() if jtype in ("image", "mmh3_image") else b"RIFF")
        return {"id": jid, "type": jtype, "status": "completed", "progress": 1.0,
                "output_path": self.output_file, "error": None}

    def get(self, job_id):
        return {"id": job_id, "type": "fake", "status": "completed", "progress": 1.0,
                "output_path": self.output_file, "error": None}


@contextmanager
def _fake_jobs():
    """core.create_job/get_job 換成 FakeJob，yield (client, fake)，退出時還原。

    raise_server_exceptions=False：n 非整數是既有的未捕捉 ValueError，要拿到
    500 狀態碼而不是讓它從 client.post 丟出來。
    """
    with tempfile.TemporaryDirectory() as d:
        fake = FakeJob(str(Path(d) / "o.png"))
        saved = (core.create_job, core.get_job)
        core.create_job, core.get_job = fake.create, fake.get
        try:
            yield TestClient(app, raise_server_exceptions=False), fake
        finally:
            core.create_job, core.get_job = saved


def _edits(client, model="h3-image", **form):
    return client.post("/v1/images/edits",
                       data={"prompt": "a cat", "model": model, **form},
                       files=[("image", ("a.png", _png_bytes(), "image/png"))])


def test_ssrf_guard_blocks_internal_targets():
    """LAN / link-local(169.254.169.254 metadata) 拒絕；loopback 放行（本地
    canvas :8090 / SDXL :8187 / gateway /files 全是 loopback，by design）。"""
    for url in ("http://192.168.1.5/x.png", "http://10.0.0.2/x.png",
                "http://169.254.169.254/latest/meta-data/"):
        try:
            compat_openai._ssrf_check(url)
        except Exception:
            pass
        else:
            raise AssertionError(f"ssrf guard let through {url}")
    compat_openai._ssrf_check("http://127.0.0.1:8600/files/x.mp4")  # loopback allowed


def _engine_of(params: dict) -> str:
    """用 worker 自己的 _pick_engine 解析（測試裡不複寫一份優先級），
    順手清掉環境變數，免得開發機的 QWEN_IMAGE_ENGINE 污染結果。"""
    saved = {k: os.environ.pop(k, None)
             for k in ("QWEN_IMAGE_ENGINE", "QWEN_IMAGE_UNCENSORED")}
    try:
        # 本地 stub：engine 優先級 = engine 參數 > env > unc（原 qwen_image._pick_engine）
        want = (params.get("engine") or os.environ.get("QWEN_IMAGE_ENGINE") or "").lower()
        return want or ("krea2" if params.get("uncensored") else "viggle")
    finally:
        os.environ.update({k: v for k, v in saved.items() if v is not None})


def test_chat_model_name_never_reaches_the_image_worker():
    """守門必須匹配 h3-image/mmh3 而非裸 qwen：CHAT 模型名（qwen3.8-27b、
    sora-2 等非生圖名）落到 501，一個生圖 job 都不排。"""
    with tempfile.TemporaryDirectory() as d:
        fake = FakeJob(str(Path(d) / "o.png"))
        saved = (core.create_job, core.get_job)
        core.create_job, core.get_job = fake.create, fake.get
        try:
            client = TestClient(app)
            r = client.post("/v1/images/generations",
                            json={"model": "qwen3.8-27b", "prompt": "hello"})
            assert r.status_code == 501, r.text  # 落到非生圖分支（舊引擎已退役）
            r = client.post("/v1/images/edits", data={"prompt": "hello", "model": "qwen3.8-27b"},
                            files=[("image", ("a.png", _png_bytes(), "image/png"))])
            assert r.status_code == 501, r.text
            assert fake.calls == [], fake.calls  # 一個生圖 job 都沒排
        finally:
            core.create_job, core.get_job = saved


def test_generations_seed_increments_per_image():
    """n 張圖共用 seed+i（否則 n>1 會回同一張圖）。"""
    with tempfile.TemporaryDirectory() as d:
        fake = FakeJob(str(Path(d) / "o.png"))
        saved = (core.create_job, core.get_job)
        core.create_job, core.get_job = fake.create, fake.get
        try:
            client = TestClient(app)
            r = client.post("/v1/images/generations", json={
                "model": "h3-image", "prompt": "a cat", "n": 3, "seed": 42})
            assert r.status_code == 200 and len(r.json()["data"]) == 3, r.text
            assert [p["seed"] for _, p in fake.calls] == [42, 43, 44]
        finally:
            core.create_job, core.get_job = saved


def test_edits_n1_returns_one_image():
    """預設路徑不能變：n=1 仍然只有 1 個 job、1 個 entry。"""
    with _fake_jobs() as (client, fake):
        r = _edits(client, n="1")
        assert r.status_code == 200, r.text
        assert len(r.json()["data"]) == 1
        assert [j for j, _ in fake.calls] == ["mmh3_image"]


def test_edits_n3_returns_three_images():
    """edits 以前 clamp 了 n 卻從不讀它 → n>1 靜默只回 1 張（HTTP 200，無處報錯）。"""
    with _fake_jobs() as (client, fake):
        r = _edits(client, n="3")
        assert r.status_code == 200, r.text
        assert len(r.json()["data"]) == 3
        assert [j for j, _ in fake.calls] == ["mmh3_image"] * 3


def test_edits_seed_increments_per_image():
    """n 張圖用 seed+i（共用一個 seed 就是回同一張圖 n 次）。"""
    with _fake_jobs() as (client, fake):
        r = _edits(client, n="3", seed="42")
        assert r.status_code == 200 and len(r.json()["data"]) == 3, r.text
        assert [p["seed"] for _, p in fake.calls] == [42, 43, 44]


def test_edits_n_clamping_unchanged():
    """0 / 負數照舊夾成 1；非整數是既有的未捕捉 ValueError（500），這次沒動它。"""
    with _fake_jobs() as (client, fake):
        for raw in ("0", "-3"):
            fake.calls.clear()
            r = _edits(client, n=raw)
            assert r.status_code == 200 and len(r.json()["data"]) == 1, (raw, r.text)
            assert len(fake.calls) == 1, raw
        fake.calls.clear()
        r = _edits(client, n="abc")
        assert r.status_code == 500, r.text
        assert fake.calls == [], fake.calls  # 壞 n 不排 job


def test_image_generations_returns_b64():
    with tempfile.TemporaryDirectory() as d:
        png = str(Path(d) / "out.png")
        fake = FakeJob(png)
        saved = (core.create_job, core.get_job)
        core.create_job, core.get_job = fake.create, fake.get
        try:
            client = TestClient(app)
            # 舊引擎全退役：無 model（舊默認）→ 501
            r = client.post("/v1/images/generations", json={
                "prompt": "a cat", "size": "2048x1024", "seed": 42})
            assert r.status_code == 501, r.text
            # h3-image → mmh3_image worker
            r = client.post("/v1/images/generations", json={
                "model": "h3-image", "prompt": "a cat",
                "size": "2048x1024", "seed": 42})
            assert r.status_code == 200, r.text
            data = r.json()["data"][0]["b64_json"]
            assert base64.b64decode(data)[:8] == b"\x89PNG\r\n\x1a\n"
            assert fake.calls[0][0] == "mmh3_image"
            assert fake.calls[0][1]["width"] == 2048 and fake.calls[0][1]["height"] == 1024
            assert fake.calls[0][1]["seed"] == 42
        finally:
            core.create_job, core.get_job = saved


def test_image_size_rounding_and_bad_size():
    with tempfile.TemporaryDirectory() as d:
        fake = FakeJob(str(Path(d) / "o.png"))
        saved = (core.create_job, core.get_job)
        core.create_job, core.get_job = fake.create, fake.get
        try:
            client = TestClient(app)
            # 32 倍數取整；H3-Image 工作範圍單邊 512-6400
            client.post("/v1/images/generations", json={"model": "h3-image", "prompt": "x", "size": "867x547"})
            assert (fake.calls[0][1]["width"], fake.calls[0][1]["height"]) == (864, 544)
            r = client.post("/v1/images/generations", json={"prompt": "x", "size": "big"})
            assert r.status_code == 400
            r = client.post("/v1/images/generations", json={"model": "h3-image", "prompt": "x", "size": "7000x1000"})
            assert r.status_code == 400  # max side 6400
            r = client.post("/v1/images/generations", json={"model": "h3-image", "prompt": "x", "size": "480x480"})
            assert r.status_code == 400  # min side 512（取整後 448/480 皆低於工作範圍）
            fake.calls.clear()
            client.post("/v1/images/generations", json={"model": "h3-image", "prompt": "x"})
            assert (fake.calls[0][1]["width"], fake.calls[0][1]["height"]) == (2048, 2048)
        finally:
            core.create_job, core.get_job = saved


def test_image_edits_rejects_bad_input():
    client = TestClient(app)
    r = client.post("/v1/images/edits", data={"prompt": "x"})
    assert r.status_code == 400
    r = client.post("/v1/images/edits", data={"prompt": "x"},
                    files=[("image", ("a.png", _png_bytes(), "image/png")),
                           ("mask", ("m.png", _png_bytes(), "image/png"))])
    assert r.status_code == 400


