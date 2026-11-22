#!/home/hatch/workspace/tools/kokoro-venv/bin/python
"""Kokoro TTS narration helper for Awesome Hacks Shorts.

Channel voice: Kokoro hm_omega (Hindi male), normal speed (1.0).
Reads a Devanagari script .txt file and produces a narration .wav.

Usage:
    kokoro_narrate.py <script.txt> <output.wav> [--sections-dir DIR]
                        [--voice hm_omega] [--speed 1.0] [--lang hi]

Per-section support: split the script into sections with a blank line
(or a line containing only ---, or a line starting with ## ). With
--sections-dir, each section is also saved as section_01.wav,
section_02.wav, ... so every section's voiceover can be matched to its
video clip. The main output.wav is the full narration with all sections
joined (0.5s gap between sections, 0.25s gap between sentences).

Pace (measured 2026-11-22, hm_omega speed 1.0):
- Single-section script: ~2.40 words/sec -> a ~145-word script = ~60s.
  Perfect fit for a 60s Short.
- Multi-section script: ~2.0 words/sec effective (each section call has
  its own pacing + 0.5s gaps) -> a ~145-word script = ~72s. When using
  per-section wavs, keep scripts to ~125-135 words or apply a mild
  ffmpeg atempo (~1.1) to fit 60s.
Generation runs at ~1x real-time on this CPU (~60s of audio takes
~60s to generate), plus ~2s model load.

Requirements: ~/workspace/tools/kokoro-venv (kokoro-onnx), model files in
~/workspace/tools/kokoro-models/, system espeak-ng (apt-installed).
"""
import argparse
import os
import re
import sys
import time

import numpy as np
import soundfile as sf
from kokoro_onnx import EspeakConfig, Kokoro

# Paths are configurable via environment variables so this helper works
# on any machine; defaults are the paths on the original Muse VM.
MODEL = os.environ.get(
    "KOKORO_MODEL",
    "/home/hatch/workspace/tools/kokoro-models/kokoro-v1.0.fp16.onnx")
VOICES = os.environ.get(
    "KOKORO_VOICES",
    "/home/hatch/workspace/tools/kokoro-models/voices-v1.0.bin")
ESPEAK_LIB = os.environ.get(
    "ESPEAK_LIB", "/usr/lib/x86_64-linux-gnu/libespeak-ng.so.1")
ESPEAK_DATA = os.environ.get(
    "ESPEAK_DATA", "/usr/lib/x86_64-linux-gnu/espeak-ng-data")

SENTENCE_GAP_S = 0.25   # silence Kokoro inserts between sentences
SECTION_GAP_S = 0.50    # silence between sections
MAX_SECTION_CHARS = 1000  # longer sections are split into sentence groups

# Devanagari danda, !, ?, ., plus newlines end a sentence.
SENTENCE_END = re.compile(r"([^।?!\.\n]+[।?!\.]?)")


def split_sections(text):
    """Split script into sections on blank lines / --- / ## markers."""
    sections = []
    current = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "" or stripped == "---" or stripped.startswith("## "):
            if current:
                sections.append(" ".join(current))
                current = []
            continue
        current.append(stripped)
    if current:
        sections.append(" ".join(current))
    return [s for s in sections if s.strip()]


def split_sentences(section):
    """Split a section into sentences (fallback for very long sections)."""
    sentences = []
    for part in SENTENCE_END.findall(section):
        part = part.strip(" \t")
        if part:
            sentences.append(part)
    return sentences


def synth_chunks(kokoro, text, voice, speed, lang):
    """Synthesize text in one call per chunk. Short text = one call, which
    keeps natural cross-sentence prosody (much better pace than
    sentence-by-sentence synthesis). Only very long text is split."""
    if len(text) <= MAX_SECTION_CHARS:
        return [text]
    chunks, current = [], ""
    for sent in split_sentences(text):
        if len(current) + len(sent) + 1 > MAX_SECTION_CHARS and current:
            chunks.append(current)
            current = sent
        else:
            current = f"{current} {sent}".strip()
    if current:
        chunks.append(current)
    return chunks


def silence(sr, seconds):
    return np.zeros(int(sr * seconds), dtype=np.float32)


def main():
    ap = argparse.ArgumentParser(description="Kokoro Hindi narration generator")
    ap.add_argument("script", help="Devanagari script .txt file")
    ap.add_argument("output", help="Output narration .wav file")
    ap.add_argument("--voice", default="hm_omega", help="Kokoro voice (default: hm_omega)")
    ap.add_argument("--speed", type=float, default=1.0, help="Speaking speed (default: 1.0 = normal)")
    ap.add_argument("--lang", default="hi", help="Language code (default: hi)")
    ap.add_argument("--sections-dir", default=None,
                    help="Also save each section as section_NN.wav in this directory")
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--voices", default=VOICES)
    args = ap.parse_args()

    # espeak/phonemizer reject bare "en" (needs a locale); Kokoro itself
    # also expects "en-us"/"en-gb". Map plain "en" to American English.
    if args.lang == "en":
        args.lang = "en-us"

    with open(args.script, encoding="utf-8") as f:
        text = f.read().strip()
    if not text:
        print("ERROR: script file is empty", file=sys.stderr)
        return 1

    sections = split_sections(text)
    print(f"sections: {len(sections)}", flush=True)

    cfg = EspeakConfig(lib_path=ESPEAK_LIB, data_path=ESPEAK_DATA)
    t0 = time.time()
    kokoro = Kokoro(args.model, args.voices, espeak_config=cfg)
    print(f"model loaded in {time.time() - t0:.1f}s", flush=True)

    sr = None
    full_parts = []
    total_words = 0
    gen_time = 0.0
    for i, section in enumerate(sections, 1):
        words = len(section.split())
        total_words += words
        sec_parts = []
        for chunk in synth_chunks(kokoro, section, args.voice, args.speed, args.lang):
            t0 = time.time()
            samples, sr = kokoro.create(
                chunk, voice=args.voice, speed=args.speed, lang=args.lang,
                sentence_pause=SENTENCE_GAP_S,
            )
            gen_time += time.time() - t0
            sec_parts.append(samples.astype(np.float32))
            sec_parts.append(silence(sr, SENTENCE_GAP_S))
        if sec_parts:
            sec_parts.pop()  # drop trailing chunk gap
        sec_audio = np.concatenate(sec_parts) if sec_parts else np.zeros(0, dtype=np.float32)
        sec_dur = len(sec_audio) / sr
        print(f"  section {i:02d}: {words} words, {sec_dur:.1f}s", flush=True)
        if args.sections_dir:
            import os
            os.makedirs(args.sections_dir, exist_ok=True)
            sec_path = os.path.join(args.sections_dir, f"section_{i:02d}.wav")
            sf.write(sec_path, sec_audio, sr)
            print(f"    -> {sec_path}", flush=True)
        full_parts.append(sec_audio)
        full_parts.append(silence(sr, SECTION_GAP_S))
    if full_parts:
        full_parts.pop()  # drop trailing section gap

    audio = np.concatenate(full_parts) if full_parts else np.zeros(0, dtype=np.float32)
    sf.write(args.output, audio, sr)
    duration = len(audio) / sr
    pace = total_words / duration if duration else 0
    print(f"wrote {args.output}: {total_words} words, {duration:.1f}s, "
          f"pace {pace:.2f} words/sec, generation {gen_time:.0f}s "
          f"({gen_time / duration:.2f}x real-time)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
