"""Shot composite worker: image → video → voice → music → ffmpeg mux.

Orchestrates the other workers in-process, sequentially, inside one job.
The scheduler sees MEM_GB = max stage (video, 35GB) so a shot render holds
the GPU exclusively for its whole duration — stages never overlap.

params (every stage optional, but at least one required):
    image: {prompt, width?, height?, steps?, seed?, input?}
    video: {prompt, seconds?, steps?, seed?, first_frame?="auto"|path,
            last_frame?, refs?[], ...}   # "auto" = use generated image
    voice: {text, voice|speaker, speed?}  # 省略時由 video.prompt 的 <d voice=…> 補
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
from . import qwen_image as image_w  # iris 退役後改接 Qwen-Image-2.1（參數同構：prompt/width/height）
from . import mix as mix_w  # 擬音：複用 mix._pick_sfx 的 sfx_tag 解析器
from . import music as music_w
from . import video as video_w
from . import voice as voice_w

TYPE = "shot"
MEM_GB = video_w.MEM_GB  # peak stage

_STAGE_WEIGHTS = {"image": 1, "video": 4, "voice": 1, "music": 1}


# prompt 內的對白：<d voice="vivian">[Chinese] 台詞</d>
# 為什麼走 prompt 而不是資料庫：原本的音色來自 character_voice_bindings，而那條鏈要
# asset_versions → binding → shot_asset_references 三張表都通；實測三張全空，所以
# 對白從來沒有真的走過 TTS。prompt 早就在傳了（h3 本來就讀 <d>），從裡面取出台詞
# 與 speaker 可以完全繞開那條斷鏈。voice= 可省略，省略時用 tts_qwen 的預設音色。
_DIALOGUE_RE = re.compile(
    r"<d\s*(?:voice=[\"']?([^\"'>\s]+)[\"']?)?\s*>(.*?)</d>", re.S | re.I)
_LANG_TAG_RE = re.compile(r"^\s*\[[A-Za-z-]+\]\s*")


def dialogue_from_prompt(prompt: str) -> dict | None:
    """從 prompt 抽出台詞 → TTS 參數。沒有 <d> 回 None。

    回傳 {"text": ..., "voice": ...}；voice 可能是 None（交給 worker 預設）。
    多段 <d> 依序串接 —— 那是連續發話，h3 也照順序念。
    """
    if not prompt or "<d" not in prompt.lower():
        return None
    texts, voice = [], None
    for raw_voice, body in _DIALOGUE_RE.findall(prompt):
        line = _LANG_TAG_RE.sub("", body).strip()
        if line:
            texts.append(line)
        if voice is None and raw_voice:
            voice = raw_voice.strip()
    if not texts:
        return None
    spec = {"text": " ".join(texts)}
    if voice:
        spec["voice"] = voice
    return spec


def run(params: dict, job_dir: Path, progress, cancel) -> dict:
    params = dict(params)
    # prompt 帶 <d> 就自動補一個 voice 階段（呼叫端沒顯式給 voice 時）。
    if not params.get("voice") and params.get("video"):
        spec = dialogue_from_prompt((params["video"] or {}).get("prompt") or "")
        if spec:
            params["voice"] = spec
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
    # h3 生成的音軌一律丟棄，成片只鋪 TTS 原聲（2026-10-04 使用者定案）。
    # 但 ref_audio 仍然要餵：它不是為了拿聲音，是為了讓 h3 知道台詞、嘴型對齊。
    # 實測對照（512×288 / 4.46s / 同一支 prompt）：
    #   帶 ref-audio → h3 音軌底噪 −75.8dB、時間對齊 r=0.999；無 → −60.0dB。
    # 音軌本身不用，只留它的「表演」。
    if "voice" in outputs:
        tracks.append({"path": outputs["voice"]["output_path"], "start": 0})
    if "music" in outputs:  # bed track → mux's music_volume default, loops to video length
        tracks.append({"path": outputs["music"]["output_path"], "loop": True})
    tracks += sfx_tracks  # 擬音鋪最後，音量走 mix.sfx_volume（預設比對白輕）

    # 這裡曾經對 h3 的音軌做 Wiener 降噪（power-domain，實測降 7.5–13.7dB）。
    # h3 音軌改成一律丟棄之後它就是死碼了：render.mux 在 mute_source_audio=True
    # 時用 anullsrc 當混音底，被降噪的那條軌根本不會進成品。函式本身留在
    # render.py（仍有測試），只是不再由 shot worker 呼叫。

    final = str(job_dir / "shot.mp4")
    render.mux(
        video=vid["output_path"], audio_tracks=tracks, output=final,
        # 一律丟棄 h3 自渲染的音軌（使用者 2026-10-04 定案：只用 TTS 的聲音）。
        # 注意這跟 video_audio_ref 無關 —— ref_audio 仍然餵，嘴型要對齊。
        mute_source_audio=True,
        subtitles=mix.get("subtitles"),
        dialogue_volume=float(mix.get("dialogue_volume", 1.0)),
        music_volume=float(mix.get("music_volume", 0.15)),
        timeout=float(mix.get("timeout", 600)))
    last_frame = render.extract_last_frame(final, str(job_dir / "last_frame.png"))
    return {"output_path": final, "last_frame_path": last_frame, "stages": meta}
