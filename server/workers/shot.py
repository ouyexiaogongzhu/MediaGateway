"""Shot composite worker: video → music → ffmpeg mux.

Orchestrates the other workers in-process, sequentially, inside one job.
The scheduler sees MEM_GB = max stage (video, 20GB) so a shot render holds
the GPU exclusively for its whole duration — stages never overlap.

params (every stage optional, but at least one required):
    video: {prompt, seconds?, steps?, seed?, first_frame?, last_frame?, refs?[]}
           # mmh3 原生音頻：<d voice="…"> 直接驅動畫內對白/音效，不再需要 TTS 語音階段
    music: {prompt, duration_s?, seed?, lyrics?}
    sfx:   ["ambience_rain"] | [{sfx_tag|"path", start?, loop?, volume?}]
           # 擬音不是階段：sfx_tag 由 mix._pick_sfx 解析成一個檔案，只在 mux 段生效
    mix:   {subtitles?: path, dialogue_volume?: 1.0, music_volume?: 0.15,
            sfx_volume?: 0.3, denoise?=True, timeout?: 600}
Final output: job_dir/shot.mp4 (skipped if no video stage).
"""
from __future__ import annotations

import re
from pathlib import Path

from .. import render
from . import mix as mix_w  # 擬音：複用 mix._pick_sfx 的 sfx_tag 解析器
from . import music as music_w
from . import mmh3 as video_w

TYPE = "shot"
MEM_GB = video_w.MEM_GB  # peak stage

_STAGE_WEIGHTS = {"video": 4, "music": 1}




def run(params: dict, job_dir: Path, progress, cancel) -> dict:
    params = dict(params)
    # 對白/環境音由 mmh3 原生音頻頭生成（prompt <d> 標籤），無 TTS 語音階段
    enabled = [s for s in ("video", "music") if params.get(s)]
    if not enabled:
        raise ValueError("at least one of image/video/voice/music is required")
    mix = params.get("mix") or {}
    # 擬音解析提前做：sfx_tag 只是 random.choice 一個檔案，不值得當階段
    # （_STAGE_WEIGHTS 也不必動），但錯 tag 要在燒掉 40 分鐘 GPU 之前就爆
    sfx_tracks = []
    for t in (params.get("sfx") or []):
        t = {"sfx_tag": t} if isinstance(t, str) else dict(t)
        if not (t.get("path") or t.get("sfx_tag")):
            raise ValueError("sfx entry needs path or sfx_tag")
        sfx_tracks.append({
            "path": t.get("path") or mix_w._pick_sfx(t["sfx_tag"], t.get("sfx_seed")),
            "start": float(t.get("start", 0)),
            "loop": bool(t.get("loop")),
            "volume": float(t.get("volume", mix.get("sfx_volume", 0.3)))})
    total_w = sum(_STAGE_WEIGHTS[s] for s in enabled)
    done_w = 0.0
    meta: dict = {}

    def stage_cb(name):
        def cb(ratio, phase=""):
            ratio = ratio if isinstance(ratio, (int, float)) else 0.0
            p = (done_w + _STAGE_WEIGHTS[name] * min(ratio, 1.0)) / total_w
            progress(min(p, 0.99), f"{name}:{phase}".strip(":"))
        return cb

    outputs: dict = {}
    video_audio_ref = False
    for stage in enabled:
        if cancel():
            raise Exception("cancelled")
        spec = dict(params[stage])
        out_dir = job_dir / stage
        out_dir.mkdir(parents=True, exist_ok=True)

        if stage == "video" and spec.get("first_frame") == "auto":
            raise ValueError(
                "video.first_frame='auto' needs the image stage, which was "
                "removed with qwen_image; pass an explicit first_frame path")

        module = {"video": video_w, "music": music_w}[stage]
        outputs[stage] = module.run(spec, out_dir, stage_cb(stage), cancel)
        meta[stage] = outputs[stage]
        done_w += _STAGE_WEIGHTS[stage]
        progress(min(done_w / total_w, 0.99), stage)

    vid = outputs.get("video")
    if not vid:
        first = enabled[0]
        return {"output_path": outputs[first]["output_path"], "stages": meta,
                "note": "no video stage — returning first stage output"}

    tracks = []
    # h3 生成的音軌一律丟棄，成片只鋪 TTS 原聲（2026-10-04 使用者定案）。
    # 但 ref_audio 仍然要餵：它不是為了拿聲音，是為了讓 h3 知道台詞、嘴型對齊。
    # 實測對照（512×288 / 4.46s / 同一支 prompt）：
    #   帶 ref-audio → h3 音軌底噪 −75.8dB、時間對齊 r=0.999；無 → −60.0dB。
    # 音軌本身不用，只留它的「表演」。
    if "music" in outputs:  # bed track → mux's music_volume default, loops to video length
        tracks.append({"path": outputs["music"]["output_path"], "loop": True})
    tracks += sfx_tracks  # 擬音鋪最後，音量走 mix.sfx_volume（預設比對白輕）

    # 這裡曾經對 h3 的音軌做 Wiener 降噪（power-domain，實測降 7.5–13.7dB）。
    final = str(job_dir / "shot.mp4")
    render.mux(
        video=vid["output_path"], audio_tracks=tracks, output=final,
        # mmh3 原生音頻（對白/環境音/畫內樂）就是成品，保留（2026-11-16）。
        mute_source_audio=False,
        subtitles=mix.get("subtitles"),
        dialogue_volume=float(mix.get("dialogue_volume", 1.0)),
        music_volume=float(mix.get("music_volume", 0.15)),
        timeout=float(mix.get("timeout", 600)))
    last_frame = render.extract_last_frame(final, str(job_dir / "last_frame.png"))
    return {"output_path": final, "last_frame_path": last_frame, "stages": meta}
