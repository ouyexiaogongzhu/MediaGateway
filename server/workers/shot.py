"""Shot composite worker: image → video → voice → music → ffmpeg mux.

Orchestrates the other workers in-process, sequentially, inside one job.
The scheduler sees MEM_GB = max stage (video, 35GB) so a shot render holds
the GPU exclusively for its whole duration — stages never overlap.

params (every stage optional, but at least one required):
    image: {prompt, width?, height?, steps?, seed?, input?}
    video: {prompt, seconds?, steps?, seed?, first_frame?="auto"|path,
            last_frame?, refs?[], ...}   # "auto" = use generated image
    voice: {text, voice|prompt_wav+prompt_text, speed?}
    music: {prompt, duration_s?, seed?, lyrics?}
    sfx:   ["ambience_rain"] | [{sfx_tag|"path", start?, loop?, volume?}]
           # 擬音不是階段：sfx_tag 由 mix._pick_sfx 解析成一個檔案，只在 mux 段生效
    mix:   {subtitles?: path, dialogue_volume?: 1.0, music_volume?: 0.15,
            sfx_volume?: 0.3, denoise?=True, timeout?: 600}
Final output: job_dir/shot.mp4 (skipped if no video stage).
"""
from __future__ import annotations

import warnings
from pathlib import Path

from .. import render
from . import qwen_image as image_w  # iris 退役後改接 Qwen-Image-2.1（參數同構：prompt/width/height）
from . import mix as mix_w  # 擬音：複用 mix._pick_sfx 的 sfx_tag 解析器
from . import music as music_w
from . import video as video_w
from . import voice as voice_w

TYPE = "shot"
MEM_GB = video_w.MEM_GB  # peak stage

_STAGE_WEIGHTS = {"image": 1, "video": 4, "voice": 1, "music": 1}


def run(params: dict, job_dir: Path, progress, cancel) -> dict:
    enabled = [s for s in ("image", "video", "voice", "music") if params.get(s)]
    if not enabled:
        raise ValueError("at least one of image/video/voice/music is required")
    # Ref2VA：有台词时 TTS 必须先于视频跑——wav 作为 ref_audio 驱动 h3 口型，
    # h3 随即自渲染一条干净音轨（见下面 mute_source_audio）
    if "voice" in enabled and "video" in enabled:
        enabled.remove("voice")
        enabled.insert(enabled.index("video"), "voice")
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
            img = outputs.get("image")
            if not img:
                raise ValueError("video.first_frame='auto' needs the image stage")
            spec["first_frame"] = img["output_path"]
        if stage == "video" and "voice" in outputs:
            # 台词 wav 进 refs 驱动口型；顺带归一化影策传来的字符串 refs
            refs = [{"kind": "image", "path": r} if isinstance(r, str) else r
                    for r in (spec.get("refs") or [])]
            # h3.c:633 硬性要求：任何音訊參考都必須配 image 或 video 參考，否則
            # 整個 job 被拒。但 first_frame/last_frame 是 frame anchor，跟
            # full reference 互斥（h3 會報 "full references cannot be combined
            # with frame anchors"）。所以缺視覺參考時把圖當**參考圖**塞進 refs，
            # 不是當首幀——這也是 Ref2VA 拿到角色外觀的正規途徑。
            if not any(r.get("kind") in ("image", "video") for r in refs):
                if spec.get("first_frame"):
                    refs.insert(0, {"kind": "image", "path": spec.pop("first_frame")})
                elif not spec.get("last_frame"):
                    img = outputs.get("image")
                    if not img:
                        raise ValueError(
                            "voice stage needs a visual reference: h3 rejects "
                            "audio refs without one. Add refs, first_frame, "
                            "or an image stage.")
                    refs.insert(0, {"kind": "image", "path": img["output_path"]})
            refs.append({"kind": "audio", "path": outputs["voice"]["output_path"]})
            spec["refs"] = refs
        if stage == "video":
            video_audio_ref = any(isinstance(r, dict) and r.get("kind") == "audio"
                                  for r in (spec.get("refs") or []))

        module = {"image": image_w, "video": video_w,
                  "voice": voice_w, "music": music_w}[stage]
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
    # TTS wav 有兩種去處：餵給 h3 當 ref_audio（此時 h3 自渲染的音軌就是台詞，
    # 實測底噪 −86.5dB / 時間對齊 r=0.912），或直接鋪進混音。兩者只能選一條——
    # 同時鋪會是兩個音色念同一句（使用者判「不符合要求」已棄用的 ttsmux 變體）。
    if "voice" in outputs and not video_audio_ref:
        tracks.append({"path": outputs["voice"]["output_path"], "start": 0})
    if "music" in outputs:  # bed track → mux's music_volume default, loops to video length
        tracks.append({"path": outputs["music"]["output_path"], "loop": True})
    tracks += sfx_tracks  # 擬音鋪最後，音量走 mix.sfx_volume（預設比對白輕）

    # 降噪（power-domain Wiener：實測底噪降 6–16dB、語音 0.0–1.3dB）只補一個洞。
    # 舊註解的「7.5–13.7dB、語音 0.0dB」是相對門檻量出來的假數字——絕對門檻
    # (−55dBFS, hiss.py) 重測原實作其實語音掉 0.5–4.0dB，門檻已修（見 render.py）。
    # h3 自渲染的音軌只有「沒被 dialogue ref 洗成 TTS 原檔（實測 −86.5dB）」且
    # 「沒被 tracks 墊掉（mute_source_audio）」時才會進成品，那條路徑底噪 −60.0dB。
    # 其餘兩條要嘛已經乾淨、要嘛根本不進成品——跑降噪只是白重編一次 libx264。
    video_in = vid["output_path"]
    if mix.get("denoise", True) and not video_audio_ref and not tracks:
        den_dir = job_dir / "denoise"
        den_dir.mkdir(parents=True, exist_ok=True)
        clean = str(den_dir / "video_denoised.mp4")
        try:
            render.denoise_audio(video_in, clean, timeout=float(mix.get("timeout", 600)))
            video_in = clean
        except Exception as e:
            # 降噪是加分不是必要條件：ffmpeg 抽風不該燒掉剛燒完 GPU 的整顆鏡頭。
            # 退回原軌（嘶聲回來但影片還在），偏差寫 stderr 而不是讓整個 job 死掉。
            warnings.warn(f"denoise_audio failed ({e}); shipping un-denoised track",
                          stacklevel=2)

    final = str(job_dir / "shot.mp4")
    render.mux(
        video=video_in, audio_tracks=tracks, output=final,
        # refs 帶 audio 時 h3 自渲染的音軌是乾淨的（實測 −86.5dB vs 無 ref 的
        # −60.0dB）→ 保留；舊註解「h3 不再出音轨」前提已過期，照舊會丟掉 Ref2VA 的成果
        mute_source_audio=bool(tracks) and not video_audio_ref,
        subtitles=mix.get("subtitles"),
        dialogue_volume=float(mix.get("dialogue_volume", 1.0)),
        music_volume=float(mix.get("music_volume", 0.15)),
        timeout=float(mix.get("timeout", 600)))
    last_frame = render.extract_last_frame(final, str(job_dir / "last_frame.png"))
    return {"output_path": final, "last_frame_path": last_frame, "stages": meta}
