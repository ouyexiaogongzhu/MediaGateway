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
        assert "mmh3" in core.registry() and "mmh3_image" in core.registry()
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


def test_mmh3_ref2va_cmd_shape(tmp_path):
    """refs → Ref2VA 路徑：--reference 逐個、Turbo LoRA 8步 shift 6/3 sol、無 --patch。"""
    from server.workers import mmh3

    ref = tmp_path / "ref.png"
    ref.write_bytes(b"PNG")
    captured = {}

    def fake_run_cli(cmd, **kw):
        captured["cmd"] = cmd
        out = kw["log_path"].parent / "output.mp4"
        out.write_bytes(b"MP4")
        if kw.get("on_log"):
            kw["on_log"]("step 8/8\n")
        return {}

    saved = mmh3.run_cli
    mmh3.run_cli = fake_run_cli
    saved_root = mmh3.MODELS_ROOT
    lora_dir = tmp_path / "loras"
    lora_dir.mkdir()
    (lora_dir / mmh3.REF2VA_LORA).write_bytes(b"LoRA")  # 下載與否不該影響 cmd 形狀測試
    mmh3.MODELS_ROOT = tmp_path
    try:
        res = mmh3.run({"prompt": "dancing", "width": 768, "height": 1344,
                        "frames": 120, "seed": 3, "steps": 4,  # API 層塞進來的預設
                        "refs": [{"kind": "image", "path": str(ref)},
                                 {"kind": "image", "path": str(ref)}]},
                       tmp_path, lambda *a: None, lambda: False)
        cmd = captured["cmd"]
        assert cmd.count("--reference") == 2
        assert cmd[cmd.index("--lora") + 1] == mmh3.REF2VA_LORA
        assert cmd[cmd.index("--steps") + 1] == "4"          # DMAD 蒸餾步數（API 塞的預設丟棄）
        assert "--patch" not in cmd                          # FastH3 patch 是 fl2va 的
        assert cmd[cmd.index("--shift-video") + 1] == "12"
        assert cmd[cmd.index("--shift-audio") + 1] == "2"
        assert cmd[cmd.index("--sampler") + 1] == "renoise"
        assert cmd[cmd.index("--attention") + 1] == "sol"
        assert res["mode"] == "ref2va-dmad"
        # 純 T2VA 路徑不受影響：仍帶 --patch、預設 4 步
        mmh3.run({"prompt": "cat", "width": 768, "height": 1344, "frames": 120},
                 tmp_path, lambda *a: None, lambda: False)
        cmd2 = captured["cmd"]
        assert "--patch" in cmd2 and cmd2[cmd2.index("--steps") + 1] == "4"
        assert "--reference" not in cmd2
        # >9 張拒絕
        try:
            mmh3.run({"prompt": "x", "refs": [{"kind": "image", "path": str(ref)}] * 10},
                     tmp_path, lambda *a: None, lambda: False)
            raise AssertionError("expected ValueError")
        except ValueError as e:
            assert "9" in str(e)
    finally:
        mmh3.run_cli = saved
        mmh3.MODELS_ROOT = saved_root


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n{len(fns)} tests passed")
