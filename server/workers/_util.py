"""Shared helpers for subprocess-wrapping workers.

Underscore-prefixed module: core.registry() only registers modules with
TYPE+run, and skips _* outright — safe to hold plain functions.
"""
from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path


def job_output(job_dir, params, default="final.mp4"):
    """Resolve params['output_name'] inside job_dir, refusing escapes.

    `job_dir / name` drops the left side entirely when name is absolute, and
    `..` walks out — and the mux runs ffmpeg -y, so that is arbitrary file
    overwrite driven by the free-form /v1/jobs params dict.
    """
    out = (Path(job_dir) / str(params.get("output_name", default))).resolve()
    root = Path(job_dir).resolve()
    if out != root and root not in out.parents:
        raise ValueError("output_name must stay inside the job directory")
    return str(out)


def number(params: dict, key: str, default, lo, hi, cast):
    """Coerce params[key] via cast() with bounds. Clean ValueError instead of
    a raw `invalid literal for int()` leaking into the job error field."""
    raw = params.get(key)
    if raw is None or raw == "":
        return default
    try:
        v = cast(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{key} must be a number, got {raw!r}") from None
    if not lo <= v <= hi:
        raise ValueError(f"{key} out of range [{lo}, {hi}]: {v}")
    return v


def seed_of(params: dict, default: int) -> int:
    # seed=0 is valid — never `params.get("seed") or default` here
    return number(params, "seed", default, 0, 0xFFFFFFFF, int)


def _kill(proc: subprocess.Popen) -> None:
    """SIGKILL the child's process group and reap it (no zombie). Tolerates an
    already-exited group — cancel can race completion."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    proc.wait()


def run_cli(cmd: list[str], *, cwd: str, log_path: os.PathLike, env: dict | None,
            timeout: float, cancel, engine: str) -> None:
    """Run cmd in its own session, output streamed to log_path; raise with the
    log tail on failure or timeout, on cancellation kill the group and raise.

    ponytail: log file + wait + killpg, never capture_output (a dead child
    holding the pipe blocks the parent forever).
    """
    if cancel():
        raise Exception("cancelled")
    with open(log_path, "w") as log:  # context manager: closed even on raise
        proc = subprocess.Popen(cmd, cwd=cwd, stdout=log, stderr=subprocess.STDOUT,
                                env=env, start_new_session=True)
        deadline = time.monotonic() + timeout
        while True:
            try:
                proc.wait(timeout=2)  # poll cancel() mid-run; SIGKILL needs no grace
                break
            except subprocess.TimeoutExpired:
                if cancel():
                    _kill(proc)
                    raise Exception("cancelled") from None
                if time.monotonic() >= deadline:
                    _kill(proc)
                    raise Exception(f"{engine} timeout after {timeout}s") from None
        if cancel():
            _kill(proc)
            raise Exception("cancelled")
    if proc.returncode != 0:
        try:
            with open(log_path, errors="ignore") as f:
                tail = f.read()[-500:]
        except OSError:
            tail = ""
        raise Exception(f"{engine} exited {proc.returncode}: {tail}")
