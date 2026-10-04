"""Voice worker: thin router to the Qwen3-TTS MLX engine (tts_qwen).

CosyVoice was retired 2026-10-04 (no Cantonese G2P in CosyVoice3's tokenizer —
pronunciation errors were architectural, not a version problem). Everything now
goes through mlx-audio; this module stays because shot.py and the scheduler
address the "voice" job type.

The scheduler calls busy()/resident()/request_unload() on this module to keep
GPU-heavy jobs from stacking. tts_qwen is a per-job subprocess that exits when
the job ends, so nothing is resident between jobs and unload is a no-op — the
hooks stay only because core._admit_next looks them up by module name.
"""
from __future__ import annotations

from pathlib import Path

TYPE = "voice"
MEM_GB = 8.7  # 排程預算用的峰值（TTS 進程運行期間）


def busy() -> bool:
    """合成进行中 —— 调度器不得此时卸载。tts_qwen 是短任務子進程，不常駐。"""
    return False


def resident() -> bool:
    """tts_qwen 每個 job 開子進程、跑完即退，job 之間不佔顯存。"""
    return False


def request_unload() -> None:
    """沒有常駐進程可殺 —— 每次 job 的記憶體由 MEM_GB 排程預算管。"""


# CosyVoice 已退役，唯一引擎是 tts_qwen。engine 字段若还在请求里，明确拒绝而不是
# 静默换引擎：拿到 cosyvoice 却静默出 TTS 音，查起来要翻三层才知道发生了什么。
_KNOWN_ENGINES = {"", "qwen", "qwen3", "qwen3-tts", "tts_qwen"}


def run(params: dict, job_dir: Path, progress, cancel) -> dict:
    engine = str(params.get("engine") or "").strip().lower()
    if engine not in _KNOWN_ENGINES:
        raise ValueError(
            f"unsupported voice engine {params.get('engine')!r}; "
            "cosyvoice was retired 2026-10-04, only tts_qwen remains")
    from . import tts_qwen
    return tts_qwen.run(params, job_dir, progress, cancel)