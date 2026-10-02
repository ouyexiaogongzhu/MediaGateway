"""生圖 worker — 兩條蒸餾引擎跑 stable-diffusion.cpp（Metal, GGUF）。

生產檔，2026-10-02 實測，M5 Pro 48GB，牆鐘（含模型載入）：
  Viggle v0.3 6 步 + dbcache   1024² 63s / 1024x576 38s / 512x288 17s
                              主力，中文文字最好、構圖全能
                              ⚠️ 這三個數字是**帶 dbcache** 量到的。生產現已改 nocache
                                 （512² 鬼影，見下面 DBCACHE），純 nocache 牆鐘未重測，
                                 別把這列當現況往外報
  Krea2 v1.1 8 步 + --tae     1024² 111s（中文）/ 111s（多人）/ 101s（NSFW）
                              第二引擎，西方寫實質感/直白構圖
                              （4 步是蒸餾設計點，實測 61/68/63s；現依 2026-10-02
                               決策走 8 步，代價 +60~82% 牆鐘）
  （舊的 qwen_image_2.1 base 20 步 cfg6 = 431s，已退役刪除）

⚠️ 512 以下再降解析度省不到時間：固定成本約 9s 主導（512² 15s vs 512x288 17s）。
⚠️ 1080×1920 別生，走 FlashVSR。短劇解析度族：草稿 512x288 → 正片 1024x576。
⚠️ 併發跑兩個 sd-cli 是負報酬（1024² 0.97x、512² 0.81x），瓶頸是算力不是記憶體——
   底下的 _run_lock + MEM_GB 是對的，別改成併發。細節見 docs/tools.md §13-15。

兩條都是 DMD 蒸餾，cfg 固定 1.0，步數與 sigma 都是燒錄進去不可調的配對。
Install: ~/tool/sd.cpp（build/bin/sd-cli）+ models/{diffusion_models,vae,text_encoders}。
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

from ._util import number, run_cli, seed_of

TYPE = "qwen_image"
# 实测 sd-cli 峰值 RSS 22-27GB（8 refs 编辑上下文），不是 10——按 10 算账时
# BUDGET_GB=40 会一次放行 3 个 job，全部堵在 _run_lock 上，started_at 又在拿到
# 锁之前写入，锁等待全被记成工时（影策外层超时照跳，「超预算 4 倍」假象）。
# 同一笔错账也曾放 3 个 sd-cli 同时进 48GB → 记忆体死锁。26 保证同时只跑一个。
# ⚠️ 26 是照 qwen 系（8B LLM + qwen VAE）定的。Krea2 换成 4B LLM + wan VAE，
#    峰值只会更低；但 Krea2 目前不开 -r（见 ENGINES），所以这个数没被重新测过。
MEM_GB = 26.0

DEFAULT_HOME = os.environ.get("SDCPP_HOME", "/Users/vincent/tool/sd.cpp")
DEFAULT_TIMEOUT = 1800.0

# 兩條引擎的完整配方，全部實測過。切引擎 = 換 VAE + text encoder + 步數 + 旗标，
# 不是換個 diffusion 檔的事（Krea2 是 wan VAE/4B 編碼器，Qwen 系是 qwen VAE/8B）。
ENGINES = {
    "viggle": {
        # DMD 蒸餾：40 次模型評估 → 6 次，免 CFG。scheduler 實測 discrete 優於 simple。
        # ⚠️ --sigmas 必須 7 個值、尾巴補 0。HF model card 上寫 6 個，那是 diffusers
        # 格式；照抄 6 個進 sd.cpp 會產出整片鹽胡椒噪點（實測踩過）。
        "diff": "models/diffusion_models/Qwen-Image-2.1-viggle-turbo-v0.3-6step-Q4_K_M.gguf",
        "vae": "models/vae/qwen_image_2.1_vae_bf16.safetensors",
        "llm": "models/text_encoders/Qwen3VL-8B-Instruct-Q4_K_M.gguf",
        "vision": "models/text_encoders/mmproj-Qwen3VL-8B-Instruct-F16.gguf",
        "steps": 6,
        "sigmas": "1,0.9375,0.875,0.75,0.5,0.25,0",
        "refs": True,  # -r 參考圖走 Qwen3VL 視覺塔
    },
    "krea2": {
        # ⚠️ --tae（TAEHV）是 Krea2 的 2 倍提速來源：wan VAE 真 decode 要 84s，
        #    占 207s 的 41%；TAEHV 打到接近 0。Qwen 系不需要（decode 本來就 ~1s）。
        # 視覺塔/參考圖未驗證，先不開 refs——見 _pick_engine 的 ref 回退說明。
        "diff": "models/diffusion_models/Krea2_turbo_uncensored_v1.1-Q6_K.gguf",
        "vae": "models/vae/wan_2.1_vae.safetensors",
        "tae": "models/vae/taew2_1.safetensors",
        "llm": "models/text_encoders/Qwen3VL-4B-Instruct-Q4_K_M.gguf",
        # 8 步（非蒸餾設計點的 4 步）——2026-10-02 依實測目檢結論配置。
        # 代價實測：cn 61→111s / multi 68→111s / nsfw 63→101s，即 +60~82% 牆鐘。
        "steps": 8,
        # ⚠️ 視覺塔必須是 4B 那顆。配 8B 的 mmproj 會**靜默失效**：llm.hpp:386 只印
        #    ERROR 然後 vision disabled，sd-cli 照樣 rc=0 存檔，但圖完全無視 -r
        #    （實測：臥床裸女輸入 → 穿紅衣棚拍，連審查降級一起發生）。
        #    判準是 out_hidden_size 要等於 LLM hidden_size（4B=2560 / 8B=4096）。
        "vision": "models/text_encoders/mmproj-Qwen3VL-4B-Instruct-F16.gguf",
        "refs": True,  # 2026-10-02 實測通過（view/krea_r4bmmproj.png：姿勢/床/窗/簾全對位）
    },
}

# dbcache 塊級快取，**生產預設關閉**（opt-in，要開用 QWEN_IMAGE_DBCACHE=1）。
# 關的理由不是效能而是畫質：1024² benchmark 產物目檢乾淨，但 512² 實測明顯鬼影
# ——左側半透明重複人形 + 主體周圍 2~3 張幽靈臉（docs/tools.md §11）。而短劇草稿檔
# 正是 512x288/512²，髒的剛好是產量最大那一級。牆鐘只快 9.1%，買不到這個。
# ⚠️ 真要開回來，門檻兩個坑別回頭：threshold 0.2 起鬼影/重曝、0.25 跳 3/8 步必壞，
# 所以是 0.1；warmup 留預設 0——舊值 warmup=4 是照 20 步檔定的，4/6 步引擎上前
# 4 步強制計算，快取等於沒開。
DBCACHE = os.environ.get("QWEN_IMAGE_DBCACHE", "0") == "1"
DBCACHE_OPTION = "threshold=0.1"  # image_upscale 共用同一條，避免兩處各抄一份改漏

# 每張參考圖的視覺編碼常駐統一記憶體。實測 M5 Pro 48GB：864x480 + 8 refs 完成
# （579s），9-10 refs 時 RSS 衝上 ~22GB、swap 打滿、進程 0.3% CPU 停滯永不返回
# ——和 1024x1024 的像素死鎖同一機制，觸發軸換成 ref 數。硬截是硬體保護；
# 產品級的 maxImages（=4）由影策能力聲明與畫布提交校驗管。
# ponytail: 硬上限 8 是單機實測值，換機器/換模型需重測。Krea2 未開 refs，不適用。
MAX_REFS = 8

# ponytail: global lock — single Metal device; parallel runs unmeasured
_run_lock = threading.Lock()


def _round32(v: int) -> int:
    # Qwen 系要求邊長 %32==0；Krea2 走 wan VAE，同樣是 32 倍數網格。
    # 短劇解析度族（288x512 / 576x1024 / 1080x1920）本身就已對齊，無需額外處理。
    return max(32, v // 32 * 32)


def _pick_engine(params: dict, unc: bool | None) -> str:
    """選引擎。優先級 = 任務參數 engine > env QWEN_IMAGE_ENGINE > unc 推導。

    unc 沿用舊語義（compat 按模型名分流：uncensored/nsfw → unc=True）。舊碼裡
    unc 的意思是「換去審查底模 + heretic 視覺塔」，實測 Viggle 對 normal 和 NSFW
    提示詞都是零降級直出（官方底模也是），那個前提已不成立——所以 unc 現在只當
    「要 Krea2」的快捷方式：它本來就叫 uncensored，西方寫實質感/直白構圖，
    正是要 NSFW 提示詞時想要的東西。
    """
    want = (params.get("engine") or os.environ.get("QWEN_IMAGE_ENGINE") or "").lower()
    if want:
        if want not in ENGINES:
            raise ValueError(f"engine must be one of {sorted(ENGINES)}, got {want!r}")
        return want
    if unc is None:
        unc = os.environ.get("QWEN_IMAGE_UNCENSORED") == "1"
    return "krea2" if unc else "viggle"


def _cli_cmd(prompt: str, out: Path, width: int, height: int, engine: str,
             seed: int, refs=()) -> list[str]:
    b = Path(DEFAULT_HOME)
    e = ENGINES[engine]
    cmd = [
        str(b / "build" / "bin" / "sd-cli"),
        "--diffusion-model", str(b / e["diff"]),
        "--vae", str(b / e["vae"]),
        "--llm", str(b / e["llm"]),
    ]
    if e.get("tae"):
        cmd += ["--tae", str(b / e["tae"])]
    if e.get("sigmas"):
        cmd += ["--scheduler", "discrete", "--sigmas", e["sigmas"]]
    else:
        # Krea2 走 --diffusion-fa，不用 --fa（那是給 flash-attn 的路徑）
        cmd += ["--diffusion-fa"]
    if refs:
        for r in refs:  # 圖+文編輯：-r 參考圖；編輯模式必須給視覺塔
            cmd += ["-r", str(r)]
        cmd += ["--llm_vision", str(b / e["vision"])]
        # 多 ref 的視覺 KV 是峰值 RSS 大頭（25GB/8ref）。8-bit 前綴快取省一半，
        # 換取 swap 壓力消失；KV 量化對畫質影響遠小於 swap 拖慢（docs/qwen_image_2.1.md）
        cmd += ["--model-args", "qwen_image_2_1_prefix_cache_type=q8_0"]
    if DBCACHE:
        cmd += ["--cache-mode", "dbcache", "--cache-option", DBCACHE_OPTION]
    cmd += [
        "-p", prompt,
        "-W", str(width), "-H", str(height),
        # 注意：sd.cpp 的 -s 是 --seed 的缩写，steps 只有长旗标——用 -s 传步数
        # 实际改的是 seed（随后还被 --seed 覆盖），步数永远是默认 20。
        "--steps", str(e["steps"]), "--cfg-scale", "1.0",
        "--sampling-method", "euler", "--offload-to-cpu",
        "--seed", str(seed),
        "-o", str(out),
    ]
    return cmd


def run(params: dict, job_dir: Path, progress, cancel) -> dict:
    prompt = params.get("prompt")
    if not prompt:
        raise ValueError("prompt is required")
    width = _round32(number(params, "width", 1024, 32, 4096, int))
    height = _round32(number(params, "height", 1024, 32, 4096, int))
    unc = bool(params.get("uncensored")) or os.environ.get("QWEN_IMAGE_UNCENSORED") == "1"
    engine = _pick_engine(params, unc)
    e = ENGINES[engine]
    seed = seed_of(params, int.from_bytes(os.urandom(4), "little"))
    timeout = number(params, "timeout", DEFAULT_TIMEOUT, 1.0, 4 * 3600.0, float)

    out = job_dir / "image.png"
    refs = [r for r in (params.get("refs") or []) if r]
    if len(refs) > MAX_REFS:
        # 前面的是角色/場景（上游連線順序），截尾丟道具——記憶體死鎖比丟道具參考貴。
        refs = refs[:MAX_REFS]
    progress(0.05, "generating")
    with _run_lock:
        run_cli(_cli_cmd(prompt, out, width, height, engine, seed, refs),
                cwd=DEFAULT_HOME, log_path=job_dir / "qwen_image.log",
                env=None,  # inherit environ
                timeout=timeout, cancel=cancel, engine="qwen_image")
    progress(0.95, "saving")
    if not out.is_file():
        raise Exception("qwen_image produced no output")
    return {"output_path": str(out), "engine": engine, "model": Path(e["diff"]).name,
            "steps": e["steps"], "cfg_scale": 1.0, "seed": seed,
            "width": width, "height": height}