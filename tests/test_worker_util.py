"""Tests for server/workers/_util.py param coercion — no GPU.

Run: .venv/bin/python tests/test_worker_util.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.workers._util import number, seed_of  # noqa: E402


def test_number_coerce_and_bounds():
    assert number({}, "width", 1024, 32, 4096, int) == 1024
    assert number({"width": "768"}, "width", 1024, 32, 4096, int) == 768
    assert number({"width": ""}, "width", 1024, 32, 4096, int) == 1024
    for bad in ({"width": "abc"}, {"width": 100000}, {"width": -32}, {"width": 10.5},):
        try:
            number(bad, "width", 1024, 32, 4096, int)
        except ValueError as e:
            assert "width" in str(e)
        else:
            raise AssertionError(f"expected ValueError for {bad}")


def test_seed_zero_is_a_real_seed():
    assert seed_of({"seed": 0}, 42) == 0  # `or` would have swapped in 42
    assert seed_of({}, 42) == 42
    assert seed_of({"seed": 0xFFFFFFFF}, 0) == 0xFFFFFFFF
    for bad in ({"seed": -1}, {"seed": "x"}, {"seed": 2**32}):
        try:
            seed_of(bad, 0)
        except ValueError:
            pass
        else:
            raise AssertionError(f"expected ValueError for {bad}")


def test_util_not_in_registry():
    from server import core
    core._REGISTRY.clear()
    try:
        assert "_util" not in core.registry()
        assert "qwen_image" in core.registry() and "flashvsr" in core.registry()
    finally:
        core._REGISTRY.clear()


def test_run_cli_cancel_interrupts_child():
    import tempfile
    import time
    from server.workers._util import run_cli
    with tempfile.TemporaryDirectory() as d:
        polls = iter([False, False, True])  # pre-Popen check, then two wait polls
        t0 = time.monotonic()
        try:
            run_cli(["/bin/sleep", "30"], cwd=d, log_path=Path(d) / "log", env=None,
                    timeout=60, cancel=lambda: next(polls, True), engine="t")
        except Exception as e:
            assert "cancelled" in str(e)
        else:
            raise AssertionError("expected cancelled")
        assert time.monotonic() - t0 < 15  # killed mid-run, not waited to completion


def test_run_cli_timeout():
    import tempfile
    from server.workers._util import run_cli
    with tempfile.TemporaryDirectory() as d:
        try:
            run_cli(["/bin/sleep", "30"], cwd=d, log_path=Path(d) / "log", env=None,
                    timeout=2, cancel=lambda: False, engine="t")
        except Exception as e:
            assert "timeout" in str(e)
        else:
            raise AssertionError("expected timeout")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n{len(fns)} tests passed")
