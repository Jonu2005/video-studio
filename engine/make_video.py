#!/usr/bin/env python3
"""Video Studio — free, unlimited, open-source long-form video generator.

Documentary/explainer style: script -> Piper narration -> visuals
planned per ~7s beat (real stock footage first — Pexels/Pixabay when
a key is set, else Wikimedia Commons — then AI-generated images per
beat in the chosen style, Openverse stock, title card) with Ken Burns
motion + crossfades and per-section text callouts -> burned-in
captions -> synth music bed -> 1080p landscape video, assembled
section by section so even a 30-minute video renders without loading
everything at once.

Usage:
    python3 make_video.py --script script.txt --title "My Title" \
        --out video.mp4 [--lang hi|en] [--music track.wav] [--voice NAME] \
        [--style cinematic|madhubani|realistic|cartoon3d|anime|watercolor|oilpainting|cyberpunk]

The script is plain UTF-8 text, sections separated by blank lines.
See README.md and engine/sections.py for the full script format.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sections as section_mod
import images as images_mod
import ai_images as ai_images_mod
import stock_video as stock_video_mod
import render as render_mod

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENGINE_DIR = os.path.dirname(os.path.abspath(__file__))

WIDTH, HEIGHT, FPS = 1920, 1080, 30
SECTION_GAP_S = 0.5       # silence between sections (matches Piper helper)
MUSIC_VOLUME = 0.15
TARGET_MEAN_DB = -17.0
MAX_ADJUST_DB = 6.0
ZOOM_AMOUNT = 0.12        # total zoom across a section (1.00 -> 1.12)
CROSSFADE_S = 0.5         # crossfade between consecutive images
# Ken Burns motions, cycled across the video's images so no two
# neighbouring images move the same way.
MOTIONS = ("zoom-in", "zoom-out", "pan-right", "pan-left")

DEFAULT_CONFIG = {
    "voices": {"hi": "priyamvada", "en": "amy"},
    "speed": 1.0,
    "music_default": os.path.expanduser(
        "~/workspace/youtube/music/synth_chill_gmaj.wav"),
    "caption_font": {"hi": "Noto Sans Devanagari", "en": "DejaVu Sans"},
    "caption_size": 34,
    "crf": 23,
    "preset": "veryfast",
    "pexels_api_key": "",
    "pixabay_api_key": "",
    "style_default": "cinematic",
}


def load_config(path):
    config = dict(DEFAULT_CONFIG)
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as handle:
            config.update(json.load(handle))
    return config


def resolve_narrator():
    """Find the Piper narration helper script and its Python interpreter.

    (Nov 2026: the website uses Piper, not Kokoro — Kokoro needs
    ~580MB peak RSS on CPU, more than the 512MB free container;
    Piper needs ~249MB. The main engine + Awesome Hacks still use
    Kokoro on the Muse VM, where memory is not constrained.)
    """
    script = os.environ.get("VIDEO_STUDIO_NARRATOR_SCRIPT")
    if not script:
        vendored = os.path.join(ENGINE_DIR, "vendor", "piper_narrate.py")
        original = os.path.expanduser(
            "~/workspace/tools/piper_narrate.py")
        script = vendored if os.path.exists(vendored) else original
    python = os.environ.get("VIDEO_STUDIO_NARRATOR_PYTHON")
    if not python:
        python = sys.executable
    return python, script


def log(message):
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


class Job:
    """Tracks paths and progress for one render.

    status.json is the UI's only window into a render that can run for an
    hour, so it is updated *within* long stages too (per narration section,
    per image, per segment) — not just between steps. It also carries
    `stage_started_at` (epoch seconds, reset when the stage name changes)
    so the UI can show how long the current stage has been running, and
    `sections_done` / `sections_total` for the current stage's unit count.
    Writes are atomic (tmp file + rename) so a reader never sees a
    half-written file.
    """

    def __init__(self, workdir):
        self.workdir = workdir
        os.makedirs(workdir, exist_ok=True)
        self.status_path = os.path.join(workdir, "status.json")
        self._stage = None
        self._stage_started_at = None
        self.sections_total = None
        self.sections_done = None
        self.stage("starting", 0.0, "Starting render")

    def stage(self, stage, progress, message):
        now = time.time()
        if stage != self._stage:
            self._stage = stage
            self._stage_started_at = now
        log(message)
        payload = {"stage": stage, "progress": progress,
                   "message": message,
                   "stage_started_at": self._stage_started_at,
                   "updated_at": now}
        if self.sections_total is not None:
            payload["sections_total"] = self.sections_total
        if self.sections_done is not None:
            payload["sections_done"] = self.sections_done
        tmp_path = self.status_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        os.replace(tmp_path, self.status_path)


# Narration (Piper synthesises ~5x real-time on CPU, so it is quick
# even on the small free container). Its progress band in status.json
# runs from NARRATION_PROGRESS_START to _END, advanced once per
# finished section so the UI bar keeps moving instead of sitting at 5%
# for the whole stage.
NARRATION_PROGRESS_START = 0.05
NARRATION_PROGRESS_END = 0.40
_SECTION_WAV_RE = re.compile(r"section_(\d+)\.wav")


def run_narration(job, clean_script, narration_path, sections_dir,
                  voice, speed, lang, total_sections):
    """Run the Piper helper; stream its output so progress is visible,
    and turn each finished section wav into a status.json update."""
    python, helper = resolve_narrator()
    log(f"narration: Piper voice={voice} lang={lang} "
        f"(helper: {helper})")
    cmd = [python, helper, clean_script, narration_path,
           "--sections-dir", sections_dir,
           "--voice", voice, "--speed", str(speed), "--lang", lang]
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True)
    for line in process.stdout:
        print(f"  piper: {line.rstrip()}", flush=True)
        # The helper writes "-> .../section_NN.wav" as each section's
        # narration lands on disk — that is the sub-stage progress signal.
        match = _SECTION_WAV_RE.search(line)
        if match and "->" in line and total_sections:
            done = int(match.group(1))
            if done != job.sections_done:
                job.sections_done = done
                span = NARRATION_PROGRESS_END - NARRATION_PROGRESS_START
                job.stage(
                    "narration",
                    NARRATION_PROGRESS_START + span * done / total_sections,
                    f"Step 1/5: narration — section {done}/"
                    f"{total_sections} done")
    process.wait()
    if process.returncode != 0:
        raise render_mod.RenderError(
            f"Piper narration failed (exit {process.returncode})")
    if not os.path.exists(narration_path):
        raise render_mod.RenderError("Piper produced no narration file")


def build_music_track(bed_path, total_duration, out_path):
    """Loop the music bed to the full video length with fades."""
    fade_out_start = max(total_duration - 3.0, 0)
    render_mod.run_command([
        "ffmpeg", "-y", "-v", "error",
        "-stream_loop", "-1", "-i", bed_path,
        "-t", str(total_duration),
        "-af",
        f"afade=t=in:st=0:d=1,afade=t=out:st={fade_out_start:.2f}:d=3",
        out_path,
    ], "music track")


def _zoompan_expr(motion, frames):
    """zoompan (z, x, y) expressions for one Ken Burns motion."""
    if motion == "zoom-out":
        return (f"1.12-{ZOOM_AMOUNT}*on/{frames}",
                "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)")
    if motion == "pan-right":
        return ("1.12", f"(iw-iw/zoom)*on/{frames}",
                "ih/2-(ih/zoom/2)")
    if motion == "pan-left":
        return ("1.12", f"(iw-iw/zoom)*(1-on/{frames})",
                "ih/2-(ih/zoom/2)")
    return (f"1+{ZOOM_AMOUNT}*on/{frames}",
            "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)")


def render_segment(job, index, visual_plan, narration_wav, music_path,
                   music_offset, duration, ass_path, config, lang,
                   motion_start, narration_gain=1.0):
    """Render one section: visuals + captions + narration + music.

    visual_plan is a list of {"path":..., "share":..., "kind":...}
    dicts (shares sum to 1.0), one per visual beat. "image" beats get
    Ken Burns motion (cycled via motion_start); "video" beats are real
    stock footage, already 1920x1080/30fps/muted by
    stock_video.prepare_beat_clip, and play as-is. Consecutive visuals
    are joined with a 0.5s crossfade. Every visual's clip is padded so
    the crossfades don't eat into the section's screen time, and the
    output is trimmed to exactly `duration`. motion_start is the
    global image counter, so motions alternate across the whole video.
    Returns (segment_path, images_used)."""
    n_items = len(visual_plan)
    # Clip durations: every clip except the last is CROSSFADE_S longer
    # (the xfade overlaps that tail with the next clip); the last clip
    # gets a small pad so frame rounding never leaves the video short
    # (output is trimmed to `duration` with -t anyway).
    clip_durs = []
    for j, item in enumerate(visual_plan):
        dur = item["share"] * duration
        dur += CROSSFADE_S if j < n_items - 1 else 0.2
        clip_durs.append(dur)
    segment = os.path.join(job.workdir, f"segment_{index:02d}.mp4")
    ass_escaped = ass_path.replace(":", "\\:").replace("'", "\\'")
    parts = []
    motion_idx = motion_start
    for j, item in enumerate(visual_plan):
        if item.get("kind") == "video":
            # Real footage: play as-is. setpts resets timestamps after
            # the trim/loop in prepare_beat_clip so the xfade chain
            # stays in sync; fps/format normalise the stream.
            parts.append(
                f"[{j}:v]setpts=PTS-STARTPTS,fps={FPS},"
                f"format=yuv420p[v{j}]"
            )
        else:
            frames = max(int(round(clip_durs[j] * FPS)), 1)
            motion = MOTIONS[motion_idx % len(MOTIONS)]
            motion_idx += 1
            z, x, y = _zoompan_expr(motion, frames)
            parts.append(
                f"[{j}:v]scale={WIDTH}:{HEIGHT}:"
                f"force_original_aspect_ratio=increase,"
                f"crop={WIDTH}:{HEIGHT},"
                f"zoompan=z='{z}':d={frames}:x='{x}':y='{y}':"
                f"s={WIDTH}x{HEIGHT}:fps={FPS}[v{j}]"
            )
    # xfade chain: the k-th fade starts where visual k-1's share ends.
    current = "v0"
    offset = 0.0
    for j in range(1, n_items):
        offset += visual_plan[j - 1]["share"] * duration
        out_label = f"x{j}" if j < n_items - 1 else "vx"
        parts.append(
            f"[{current}][v{j}]xfade=transition=fade:"
            f"duration={CROSSFADE_S}:offset={offset:.3f}[{out_label}]"
        )
        current = out_label
    narr_idx, music_idx = n_items, n_items + 1
    parts.append(f"[{current}]ass='{ass_escaped}'[v]")
    parts.append(
        f"[{narr_idx}:a]volume={narration_gain:.4f},"
        f"apad=whole_dur={duration:.3f}[na]")
    parts.append(
        f"[{music_idx}:a]volume={MUSIC_VOLUME},"
        f"atrim=0:{duration:.3f}[mu]")
    parts.append(
        "[na][mu]amix=inputs=2:duration=first:dropout_transition=0:"
        "normalize=0,aresample=44100[a]")
    filter_complex = ";".join(parts)
    cmd = ["ffmpeg", "-y", "-v", "error"]
    for item in visual_plan:
        cmd += ["-i", item["path"]]
    cmd += ["-i", narration_wav,
            "-ss", f"{music_offset:.3f}", "-i", music_path,
            "-filter_complex", filter_complex,
            "-map", "[v]", "-map", "[a]",
            "-c:v", "libx264", "-preset", config["preset"],
            "-crf", str(config["crf"]), "-pix_fmt", "yuv420p",
            "-threads", "2",
            "-r", str(FPS),
            "-c:a", "aac", "-b:a", "160k",
            "-t", f"{duration:.3f}",
            segment]
    render_mod.run_command(cmd, f"segment {index}")
    return segment, motion_idx - motion_start


def main():
    parser = argparse.ArgumentParser(
        description="Video Studio: free long-form video generator")
    parser.add_argument("--script", required=True,
                        help="Script .txt (sections separated by blank lines)")
    parser.add_argument("--title", required=True, help="Video title")
    parser.add_argument("--out", required=True, help="Output video .mp4")
    parser.add_argument("--lang", default="hi", choices=["hi", "en"],
                        help="Script language (default: hi)")
    parser.add_argument("--voice", default=None,
                        help="Piper voice (default from config)")
    parser.add_argument("--music", default=None,
                        help="Music bed .wav (default from config; "
                             "'none' for no music)")
    parser.add_argument("--style", default=None,
                        help="Visual style for AI images: cinematic "
                             "(default), madhubani, realistic, cartoon3d, "
                             "anime, watercolor, oilpainting, cyberpunk")
    parser.add_argument("--workdir", default=None,
                        help="Working directory (default: <out>.work)")
    parser.add_argument("--config", default=None,
                        help="config.json path (default: project config)")
    parser.add_argument("--keep-segments", action="store_true",
                        help="Keep per-section segment files after concat")
    parser.add_argument("--reuse-narration", action="store_true",
                        help="Reuse narration wavs already in the workdir "
                             "instead of regenerating them (only valid if "
                             "the script has not changed since they were made)")
    parser.add_argument("--reuse-images", action="store_true",
                        help="Reuse AI images already in the workdir "
                             "(ai_images/ai_XX_Y.jpg) instead of "
                             "regenerating them; lets you hand-replace a "
                             "bad image and re-render without losing the "
                             "good ones")
    args = parser.parse_args()

    started = time.time()
    config_path = args.config or os.path.join(PROJECT_ROOT, "config.json")
    config = load_config(config_path)
    voice = args.voice or config["voices"].get(args.lang, "hm_omega")
    music = args.music if args.music is not None else config["music_default"]
    if music == "none":
        music = None
    style = args.style or config.get("style_default", "cinematic")
    known_styles = set(ai_images_mod.DEFAULT_STYLES)
    known_styles.update((config.get("styles") or {}).keys())
    if style not in known_styles:
        log(f"  WARNING: unknown style '{style}'; using 'cinematic'")
        style = "cinematic"
    log(f"style: {style}")
    out_path = os.path.abspath(args.out)
    out_dir = os.path.dirname(out_path)
    os.makedirs(out_dir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(out_path))[0]
    # The workdir MUST be absolute: the concat step writes segment paths
    # into concat.txt, and ffmpeg resolves relative entries against the
    # concat file's own directory — a relative workdir doubles the path
    # and concat fails ("Impossible to open .../video.work/video.work/").
    workdir = os.path.abspath(
        args.workdir or os.path.join(out_dir, f"{stem}.work"))
    job = Job(workdir)

    # 1. Parse the script.
    with open(args.script, encoding="utf-8") as handle:
        script_text = handle.read()
    parsed = section_mod.parse_script(script_text)
    if not parsed:
        print("ERROR: no sections found in script", file=sys.stderr)
        return 1
    total_words = sum(len(s["text"].split()) for s in parsed)
    job.sections_total = len(parsed)
    log(f"script: {len(parsed)} sections, {total_words} words, "
        f"title: {args.title}")

    clean_script = os.path.join(workdir, "script_clean.txt")
    with open(clean_script, "w", encoding="utf-8") as handle:
        handle.write(section_mod.clean_script_text(parsed))

    # 2. Narration (Piper, per section) — or reuse existing wavs.
    job.stage("narration", 0.05, "Step 1/5: generating narration (Piper)")
    narration_path = os.path.join(workdir, "narration.wav")
    sections_dir = os.path.join(workdir, "sections")
    existing = [os.path.join(sections_dir, f"section_{i:02d}.wav")
                for i in range(1, len(parsed) + 1)]
    if (args.reuse_narration and os.path.exists(narration_path)
            and all(os.path.exists(w) for w in existing)):
        log("  reusing existing narration wavs from workdir")
        job.sections_done = len(parsed)
    else:
        if args.reuse_narration:
            log("  no complete narration found in workdir; regenerating")
        job.sections_done = 0
        run_narration(job, clean_script, narration_path, sections_dir,
                      voice, config["speed"], args.lang, len(parsed))

    section_durs = []
    for i in range(1, len(parsed) + 1):
        wav = os.path.join(sections_dir, f"section_{i:02d}.wav")
        section_durs.append(render_mod.media_duration(wav))
    # Each section's video also covers the 0.5s gap after it (except last).
    segment_durs = [d + (SECTION_GAP_S if i < len(parsed) - 1 else 0.0)
                    for i, d in enumerate(section_durs)]
    total_duration = sum(segment_durs)
    log(f"narration done: total {total_duration:.1f}s "
        f"({total_duration / 60:.1f} min)")

    # Measure the narration level and lift it to the target so the
    # target loudness at mix time. The gain is capped so peaks stay
    # below -3 dB (no clipping).
    narr_mean, narr_max = render_mod.volumedetect(narration_path)
    gain_db = min(TARGET_MEAN_DB - narr_mean, -3.0 - narr_max)
    gain_db = max(-3.0, min(9.0, gain_db))
    narration_gain = 10 ** (gain_db / 20.0)
    log(f"  narration level: mean {narr_mean:.1f} dB, max {narr_max:.1f} dB"
        f" -> narration gain {gain_db:+.1f} dB")

    # 3. Visuals, planned per beat (~7s, aligned to caption
    # boundaries): real stock footage first (Pexels/Pixabay when a
    # key is set, else Wikimedia Commons — no key), then a v2 AI image
    # in the chosen style, then Openverse stock, then a title card.
    job.stage("images", 0.42,
              f"Step 2/5: visuals bana rahe hain ({style})")
    cache_dir = os.path.join(PROJECT_ROOT, "cache")
    ai_dir = os.path.join(workdir, "ai_images")
    stock_dir = os.path.join(workdir, "stock")
    pexels_key = (os.environ.get("PEXELS_API_KEY")
                  or config.get("pexels_api_key") or "")
    pixabay_key = (os.environ.get("PIXABAY_API_KEY")
                   or config.get("pixabay_api_key") or "")
    attributions = []
    section_plans = []
    section_beats = []
    beat_stats = {"total": 0, "pexels": 0, "pixabay": 0, "wikimedia": 0,
                  "ai_image": 0, "openverse": 0, "title_card": 0}
    for i, section in enumerate(parsed, 1):
        beats = section_mod.split_into_beats(section["text"],
                                             segment_durs[i - 1])
        section_beats.append(beats)
        log(f"  section {i:02d}: {len(beats)} beat(s) "
            f"({segment_durs[i - 1]:.1f}s)")
        plan = []
        card_path = None  # one title card per section, shared by beats
        for b, beat in enumerate(beats):
            share = beat["duration"] / segment_durs[i - 1]
            # Prepared stock clips need the crossfade pad render_segment
            # adds (0.5s, or 0.2s for the last beat) plus a 0.1s safety
            # margin so frame rounding never leaves the clip short.
            clip_len = beat["duration"] + (
                CROSSFADE_S if b < len(beats) - 1 else 0.2) + 0.1
            query = stock_video_mod.beat_query(
                beat["text"], args.title, args.lang) \
                or section["img_query"]
            item = None
            if query:
                log(f"    beat {b + 1}: stock video '{query}'")
                clip, info = stock_video_mod.fetch_clip(
                    query, cache_dir,
                    pexels_key or None, pixabay_key or None)
                if clip:
                    prepared = os.path.join(
                        stock_dir, f"stock_{i:02d}_{b:02d}.mp4")
                    if args.reuse_images and os.path.exists(prepared):
                        log("      -> reusing prepared stock clip")
                    else:
                        prepared = stock_video_mod.prepare_beat_clip(
                            clip, prepared, clip_len)
                    if prepared:
                        source = info.get("source", "stock")
                        item = {"path": prepared, "share": share,
                                "kind": "video"}
                        beat_stats[source] = \
                            beat_stats.get(source, 0) + 1
                        attributions.append({
                            "query": query,
                            "source": source,
                            "title": info.get("title", ""),
                            "source_url": info.get("page_url", ""),
                            "clip_url": info.get("url", ""),
                        })
                        log(f"      -> stock video ({source}): "
                            f"{info.get('title', '')[:50]}")
            if item is None:
                # AI image for the beat (v2 pipeline, beat-level).
                img_path = os.path.join(
                    ai_dir, f"ai_{i:02d}_beat{b:02d}.jpg")
                path, attr = ai_images_mod.plan_beat_image(
                    beat["text"], args.title, args.lang, style, img_path,
                    config, reuse_images=args.reuse_images)
                if path:
                    item = {"path": path, "share": share, "kind": "image"}
                    beat_stats["ai_image"] += 1
                    attributions.append(attr)
            if item is None and query:
                # Openverse stock image for the beat.
                log(f"    beat {b + 1}: AI image failed; "
                    f"trying Openverse")
                image, attribution = images_mod.fetch_image(
                    query, cache_dir)
                if image:
                    item = {"path": image, "share": share, "kind": "image"}
                    beat_stats["openverse"] += 1
                    attributions.append(attribution)
                    log(f"      -> Openverse image")
            if item is None:
                # Title card (one per section, shared by its beats).
                if card_path is None:
                    card_path = os.path.join(workdir, f"card_{i:02d}.jpg")
                    # Title only, no subtitle text: the burned-in
                    # captions already carry the narration, and card
                    # text would overlap them.
                    images_mod.make_title_card(
                        section["title"] or args.title, "",
                        card_path, args.lang, variant=i)
                item = {"path": card_path, "share": share, "kind": "image"}
                beat_stats["title_card"] += 1
                attributions.append({"query": query,
                                     "source": "title-card"})
                log(f"      -> title card (no visual found)")
            plan.append(item)
            beat_stats["total"] += 1
        section_plans.append(plan)
        job.sections_done = i
        job.stage("images", 0.42 + 0.08 * i / len(parsed),
                  f"Step 2/5: visuals ({i}/{len(parsed)})")
    stock_n = (beat_stats["pexels"] + beat_stats["pixabay"]
               + beat_stats["wikimedia"])
    log(f"  beats: {beat_stats['total']} total — {stock_n} stock video, "
        f"{beat_stats['ai_image']} AI image, "
        f"{beat_stats['openverse']} Openverse, "
        f"{beat_stats['title_card']} title card")

    # 4. Captions: per-section ASS files for burn-in (exact 1080p sizing)
    # plus one global SRT sidecar for YouTube upload.
    job.stage("captions", 0.50, "Step 3/5: building captions")
    caption_font = config["caption_font"].get(
        args.lang, config["caption_font"]["en"])
    global_captions = []
    section_ass = []
    offset = 0.0
    for i, section in enumerate(parsed, 1):
        captions = section_mod.split_into_captions(
            section["text"], segment_durs[i - 1])
        ass_path = os.path.join(workdir, f"cap_{i:02d}.ass")
        # Text callout: the section's key line, top of frame for the
        # first 3s of the section (never overlaps the bottom captions).
        callout_text = section_mod.section_callout(section)
        callout = None
        if callout_text:
            callout = (callout_text, 0.0,
                       min(3.0, segment_durs[i - 1]))
        section_mod.write_ass(captions, ass_path, caption_font,
                              font_size=config["caption_size"],
                              callout=callout)
        section_ass.append(ass_path)
        for start, end, text in captions:
            global_captions.append((offset + start, offset + end, text))
        offset += segment_durs[i - 1]
    srt_out = os.path.join(out_dir, f"{stem}.srt")
    section_mod.write_srt(global_captions, srt_out)
    log(f"  captions: {len(global_captions)} cues -> {srt_out}")

    # 5. Music bed for the full length (or silence if no music).
    if music and os.path.exists(music):
        full_music = os.path.join(workdir, "music_full.wav")
        build_music_track(music, total_duration, full_music)
    else:
        if music:
            log(f"  WARNING: music bed not found ({music}); no music")
        full_music = os.path.join(workdir, "music_full.wav")
        render_mod.run_command([
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
            "-t", str(total_duration), full_music,
        ], "silent music track")

    # 6. Render each section, then concat.
    job.stage("render", 0.55, "Step 4/5: rendering sections")
    segments = []
    offset = 0.0
    motion_counter = 0
    for i in range(1, len(parsed) + 1):
        progress = 0.55 + 0.35 * (i - 1) / len(parsed)
        job.sections_done = i - 1
        job.stage("render", progress,
                  f"Step 4/5: rendering section {i}/{len(parsed)}")
        wav = os.path.join(sections_dir, f"section_{i:02d}.wav")
        segment, used = render_segment(
            job, i, section_plans[i - 1], wav, full_music, offset,
            segment_durs[i - 1], section_ass[i - 1], config,
            args.lang, motion_counter,
            narration_gain=narration_gain)
        motion_counter += used
        segments.append(segment)
        offset += segment_durs[i - 1]

    job.sections_done = len(parsed)
    job.stage("concat", 0.92, "Step 5/5: joining sections + loudness check")
    concat_list = os.path.join(workdir, "concat.txt")
    with open(concat_list, "w", encoding="utf-8") as handle:
        for segment in segments:
            handle.write(f"file '{segment}'\n")
    render_mod.run_command([
        "ffmpeg", "-y", "-v", "error",
        "-f", "concat", "-safe", "0", "-i", concat_list,
        "-c", "copy", out_path,
    ], "concat")

    # 7. Loudness check (mean ~-17 dB, max < -2 dB); one audio-only fix pass.
    # The fix is capped both in size and by the peak headroom, so the
    # final max stays under -2 dB.
    mean_db, max_db = render_mod.volumedetect(out_path)
    log(f"  loudness: mean {mean_db:.1f} dB, max {max_db:.1f} dB")
    adjust = TARGET_MEAN_DB - mean_db
    adjust = max(-MAX_ADJUST_DB,
                 min(MAX_ADJUST_DB, adjust, -2.5 - max_db))
    if abs(adjust) > 1.0:
        linear = 10 ** (adjust / 20.0)
        fixed = os.path.join(workdir, "video_fixed.mp4")
        render_mod.run_command([
            "ffmpeg", "-y", "-v", "error", "-i", out_path,
            "-c:v", "copy", "-af", f"volume={linear:.4f}", fixed,
        ], "loudness fix")
        os.replace(fixed, out_path)
        mean_db, max_db = render_mod.volumedetect(out_path)
        log(f"  loudness after fix ({adjust:+.1f} dB): "
            f"mean {mean_db:.1f} dB, max {max_db:.1f} dB")

    # 8. Attributions + summary.
    attr_out = os.path.join(out_dir, f"{stem}.attributions.json")
    with open(attr_out, "w", encoding="utf-8") as handle:
        json.dump(attributions, handle, ensure_ascii=False, indent=2)
    final_info = render_mod.probe_media(out_path)
    summary = {
        "title": args.title,
        "language": args.lang,
        "voice": voice,
        "style": style,
        "sections": len(parsed),
        "beats": beat_stats,
        "words": total_words,
        "duration_sec": round(total_duration, 1),
        "duration_min": round(total_duration / 60, 2),
        "output": out_path,
        "captions": srt_out,
        "attributions": attr_out,
        "loudness_mean_db": mean_db,
        "loudness_max_db": max_db,
        "render_seconds": round(time.time() - started, 1),
    }
    summary_out = os.path.join(out_dir, f"{stem}.summary.json")
    with open(summary_out, "w", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2)

    if not args.keep_segments:
        for segment in segments:
            try:
                os.remove(segment)
            except OSError:
                pass

    job.stage("done", 1.0, "Done")
    elapsed = time.time() - started
    log(f"DONE in {elapsed:.0f}s ({elapsed / 60:.1f} min): {out_path}")
    log(f"  {total_duration:.1f}s video, {len(parsed)} sections, "
        f"mean {mean_db:.1f} dB / max {max_db:.1f} dB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
