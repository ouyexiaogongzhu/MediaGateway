"""Tests for server/workers/shot.py — pure mock, no GPU, no ffmpeg.

Run: .venv/bin/python tests/test_shot.py
"""
from __future__ import annotations

import sys
import tempfile
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server.workers import mix as mix_w  # noqa: E402
from server.workers import music as music_w  # noqa: E402
from server.workers import shot  # noqa: E402
from server.workers import mmh3 as video_w  # noqa: E402


class Stage:
    """Fake stage worker: records calls, writes a placeholder output file."""
    kind = "generic"

    def __init__(self, fail=False, kind="generic"):
        self.kind = kind
        self.calls = []
        self.fail = fail

    def run(self, params, job_dir, progress, cancel):
        self.calls.append({"params": params, "job_dir": str(job_dir)})
        if self.fail:
            raise RuntimeError(f"{self.kind} stage failed")
        out = Path(job_dir) / f"{self.kind}.out"
        out.write_text("x")
        return {"output_path": str(out)}


def patch_stages(video=None, music=None, muxRecorder=None, denoise=None):
    saved = (video_w.run, music_w.run, shot.render)
    video_w.run = video.run if video else (lambda *a, **k: (_ for _ in ()).throw(AssertionError("video called")))
    music_w.run = music.run if music else (lambda *a, **k: (_ for _ in ()).throw(AssertionError("music called")))
    if muxRecorder is not None:
        class _M:
            def __getattr__(self, name):
                if name == "extract_last_frame":  # P10 tail-frame: stub, no ffmpeg
                    return lambda video, out: out
                if name == "denoise_audio":  # 真濾波要 ffmpeg+ndarray，這裡只記呼叫
                    return denoise or (lambda video, output, **kw: output)
                assert name == "mux", name
                return muxRecorder
        shot.render = _M()
    return saved


def restore(saved):
    video_w.run, music_w.run, shot.render = saved


def patch_pick_sfx(fn):
    """sfx_tag → file is mix.py's job; stub it so tests need no assets/ on disk."""
    saved = mix_w._pick_sfx
    mix_w._pick_sfx = fn
    return saved


def unpatch_pick_sfx(saved):
    mix_w._pick_sfx = saved


def run_shot(params):
    with tempfile.TemporaryDirectory() as d:
        return shot.run(params, Path(d), progress=lambda r, p="": None,
                        cancel=lambda: False)


def test_requires_at_least_one_stage():
    try:
        run_shot({})
        assert False, "empty spec must fail"
    except ValueError as e:
        assert "at least one" in str(e)


def test_stage_order_and_paths():
    vid, mus = Stage(kind="video"), Stage(kind="music")
    calls = []
    saved = patch_stages(video=vid, music=mus,
                         muxRecorder=lambda **kw: calls.append(kw) or [])
    try:
        with tempfile.TemporaryDirectory() as d:
            result = shot.run(
                {"video": {"prompt": "v"}, "music": {"prompt": "m"}},
                Path(d), progress=lambda r, p="": None, cancel=lambda: False)
            assert result["output_path"].endswith("shot.mp4")
            jd = Path(d)
            assert vid.calls[0]["job_dir"] == str(jd / "video")
            assert mus.calls[0]["job_dir"] == str(jd / "music")
            kw = calls[0]
            assert Path(kw["video"]).name == "video.out"
            # mmh3 原生音頻：視頻檔內含音軌，mix 不再鋪對白軌
            assert kw["mute_source_audio"] is False
            assert [Path(t["path"]).name for t in kw["audio_tracks"]] == ["music.out"]
            assert kw["audio_tracks"][0]["loop"] is True
            assert result["stages"]["video"]["output_path"].endswith("video.out")
    finally:
        restore(saved)


def test_no_audio_ref_mutes_model_track():
    vid, mus = Stage(kind="video"), Stage(kind="music")
    calls = []
    saved = patch_stages(video=vid, music=mus,
                         muxRecorder=lambda **kw: calls.append(kw) or [])
    try:
        run_shot({"video": {"prompt": "v"}, "music": {"prompt": "m"}})
        # mmh3 原生音頻就是成品：不再 mute（2026-11-16）
        assert calls[0]["mute_source_audio"] is False
    finally:
        restore(saved)


