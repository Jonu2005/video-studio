#!/usr/bin/env python3
"""Piper TTS narration helper for the Video Studio website.

Same CLI as the Kokoro helper it replaces, so engine/make_video.py
can use it unchanged apart from the helper path:

    piper_narrate.py script.txt narration.wav
        [--sections-dir ./sections/] [--voice priyamvada]
        [--speed 1.0] [--lang hi]

Why Piper (Nov 2026): Kokoro needs ~580MB peak RSS on CPU — more
than the 512MB free DeployVera container, so website renders stalled
forever on the narration step. Piper needs ~249MB and synthesises
~8x faster. The user chose Piper on 2026-11-23 after hearing both
voices (samples in ~/workspace/video-studio/voice-test/).

Voices: hi -> priyamvada (hi_IN-priyamvada-medium, female),
en -> amy (en_US-amy-medium, female). The --voice value may be the
short name ("priyamvada") or the full model name.

The helper prints the same progress lines as the Kokoro helper,
including "-> .../section_NN.wav" per section — make_video.py parses
those lines to advance the UI progress bar.
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import time

import numpy as np
import soundfile as sf

SECTION_GAP_S = 0.5       # silence between sections (matches engine)
SENTENCE_SILENCE_S = 0.25  # pause Piper inserts between sentences

VOICES = {
    "priyamvada": "hi_IN-priyamvada-medium",
    "amy": "en_US-amy-medium",
}
DEFAULT_VOICE_BY_LANG = {"hi": "priyamvada", "en": "amy"}


def resolve_piper():
    """Find the piper binary (pip-installed piper-tts)."""
    binary = os.environ.get("PIPER_BIN") or shutil.which("piper")
    if not binary:
        for candidate in ("/app/piper/piper",
                          "/usr/local/bin/piper"):
            if os.path.exists(candidate):
                binary = candidate
                break
    if not binary:
        raise SystemExit("piper binary not found (set PIPER_BIN or "
                         "install piper-tts)")
    return binary


def resolve_voice_dir():
    """Find the directory holding the Piper .onnx voice models."""
    directory = os.environ.get("PIPER_VOICE_DIR")
    if directory and os.path.isdir(directory):
        return directory
    for candidate in ("/app/models/piper",
                      os.path.expanduser(
                          "~/workspace/tools/piper-voices")):
        if os.path.isdir(candidate):
            return candidate
    raise SystemExit("Piper voice dir not found (set PIPER_VOICE_DIR)")


def resolve_model(voice, lang):
    """Map a voice name to its .onnx model file."""
    name = VOICES.get(voice, voice)
    voice_dir = resolve_voice_dir()
    model = os.path.join(voice_dir, name + ".onnx")
    if not os.path.exists(model):
        raise SystemExit(f"Piper voice model not found: {model}")
    return model


def split_sections(text):
    """Split a script into sections on blank lines."""
    parts = [part.strip() for part in re.split(r"\n\s*\n", text.strip())]
    return [part for part in parts if part]


def silence(sr, seconds):
    return np.zeros(int(sr * seconds), dtype=np.float32)


def synth_section(piper_bin, model, text, out_path, length_scale):
    """Synthesise one section with Piper; return (seconds, sample_rate)."""
    flat = " ".join(text.split())
    cmd = [piper_bin, "--model", model, "--output_file", out_path,
           "--length-scale", str(length_scale),
           "--sentence-silence", str(SENTENCE_SILENCE_S)]
    process = subprocess.run(cmd, input=flat, capture_output=True,
                             text=True)
    if process.returncode != 0:
        raise SystemExit(
            f"piper failed (exit {process.returncode}): "
            f"{process.stderr.strip()[-500:]}")
    info = sf.info(out_path)
    return info.duration, info.samplerate


def main():
    ap = argparse.ArgumentParser(
        description="Piper narration generator (Video Studio website)")
    ap.add_argument("script", help="script .txt (sections = blank lines)")
    ap.add_argument("output", help="output narration .wav")
    ap.add_argument("--sections-dir", default="",
                    help="write per-section wavs here")
    ap.add_argument("--voice", default="",
                    help="Piper voice (priyamvada / amy)")
    ap.add_argument("--speed", type=float, default=1.0,
                    help="speech speed (1.0 = normal)")
    ap.add_argument("--lang", default="hi", help="hi or en")
    args = ap.parse_args()

    with open(args.script, encoding="utf-8") as handle:
        text = handle.read()
    sections = split_sections(text)
    print(f"sections: {len(sections)}", flush=True)

    voice = args.voice or DEFAULT_VOICE_BY_LANG.get(args.lang, "priyamvada")
    piper_bin = resolve_piper()
    model = resolve_model(voice, args.lang)
    print(f"piper voice={voice} model={os.path.basename(model)}",
          flush=True)

    # Piper's --length-scale is the inverse of speed (smaller = faster).
    speed = min(2.0, max(0.5, args.speed))
    length_scale = round(1.0 / speed, 3)

    sr = None
    full_parts = []
    total_words = 0
    gen_time = 0.0
    for i, section in enumerate(sections, 1):
        words = len(section.split())
        total_words += words
        sec_path = None
        if args.sections_dir:
            os.makedirs(args.sections_dir, exist_ok=True)
            sec_path = os.path.join(args.sections_dir,
                                    f"section_{i:02d}.wav")
        else:
            sec_path = args.output + f".section_{i:02d}.wav"
        t0 = time.time()
        sec_dur, sr = synth_section(piper_bin, model, section, sec_path,
                                    length_scale)
        gen_time += time.time() - t0
        print(f"  section {i:02d}: {words} words, {sec_dur:.1f}s",
              flush=True)
        print(f"    -> {sec_path}", flush=True)
        audio, _ = sf.read(sec_path, dtype="float32")
        full_parts.append(audio)
        full_parts.append(silence(sr, SECTION_GAP_S))
    if full_parts:
        full_parts.pop()  # drop trailing section gap

    audio = np.concatenate(full_parts) if full_parts else np.zeros(
        0, dtype=np.float32)
    sf.write(args.output, audio, sr)
    duration = len(audio) / sr
    pace = total_words / duration if duration else 0
    print(f"wrote {args.output}: {total_words} words, {duration:.1f}s, "
          f"pace {pace:.2f} words/sec, generation {gen_time:.0f}s "
          f"({gen_time / duration:.2f}x real-time)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
