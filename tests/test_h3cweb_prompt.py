"""H3 Context-IR prompt validation — one runnable check per spec rule.

Spec: ~/.claude/skills/h3-prompt-writing/references/base-en.txt
Each test below is a rule that was actually broken at least once in practice.

Run: .venv/bin/python tests/test_h3cweb_prompt.py
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = tempfile.mkdtemp(prefix="mg_prompt_test_")
os.environ.setdefault("MG_DB", os.path.join(_TMP, "gateway.db"))
os.environ.setdefault("MG_ASSETS", os.path.join(_TMP, "assets"))
os.environ.setdefault("H3CWEB_COMPAT_BASE_DIR", _TMP)
os.environ.setdefault("H3CWEB_COMPAT_OUT_DIR", os.path.join(_TMP, "shots"))
os.environ.setdefault("H3CWEB_COMPAT_REFS_DIR", os.path.join(_TMP, "refs"))
for d in ("assets", "shots", "refs"):
    os.makedirs(os.path.join(_TMP, d), exist_ok=True)

from server import compat_h3cweb as compat  # noqa: E402

PRE = ("For the target video, at 0.00 seconds into the target video, "
       "<Picture 1> (from [Shot 1]) is fully referenced.")

GOOD_I2VA = f"""{PRE}

integrated_multimodal_description: [Shot 1] Live-action, cinematic, a static medium shot frames the young woman shown in <Picture 1>. The camera holds a static shot. A calm adult male voice (S1) says in an off-screen voiceover: <d>[Chinese] 你是誰？</d> while the woman's lips remain completely closed.

overall_soundscape: A faint, steady room tone of an empty indoor space runs beneath the line.

non_diegetic_music: N/A"""

GOOD_T2VA = """integrated_multimodal_description: [Shot 1] Live-action, cinematic, a static medium shot frames a rain-soaked cyclist.

overall_soundscape: Steady rain taps the pavement beneath a low ventilation hum.

non_diegetic_music: Sparse piano notes at a slow tempo."""

# the real mistake: <d> left in overall_soundscape, so the model gets no
# dialogue anchor at all
DIALOGUE_IN_SOUNDSCAPE = f"""{PRE}

integrated_multimodal_description: [Shot 1] Live-action, cinematic, a static medium shot frames the young woman shown in <Picture 1>. She looks into the lens and waits.

overall_soundscape: A calm male voice off-screen asks: <d>[Chinese] 你是誰？</d> Quiet room tone beneath it.

non_diegetic_music: N/A"""


def check(name, cond):
    print(f"{'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        raise SystemExit(1)


def err(prompt, refs=0):
    return compat._validate_context_ir(prompt, refs)


def main():
    # --- the happy paths -------------------------------------------------
    check("accepts a conforming I2VA prompt", err(GOOD_I2VA, refs=1) is None)
    check("accepts a conforming T2VA prompt (no preamble)", err(GOOD_T2VA) is None)
    check("accepts real prose in non_diegetic_music",
          err(GOOD_T2VA.replace("Sparse piano notes at a slow tempo.",
                                "A solo cello holds one long bowed note.")) is None)

    # --- §2.1 I2VA preamble must be verbatim ------------------------------
    check("rejects I2VA with no preamble", err(GOOD_I2VA.split("\n\n", 1)[1], refs=1)
          is not None)
    check("rejects a paraphrased preamble",
          err(GOOD_I2VA.replace("is fully referenced", "is kept as is"), refs=1)
          is not None)
    check("rejects the preamble with no blank line after it",
          err(GOOD_I2VA.replace(PRE + "\n\n", PRE + "\n"), refs=1) is not None)
    check("does not require a preamble for T2VA", err(GOOD_T2VA) is None)
    check("does not check the preamble for 2+ refs (FL2VA is ambiguous)",
          err(GOOD_I2VA, refs=2) is None)

    # --- §4.6 dialogue must not live in overall_soundscape ---------------
    e = err(DIALOGUE_IN_SOUNDSCAPE, refs=1)
    check("rejects <d> inside overall_soundscape", e and "overall_soundscape" in e)
    e = err(GOOD_I2VA.replace(
        "while the woman's lips remain completely closed.",
        "while the woman's lips remain completely closed.").replace(
        "overall_soundscape: A faint, steady room tone of an empty indoor "
        "space runs beneath the line.",
        "non_diegetic_music: N/A\n\noverall_soundscape: see <d>[Chinese] 我好喜歡你。</d>"))
    check("rejects <d> inside non_diegetic_music", e is not None)

    # --- §4.4 language tags are English names -----------------------------
    check("rejects a native-script language tag",
          err(GOOD_I2VA.replace("[Chinese]", "[中文]"), refs=1) is not None)
    check("rejects an empty language tag",
          err(GOOD_I2VA.replace("[Chinese]", "[]"), refs=1) is not None)
    check("accepts <d>[English] like the official examples",
          err(GOOD_I2VA.replace("[Chinese]", "[English]"), refs=1) is None)

    # --- §4.4 every line needs a speaker id -------------------------------
    check("rejects <d> with no (S1)/(S2) before it",
          err(GOOD_I2VA.replace("(S1) says in an off-screen voiceover", "says"),
              refs=1) is not None)
    check("accepts two lines from one speaker id",
          err(GOOD_I2VA.replace(
              "while the woman's lips remain completely closed.",
              "while the woman's lips remain completely closed. She then "
              "answers, her lips moving in sync: <d>[Chinese] 我好喜歡你。</d>"),
              refs=1) is None)

    # --- field presence and order ----------------------------------------
    check("rejects a missing field",
          err(GOOD_I2VA.replace("non_diegetic_music: N/A", ""), refs=1) is not None)
    swapped = (GOOD_I2VA.replace("overall_soundscape:", "overall_soundscape: TMP")
               .replace("non_diegetic_music: N/A", "overall_soundscape:")
               .replace("overall_soundscape: TMP",
                        "non_diegetic_music: N/A"))
    e = err(swapped, refs=1)
    check("rejects fields out of order", e and ("out of order" in e or "missing" in e))
    check("rejects a duplicated field",
          err(GOOD_I2VA + "\n\nnon_diegetic_music: N/A", refs=1) is not None)

    # --- §4.7 non_diegetic_music sentinel ---------------------------------
    check("rejects 'No music.' as the sentinel",
          err(GOOD_I2VA.replace("non_diegetic_music: N/A",
                                "non_diegetic_music: No music."), refs=1) is not None)
    check("rejects 'None' as the sentinel",
          err(GOOD_I2VA.replace("non_diegetic_music: N/A",
                                "non_diegetic_music: None"), refs=1) is not None)

    # --- the gate is mandatory -------------------------------------------
    check("free text is rejected: h3.c is trained on Context-IR",
          "missing field" in (err("a cat on a roof") or ""))
    check("the loose Scene/Action/Audio form is rejected too",
          err("Scene: a pier\nAction: wind blows\nAudio: gusting wind") is not None)
    check("free text with an image is rejected on the preamble as well",
          "I2VA prompts must open" in (err("a cat on a roof", refs=1) or ""))
    print("\nall prompt-format rules hold")


if __name__ == "__main__":
    main()