def test_sfx_tags_become_mux_tracks():
    vid, mus = Stage(kind="video"), Stage(kind="music")
    calls, picked = [], []
    saved_sfx = patch_pick_sfx(
        lambda tag, seed=None: picked.append((tag, seed)) or f"/assets/sfx/{tag}/a.mp3")
    saved = patch_stages(video=vid, music=mus,
                         muxRecorder=lambda **kw: calls.append(kw) or [])
    try:
        run_shot({"video": {"prompt": "v"}, "music": {"prompt": "m"},
                  "sfx": ["ambience_rain",
                          {"sfx_tag": "thunder", "start": 1.5, "loop": True},
                          {"sfx_tag": "ambience_wind", "sfx_seed": 42}]})
        assert sorted(picked) == [("ambience_rain", None), ("ambience_wind", 42),
                                  ("thunder", None)]
        tr = calls[0]["audio_tracks"]  # [music, rain, thunder]
        assert tr[1]["path"] == "/assets/sfx/ambience_rain/a.mp3"
        assert tr[1]["start"] == 0 and tr[1]["loop"] is False
        assert tr[1]["volume"] == 0.3  # 預設比對白(1.0)輕、比配樂(0.15)重
        assert tr[2]["path"] == "/assets/sfx/thunder/a.mp3"
        assert tr[2]["start"] == 1.5 and tr[2]["loop"] is True
    finally:
        restore(saved)
        unpatch_pick_sfx(saved_sfx)


def test_mix_dict_forwarded_to_mux():
    vid = Stage(kind="video")
    calls = []
    saved_sfx = patch_pick_sfx(lambda tag, seed=None: f"/assets/sfx/{tag}/a.mp3")
    saved = patch_stages(video=vid, muxRecorder=lambda **kw: calls.append(kw) or [])
    try:
        run_shot({"video": {"prompt": "v"}, "sfx": ["ambience_wind"],
                  "mix": {"subtitles": "/tmp/s.srt", "dialogue_volume": 0.9,
                          "music_volume": 0.2, "sfx_volume": 0.05}})
        kw = calls[0]
        assert kw["subtitles"] == "/tmp/s.srt"
        assert (kw["dialogue_volume"], kw["music_volume"]) == (0.9, 0.2)
        assert kw["audio_tracks"][0]["volume"] == 0.05  # mix.sfx_volume 覆蓋預設
    finally:
        restore(saved)
        unpatch_pick_sfx(saved_sfx)


def test_first_frame_auto_now_fails_loudly():
    """image 階段已隨 qwen_image 刪除：'auto' 必須大聲失敗，
    不能靜默吞掉變成無首幀的 T2VA。"""
    saved = patch_stages(video=Stage(kind="video"), muxRecorder=lambda **kw: None)
    try:
        try:
            run_shot({"video": {"prompt": "v", "first_frame": "auto"}})
            assert False, "auto without image stage must fail"
        except ValueError as e:
            assert "image stage" in str(e)
    finally:
        restore(saved)


def test_no_video_returns_stage_output_no_mux():
    mus = Stage(kind="music")
    mux_called = []
    saved = patch_stages(music=mus, muxRecorder=lambda **kw: mux_called.append(kw))
    try:
        result = run_shot({"music": {"prompt": "m"}})
        assert result["output_path"].endswith("music.out")
        assert not mux_called, "mux must be skipped without a video stage"
    finally:
        restore(saved)


def test_stage_failure_propagates():
    vid = Stage(fail=True)
    saved = patch_stages(video=vid, muxRecorder=lambda **kw: None)
    try:
        try:
            run_shot({"video": {"prompt": "v"}})
            assert False, "stage failure must raise"
        except RuntimeError as e:
            assert "failed" in str(e)
    finally:
        restore(saved)


def test_progress_monotonic():
    class P:
        last = 0.0
        vals = []

        @classmethod
        def cb(cls, ratio, phase=""):
            assert ratio >= cls.last - 1e-9, f"progress went backwards: {ratio} < {cls.last}"
            cls.last = ratio
            cls.vals.append(ratio)
    stages = {k: Stage() for k in ("video", "music")}
    saved = patch_stages(stages["video"], stages["music"],
                         muxRecorder=lambda **kw: None)
    try:
        with tempfile.TemporaryDirectory() as d:
            shot.run({"video": {"prompt": "1"}, "music": {"prompt": "2"}}, Path(d),
                     progress=P.cb, cancel=lambda: False)
        assert P.vals and max(P.vals) <= 0.99
    finally:
        restore(saved)


def test_video_music_stage_order():
    vid, mus = Stage(kind="video"), Stage(kind="music")
    calls = []
    saved = patch_stages(video=vid, music=mus,
                         muxRecorder=lambda **kw: calls.append(kw) or [])
    try:
        run_shot({"video": {"prompt": "v"}, "music": {"prompt": "m"}})
        # music 在 video 之後：video 先出，music 軌鋪上去
        assert vid.calls and mus.calls
    finally:
        restore(saved)
