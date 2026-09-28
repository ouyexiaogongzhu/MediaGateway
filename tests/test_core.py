"""Tests for server/core.py recovery + cancel semantics — no GPU.

Run: .venv/bin/python tests/test_core.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server import core  # noqa: E402


def _reset_core_db(path: Path | None = None):
    """Drop cached connections; optionally point core at a new temp DB."""
    if path is None:
        d = tempfile.mkdtemp(prefix="core_test_")
        path = Path(d) / "gateway.db"
    core.DB_PATH = path
    core._db_initialized = False
    core._tls.conn = None  # drop this thread's cached connection


def _fresh_db():
    _reset_core_db()
    return core.db()


def test_startup_recovery_marks_running_failed():
    db = _fresh_db()
    db.execute("INSERT INTO jobs (id, type, status, created_at) "
               "VALUES ('x1', 'video', 'running', 0)")
    db.execute("INSERT INTO jobs (id, type, status, created_at) "
               "VALUES ('x2', 'video', 'queued', 0)")
    db.commit()
    # simulate restart: same path, but drop cached conn + init flag
    same_path = Path(db.execute("PRAGMA database_list").fetchone()[2])
    _reset_core_db(same_path)
    db2 = core.db()
    assert db2.execute("SELECT status FROM jobs WHERE id='x1'").fetchone()[0] == "failed"
    assert db2.execute("SELECT status FROM jobs WHERE id='x2'").fetchone()[0] == "queued"


def test_cancel_finished_job_returns_false():
    db = _fresh_db()
    db.execute("INSERT INTO jobs (id, type, status, created_at) "
               "VALUES ('f1', 'video', 'completed', 0)")
    db.commit()
    core._CANCELLED.discard("f1")
    assert core.cancel_job("f1") is False
    assert "f1" not in core._CANCELLED  # no zombie entry for finished jobs


def test_run_job_fails_job_when_setup_raises():
    """Regression: mkdir/_update 在 try 內 — setup 失敗必須把 job 標 failed，
    而不是讓它永遠卡在 'running'（毒化 memory budget）。"""
    db = _fresh_db()
    db.execute("INSERT INTO jobs (id, type, status, created_at) "
               "VALUES ('o1', 'video', 'running', 0)")
    db.commit()
    blocker = Path(tempfile.mkdtemp()) / "notadir"
    blocker.write_text("x")  # mkdir 目标父路径是文件 => NotADirectoryError
    old = core.ASSET_ROOT
    core.ASSET_ROOT = blocker
    try:
        core._run_job(object(), {"id": "o1", "params": {}})  # worker 永远不会被调到
    finally:
        core.ASSET_ROOT = old
    job = core.get_job("o1")
    assert job["status"] == "failed" and job["error"], job


def test_budget_counts_resident_engines():
    class FakeVideo:
        MEM_GB = 35.0
        _engine = object()  # resident (keep_loaded)

    class FakeVoice:
        MEM_GB = 8.7

        class _P:
            @staticmethod
            def poll():
                return None  # alive

        _proc = _P()

    saved = dict(core._REGISTRY)
    try:
        core._REGISTRY.update({"video": FakeVideo, "voice": FakeVoice})
        gb = core._resident_gb(core._REGISTRY)
        assert abs(gb - 43.7) < 0.01, gb  # 35 (engine) + 8.7 (tts alive)
    finally:
        core._REGISTRY.clear()
        core._REGISTRY.update(saved)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n{len(fns)} tests passed")
