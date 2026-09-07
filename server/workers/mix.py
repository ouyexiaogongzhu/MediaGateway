"""Mix worker: ffmpeg — audio tracks onto a video.

Two param shapes:
- audio_tracks (render.mux, re-encode): {video, audio_tracks: [{path, start?, loop?}],
  output_name?="final.mp4", subtitles?, dialogue_volume?, music_volume?}
- tracks (legacy, -c:v copy): [{"sfx_tag": "wind" | "path": "/abs/file.mp3",
  "gain_db": 0.0, "start_s": 0.0}] — sfx_tag picks a random file from
  MG_ASSETS/sfx/<tag>/. Empty tracks strips audio.
"""
from __future__ import annotations

import os
import random
import shutil
import subprocess
import threading
from pathlib import Path

from .. import core, render

TYPE = "mix"
MEM_GB = 1

DEFAULT_TIMEOUT = 600.0
FFMPEG = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"  # launchd PATH misses homebrew
_AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}

# ponytail: one global lock serializes ffmpeg subprocesses — parallel ffmpeg is
# unmeasured; drop once concurrent mix throughput is measured (see image.py).
_run_lock = threading.Lock()


def _pick_sfx(tag: str) -> str:
    d = core.ASSET_ROOT / "sfx" / tag
    files = sorted(p for p in d.iterdir()
                   if p.is_file() and p.suffix.lower() in _AUDIO_EXTS) if d.is_dir() else []
    if not files:
        raise ValueError(f"no sfx files for tag {tag!r} ({d})")
    return str(random.choice(files))


def _video_duration(path: str) -> float:
    probe = shutil.which("ffprobe") or FFMPEG.replace("ffmpeg", "ffprobe")
    out = subprocess.run([probe, "-v", "error",
                          "-show_entries", "format=duration", "-of", "csv=p=0", path],
                         capture_output=True, text=True, timeout=30)
    return float(out.stdout.strip())


def _build_cmd(video_path: str, tracks: list[dict], out: Path,
               duration: float | None = None) -> list[str]:
    cmd = [FFMPEG, "-y", "-v", "error", "-i", video_path]
    for t in tracks:
        cmd += ["-i", t["path"]]
    fc = "".join(
        f"[{i + 1}:a]adelay={int(float(t.get('start_s', 0.0) or 0.0) * 1000)}:all=1,"
        f"volume={float(t.get('gain_db', 0.0))}dB[a{i}];"
        for i, t in enumerate(tracks))
    if fc:
        cmd += ["-filter_complex",
                fc + "".join(f"[a{i}]" for i in range(len(tracks)))
                + f"amix=inputs={len(tracks)}:normalize=0[aout]",
                "-map", "0:v", "-map", "[aout]", "-c:v", "copy", "-c:a", "aac"]
    else:  # no tracks: strip audio
        cmd += ["-an", "-c:v", "copy"]
    if duration:
        # amix runs to the longest track; long ambience sfx would stretch the
        # mp4 with a frozen-video tail — cap the output at the video duration
        cmd += ["-t", f"{duration:.3f}"]
    return cmd + [str(out)]


def _run_mux(params: dict, job_dir: Path, progress, cancel) -> dict:
    """audio_tracks shape → render.mux（重编码 aac，支持垫底循环/字幕/分轨音量）。

    loop 条目的 start 被忽略（垫底恒从 0 循环铺满视频时长）。"""
    video_path = params.get("video") or params.get("video_path")
    if not video_path or not Path(video_path).is_file():
        raise ValueError(f"video not found: {video_path}")
    tracks = params.get("audio_tracks") or []
    if not isinstance(tracks, list) or not all(
            isinstance(t, dict) and isinstance(t.get("path"), str) and t["path"]
            for t in tracks):
        raise ValueError(
            "audio_tracks 必须是 [{path, start?, loop?}] 列表（path 必填非空）")
    output = str(job_dir / params.get("output_name", "final.mp4"))
    if cancel():
        raise Exception("cancelled")
    progress(0.1, "muxing")
    with _run_lock:  # 与 legacy 路径一致：全局串行 ffmpeg 子进程
        render.mux(video_path, tracks, output,
                   subtitles=params.get("subtitles"),
                   dialogue_volume=float(params.get("dialogue_volume", 1.0)),
                   music_volume=float(params.get("music_volume", 0.15)))
    progress(1.0, "done")
    return {"output_path": output, "tracks": len(tracks)}


def run(params: dict, job_dir: Path, progress, cancel) -> dict:
    if "audio_tracks" in params or params.get("subtitles"):
        if params.get("tracks"):
            raise ValueError(
                "tracks 与 audio_tracks/subtitles 混用：请只传一种契约")
        return _run_mux(params, job_dir, progress, cancel)
    video_path = params.get("video_path")
    if not video_path or not Path(video_path).is_file():
        raise ValueError(f"video_path not found: {video_path}")
    tracks = params.get("tracks") or []
    if not isinstance(tracks, list):
        raise ValueError("tracks must be a list")
    for t in tracks:
        if not isinstance(t, dict):
            raise ValueError("tracks entries must be objects")
        if not (t.get("path") or t.get("sfx_tag")):
            raise ValueError("track needs path or sfx_tag")
    resolved = []
    for t in tracks:
        t = dict(t)
        if t.get("sfx_tag"):
            t["path"] = _pick_sfx(t["sfx_tag"])
        if not t.get("path") or not Path(t["path"]).is_file():
            raise ValueError(f"track file not found: {t.get('path')}")
        resolved.append(t)

    out = job_dir / "output.mp4"
    if cancel():
        raise Exception("cancelled")
    progress(0.1, "mixing")
    try:
        duration = _video_duration(video_path)
    except Exception:  # noqa: BLE001 — probe failed: no cap (old behaviour)
        duration = None
    with _run_lock:
        try:
            proc = subprocess.run(
                _build_cmd(video_path, resolved, out, duration), capture_output=True, text=True,
                timeout=float(params.get("timeout", DEFAULT_TIMEOUT)))
        except subprocess.TimeoutExpired:
            raise Exception(f"mix timeout after {params.get('timeout', DEFAULT_TIMEOUT)}s")
    progress(0.95, "saving")
    if proc.returncode != 0:
        raise Exception(f"ffmpeg exited {proc.returncode}: {(proc.stderr or '')[-500:]}")
    if not out.is_file():
        raise Exception("mix produced no output")
    return {"output_path": str(out), "tracks": len(resolved)}
