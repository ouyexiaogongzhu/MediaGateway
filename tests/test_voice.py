"""voice worker = thin router to tts_qwen (CosyVoice retired 2026-10-04).

The engine itself is covered by tests/test_tts_qwen.py with a stubbed CLI.
What's worth pinning here: the router forwards params verbatim, and the three
hooks the scheduler looks up by module name (core._admit_next) keep answering —
they now report "nothing resident" because tts_qwen is a per-job subprocess.
"""
from __future__ import annotations

import struct
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.workers import voice  # noqa: E402

WAV = (b"RIFF" + struct.pack("<I", 36) + b"WAVEfmt " + struct.pack(
    "<IHHIIHH", 16, 1, 1, 24000, 48000, 2, 16) + b"data" + struct.pack("<I", 4)
    + b"\x00\x00\x00\x00")

STUB = """
import sys
args = sys.argv[1:]
with open(args[args.index("--output_path") + 1] + "/output.wav", "wb") as f:
    f.write(%r)
""" % WAV


def test_voice_run_delegates_to_tts_qwen(tmp_path, monkeypatch):
    stub = tmp_path / "stub_tts.py"
    stub.write_text(f"#!{sys.executable}\n{STUB}")
    stub.chmod(0o755)
    monkeypatch.setenv("MLXAUDIO_BIN", str(stub))

    job_dir = tmp_path / "job"
    job_dir.mkdir()
    res = voice.run({"text": "你到底想點啊？", "voice": "serena", "speed": 1.1},
                    job_dir, progress=lambda r, p="": None, cancel=lambda: False)

    assert Path(res["output_path"]).read_bytes() == WAV
    assert res["sample_rate"] == 24000


def test_scheduler_hooks_report_nothing_resident():
    """core._admit_next looks these up by module name; tts_qwen leaves nothing."""
    assert voice.busy() is False
    assert voice.resident() is False
    voice.request_unload()  # must not raise


def test_run_rejects_retired_cosyvoice_engine(tmp_path):
    """CosyVoice is gone, so engine=cosyvoice must fail loudly rather than silently
    returning Qwen3-TTS audio — silent engine substitution is a 3-layer debugging trap."""
    with tempfile.TemporaryDirectory() as d:
        try:
            voice.run({"text": "你好", "engine": "cosyvoice"},
                      Path(d), progress=lambda r, p="": None, cancel=lambda: False)
        except ValueError as e:
            assert "cosyvoice" in str(e).lower(), str(e)
        else:
            raise AssertionError("engine=cosyvoice was silently accepted")