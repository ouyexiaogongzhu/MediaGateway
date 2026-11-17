"""Tests for server/core.py recovery + cancel semantics — no GPU.

Run: .venv/bin/python tests/test_core.py
"""
from __future__ import annotations

import sys
import tempfile
import types
from contextlib import contextmanager
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


def test_budget_counts_resident_llm():
    """_resident_gb counts only between-job residents: the chat LLM face.
    (The old video keep_loaded / voice tts_server residents died with those
    workers — mmh3 spawns a fresh CLI per job, nothing stays resident.)"""
    llm = types.SimpleNamespace(MEM_GB=19.0, resident=lambda: True)
    sys.modules["server.llm"] = llm
    try:
        assert core._resident_gb(core._REGISTRY) == 19.0
    finally:
        del sys.modules["server.llm"]
    assert core._resident_gb(core._REGISTRY) == 0.0


# --- scheduler budget: a running job's own engine must be charged once ---

class _FakeWorker:
    """Stand-in for a server/workers/*.py module — only the scheduler-visible
    surface (MEM_GB). No threads, no GPU: _admit_next only reads the DB, the
    registry and that number, so fakes at the registry boundary exercise the
    real accounting instead of mocking it."""

    def __init__(self, mem_gb):
        self.MEM_GB = mem_gb


@contextmanager
def _sched(**workers):
    """Point core at fake workers + a throwaway DB, yield the connection.
    Replaces the registry rather than merging — a merge would leave the real
    workers in play (conftest injects a fake video._engine), making these
    assertions depend on suite order."""
    saved = dict(core._REGISTRY)
    _fresh_db()
    core._REGISTRY.clear()
    core._REGISTRY.update(workers)
    try:
        yield core.db()
    finally:
        core._REGISTRY.clear()
        core._REGISTRY.update(saved)


def _add(db, jid, jtype, status, seq=0):
    db.execute("INSERT INTO jobs (id, type, status, created_at) VALUES (?,?,?,?)",
               (jid, jtype, status, seq))
    db.commit()


def _round():
    """One scheduler round: admit the next job and mark it running, the way
    scheduler_loop does. Returns the admitted job id, or None when nothing fits."""
    nxt = core._admit_next()
    if nxt is None:
        return None
    _worker, job = nxt
    core._update(job["id"], status="running", started_at=1.0)
    return job["id"]


def _drain(limit=10):
    """Run scheduler rounds until the queue stops yielding work."""
    got = []
    while len(got) < limit:
        jid = _round()
        if jid is None:
            break
        got.append(jid)
    return got


def test_video_stays_exclusive_while_one_is_running():
    """One 35GB video job + a second queued one: 35+35 > 40 blocks the second —
    the budget alone keeps GPU engines exclusive (no double-charge involved;
    _resident_gb no longer tracks per-worker engines, mmh3 spawns per job)."""
    vid = _FakeWorker(35.0)
    with _sched(video=vid, concat=_FakeWorker(1.0)) as db:
        _add(db, "run", "video", "running")
        _add(db, "next2", "video", "queued", 1)
        assert core._admit_next() is None, "second video job must wait (35+35 > 40)"


def test_budget_ceiling_holds_across_a_video_batch():
    """Admit everything that fits, refuse the first job that doesn't: charged
    never passes BUDGET_GB, and the refused head blocks the rest (FIFO)."""
    vid = _FakeWorker(35.0)
    with _sched(video=vid, concat=_FakeWorker(1.0)) as db:
        _add(db, "run", "video", "running")
        for i in range(7):
            _add(db, f"c{i}", "concat", "queued", i + 1)
        # 35 (video) + 5 x 1.0 = 40 == BUDGET_GB exactly; c5 would be 41
        assert _drain() == [f"c{i}" for i in range(5)]
        charged = sum(core.registry()[r["type"]].MEM_GB for r in
                      core.db().execute("SELECT type FROM jobs WHERE status='running'"))
        charged += core._resident_gb(core.registry(), {"video"})
        assert charged == core.BUDGET_GB, charged


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n{len(fns)} tests passed")
