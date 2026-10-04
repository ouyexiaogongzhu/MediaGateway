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

from server.workers import qwen_image as image_w  # noqa: E402（iris 退役改接 Qwen-Image-2.1）
from server.workers import mix as mix_w  # noqa: E402
from server.workers import music as music_w  # noqa: E402
from server.workers import shot  # noqa: E402
from server.workers import video as video_w  # noqa: E402
from server.workers import voice as voice_w  # noqa: E402


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


def patch_stages(image=None, video=None, voice=None, music=None, muxRecorder=None,
                 denoise=None):
    saved = (image_w.run, video_w.run, voice_w.run, music_w.run, shot.render)
    image_w.run = image.run if image else (lambda *a, **k: (_ for _ in ()).throw(AssertionError("image called")))
    video_w.run = video.run if video else (lambda *a, **k: (_ for _ in ()).throw(AssertionError("video called")))
    voice_w.run = voice.run if voice else (lambda *a, **k: (_ for _ in ()).throw(AssertionError("voice called")))
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
    image_w.run, video_w.run, voice_w.run, music_w.run, shot.render = saved


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
    img, vid, voi, mus = (Stage(kind=k) for k in ("image", "video", "voice", "music"))
    calls = []
    saved = patch_stages(img, vid, voi, mus, muxRecorder=lambda **kw: calls.append(kw) or [])
    try:
        with tempfile.TemporaryDirectory() as d:
            result = shot.run(
                {"image": {"prompt": "p"}, "video": {"prompt": "v"},
                 "voice": {"text": "t"}, "music": {"prompt": "m"}},
                Path(d), progress=lambda r, p="": None, cancel=lambda: False)
            assert result["output_path"].endswith("shot.mp4")
            jd = Path(d)
            assert img.calls[0]["job_dir"] == str(jd / "image")
            # Ref2VA 顺序:voice 在 video 之前完成,video 收到 image+audio refs
            # (h3.c:633 音訊參考必須配視覺參考；順序為視覺在前)
            assert vid.calls[0]["params"]["refs"] == [
                {"kind": "image", "path": f"{jd}/image/image.out"},
                {"kind": "audio", "path": f"{jd}/voice/voice.out"}]
            assert vid.calls[0]["job_dir"] == str(jd / "video")
            assert voi.calls[0]["job_dir"] == str(jd / "voice")
            assert mus.calls[0]["job_dir"] == str(jd / "music")
            kw = calls[0]
            assert Path(kw["video"]).name == "video.out"
            # voice 餵了 ref_audio → 對白由 h3 自渲染，只鋪配樂（見 test_audio_ref_*）
            assert [Path(t["path"]).name for t in kw["audio_tracks"]] == ["music.out"]
            assert kw["audio_tracks"][0]["loop"] is True
            assert kw["mute_source_audio"] is False
            assert result["stages"]["image"]["output_path"].endswith("image.out")
    finally:
        restore(saved)


def test_audio_ref_keeps_model_track():
    vid, voi, mus, img = (Stage(kind=k) for k in ("video", "voice", "music", "image"))
    calls = []
    saved = patch_stages(image=img, video=vid, voice=voi, music=mus,
                         muxRecorder=lambda **kw: calls.append(kw) or [])
    try:
        run_shot({"image": {"prompt": "i"}, "video": {"prompt": "v"},
                  "voice": {"text": "t"}, "music": {"prompt": "m"}})
        assert vid.calls[0]["params"]["refs"][-1]["kind"] == "audio"
        assert calls[0]["mute_source_audio"] is False
        # 兩個音色念同一句＝使用者棄用的 ttsmux 變體 → TTS wav 只當 ref 不進混音
        assert [Path(t["path"]).name for t in calls[0]["audio_tracks"]] == ["music.out"]
    finally:
        restore(saved)


def test_no_audio_ref_mutes_model_track():
    vid, mus = Stage(kind="video"), Stage(kind="music")
    calls = []
    saved = patch_stages(video=vid, music=mus,
                         muxRecorder=lambda **kw: calls.append(kw) or [])
    try:
        run_shot({"video": {"prompt": "v"}, "music": {"prompt": "m"}})
        assert calls[0]["mute_source_audio"] is True
    finally:
        restore(saved)


def test_denoise_default_on_when_h3_track_survives():
    """沒有 dialogue ref、也沒有其他 track → h3 自己的音軌進成品（實測 −60.0dB
    嘶聲），這是 Wiener 收益 7.5–13.7dB 唯一成立的情境 → 預設降噪。"""
    vid = Stage(kind="video")
    calls, denoised = [], []
    saved = patch_stages(
        video=vid, muxRecorder=lambda **kw: calls.append(kw) or [],
        denoise=lambda v, out, **kw: denoised.append((v, out)) or out)
    try:
        run_shot({"video": {"prompt": "v"}})
        assert denoised and denoised[0][0].endswith("video.out"), denoised
        got = Path(calls[0]["video"])
        assert got.name == "video_denoised.mp4"
        assert got.parent.name == "denoise"  # job_dir/<stage> 慣例，別撞 shot.mp4
    finally:
        restore(saved)


def test_denoise_opt_out_keeps_raw_track():
    vid = Stage(kind="video")
    calls, denoised = [], []
    saved = patch_stages(
        video=vid, muxRecorder=lambda **kw: calls.append(kw) or [],
        denoise=lambda v, out, **kw: denoised.append((v, out)) or out)
    try:
        run_shot({"video": {"prompt": "v"}, "mix": {"denoise": False}})
        assert not denoised, "mix.denoise=False must skip the filter"
        assert Path(calls[0]["video"]).name == "video.out"
    finally:
        restore(saved)


def test_denoise_skipped_with_dialogue_ref():
    """TTS wav 餵過 ref_audio → h3 自渲染的台詞已是 TTS 原檔（−86.5 vs −87.1dB），
    濾波器量到語音 0.0dB：跑它只是白重編一次。"""
    vid, voi, img = (Stage(kind=k) for k in ("video", "voice", "image"))
    calls, denoised = [], []
    saved = patch_stages(
        image=img, video=vid, voice=voi, muxRecorder=lambda **kw: calls.append(kw) or [],
        denoise=lambda v, out, **kw: denoised.append((v, out)) or out)
    try:
        run_shot({"image": {"prompt": "i"}, "video": {"prompt": "v"},
                  "voice": {"text": "t"}})
        assert calls[0]["mute_source_audio"] is False  # 這條路徑 h3 音軌會進成品
        assert not denoised, "Ref2VA track is already clean — must not filter"
        assert Path(calls[0]["video"]).name == "video.out"
    finally:
        restore(saved)


def test_denoise_skipped_when_other_track_mutes_it():
    """沒有 ref 但有配樂 → h3 音軌被 mute_source_audio 丟掉，降噪輸出沒人會聽。"""
    vid, mus = Stage(kind="video"), Stage(kind="music")
    calls, denoised = [], []
    saved = patch_stages(
        video=vid, music=mus, muxRecorder=lambda **kw: calls.append(kw) or [],
        denoise=lambda v, out, **kw: denoised.append((v, out)) or out)
    try:
        run_shot({"video": {"prompt": "v"}, "music": {"prompt": "m"}})
        assert calls[0]["mute_source_audio"] is True
        assert not denoised, "denoised track would be muted anyway"
    finally:
        restore(saved)


def test_denoise_failure_falls_back_to_raw_track():
    """降噪是加分不是必要條件：ffmpeg 抽風不能燒掉剛燒完 GPU 的鏡頭，但必須留警告。"""
    vid = Stage(kind="video")
    calls = []

    def boom(video, output, **kw):
        raise RuntimeError("ffmpeg hiccup")

    saved = patch_stages(video=vid, muxRecorder=lambda **kw: calls.append(kw) or [],
                         denoise=boom)
    try:
        with warnings.catch_warnings(record=True) as got:
            warnings.simplefilter("always")
            run_shot({"video": {"prompt": "v"}})
        assert len(calls) == 1, "mux must still run on the raw track"
        assert Path(calls[0]["video"]).name == "video.out"
        assert any("denoise" in str(w.message) for w in got), "no silent degradation"
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


def test_first_frame_auto_wiring():
    img, vid = Stage(kind="image"), Stage(kind="video")
    saved = patch_stages(image=img, video=vid,
                         muxRecorder=lambda **kw: None)
    try:
        run_shot({"image": {"prompt": "p"},
                  "video": {"prompt": "v", "first_frame": "auto"}})
        ff = vid.calls[0]["params"]["first_frame"]
        assert ff.endswith("image.out"), ff
    finally:
        restore(saved)


def test_first_frame_auto_without_image_fails():
    vid = Stage()
    saved = patch_stages(video=vid, muxRecorder=lambda **kw: None)
    try:
        try:
            run_shot({"video": {"prompt": "v", "first_frame": "auto"}})
            assert False, "auto without image stage must fail"
        except ValueError as e:
            assert "auto" in str(e)
    finally:
        restore(saved)


def test_no_video_returns_stage_output_no_mux():
    img = Stage(kind="image")
    mux_called = []
    saved = patch_stages(image=img, muxRecorder=lambda **kw: mux_called.append(kw))
    try:
        result = run_shot({"image": {"prompt": "p"}})
        assert result["output_path"].endswith("image.out")
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
    stages = {k: Stage() for k in ("image", "video", "voice")}
    saved = patch_stages(stages["image"], stages["video"], stages["voice"],
                         muxRecorder=lambda **kw: None)
    try:
        with tempfile.TemporaryDirectory() as d:
            shot.run({"image": {"prompt": "1"}, "video": {"prompt": "2"},
                      "voice": {"text": "3"}}, Path(d),
                     progress=P.cb, cancel=lambda: False)
        assert P.vals and max(P.vals) <= 0.99
    finally:
        restore(saved)


def test_voice_ref_supplies_visual_reference():
    """h3.c:633 拒絕「只有音訊參考」的請求。呼叫端沒給 first_frame 時，
    shot 必須拿 image 階段的產物補上，否則整個 job 被 h3 擋掉。"""
    vid, voi, img = (Stage(kind=k) for k in ("video", "voice", "image"))
    saved = patch_stages(image=img, video=vid, voice=voi,
                         muxRecorder=lambda **kw: None)
    try:
        run_shot({"image": {"prompt": "i"}, "video": {"prompt": "v"},
                  "voice": {"text": "t"}})
        p = vid.calls[0]["params"]
        # 圖要當參考圖（refs），不能當 first_frame —— h3 不接受 frame anchor
        # 與 full reference 並存
        assert "first_frame" not in p, p
        kinds = [r["kind"] for r in p["refs"]]
        assert kinds == ["image", "audio"], p
        assert p["refs"][0]["path"].endswith("image.out"), p
    finally:
        restore(saved)


def test_voice_ref_without_any_visual_fails_loudly():
    """沒有 image 階段也沒 first_frame → 早失敗並講清楚原因，
    不要燒完 TTS 與生圖才換一句 h3 的天書。"""
    vid, voi = Stage(kind="video"), Stage(kind="voice")
    saved = patch_stages(video=vid, voice=voi, muxRecorder=lambda **kw: None)
    try:
        run_shot({"video": {"prompt": "v"}, "voice": {"text": "t"}})
    except ValueError as e:
        assert "visual reference" in str(e), e
    else:
        raise AssertionError("expected a loud failure")
    finally:
        restore(saved)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"PASS {fn.__name__}")
    print(f"\n{len(fns)} tests passed")
