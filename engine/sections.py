"""Script parsing and caption generation for Video Studio.

Script format (plain UTF-8 .txt):
- Sections are separated by blank lines, a line with only ---, or a
  line starting with "## " (which also sets that section's title).
- A line starting with "#img: <query>" at the start of a section is an
  image-search hint for that section (it is NOT narrated).
- Any other line starting with "#" is a comment and is ignored.
"""

import re

SENTENCE_SPLIT = re.compile(r"(?<=[\u0964?!.])\s+")


def parse_script(text):
    """Parse script text into sections.

    Returns a list of dicts: {"title": str, "text": str, "img_query": str}.
    """
    sections = []
    current = {"title": "", "parts": [], "img_query": ""}

    def flush():
        nonlocal current
        body = " ".join(current["parts"]).strip()
        if body:
            sections.append({
                "title": current["title"],
                "text": body,
                "img_query": current["img_query"],
            })
        current = {"title": "", "parts": [], "img_query": ""}

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line == "" or line == "---":
            flush()
            continue
        if line.startswith("## "):
            flush()
            current["title"] = line[3:].strip()
            continue
        if line.startswith("#img:"):
            current["img_query"] = line[5:].strip()
            continue
        if line.startswith("#"):
            continue
        current["parts"].append(line)
    flush()
    return sections


def clean_script_text(sections):
    """Rebuild a narration-only script (no titles/hints) for Kokoro.

    Sections stay separated by blank lines, so Kokoro's own section
    splitter produces the exact same numbering (section_01..NN).
    """
    return "\n\n".join(s["text"] for s in sections) + "\n"


def split_sentences(text):
    """Split text into sentences (Devanagari danda + Latin punctuation)."""
    parts = [p.strip() for p in SENTENCE_SPLIT.split(text) if p.strip()]
    return parts or [text]


def wrap_text(text, max_chars=42):
    """Word-wrap text into lines of at most max_chars characters."""
    lines, current = [], ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if len(candidate) <= max_chars:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines or [""]


def split_into_captions(text, duration, max_chars=48, max_lines=2):
    """Split a section's text into timed caption chunks.

    Time is distributed across chunks in proportion to character count.
    Returns a list of (start_sec, end_sec, text) with times relative to
    the section start (0 .. duration).
    """
    chunks = []
    for sentence in split_sentences(text):
        lines = wrap_text(sentence, max_chars)
        for i in range(0, len(lines), max_lines):
            chunks.append("\n".join(lines[i:i + max_lines]))
    # Merge a tiny trailing chunk into the previous one (when it still
    # fits) so a caption never ends on a lone word.
    merged = []
    for chunk in chunks:
        plain = chunk.replace("\n", " ")
        if merged and len(plain) < 12:
            candidate = merged[-1].replace("\n", " ") + " " + plain
            relines = wrap_text(candidate, max_chars)
            if len(relines) <= max_lines:
                merged[-1] = "\n".join(relines)
                continue
        merged.append(chunk)
    chunks = merged
    total = sum(len(c.replace("\n", " ")) for c in chunks) or 1
    captions, t = [], 0.0
    for chunk in chunks:
        chunk_dur = duration * len(chunk.replace("\n", " ")) / total
        captions.append((t, min(t + chunk_dur, duration), chunk))
        t += chunk_dur
    if captions:
        start, _, chunk = captions[-1]
        captions[-1] = (start, duration, chunk)
    return captions


def split_into_beats(text, duration, target_beat=7.0, min_beat=3.0):
    """Split a section's text into visual beats of ~target_beat seconds.

    Beats are groups of consecutive caption chunks (see
    split_into_captions), so beat boundaries always fall on caption
    boundaries and the visual always changes in step with the
    narration. The chunks used here are fine-grained (one short line
    each, ~2s) so grouping them to ~target_beat seconds never
    overshoots far past the target; a tiny trailing beat
    (< min_beat) is merged into the previous one.
    Returns a list of {"text", "start", "end", "duration"} dicts with
    times relative to the section start (0 .. duration).
    """
    chunks = split_into_captions(text, duration, max_chars=32,
                                 max_lines=1)
    beats = []
    current = []

    def flush(group):
        if not group:
            return
        text_parts = [c[2].replace("\n", " ") for c in group]
        beats.append({
            "text": " ".join(text_parts),
            "start": group[0][0],
            "end": group[-1][1],
            "duration": group[-1][1] - group[0][0],
        })

    for chunk in chunks:
        if current:
            cur_dur = chunk[0] - current[0][0]
            if cur_dur >= target_beat:
                flush(current)
                current = []
        current.append(chunk)
    flush(current)
    if len(beats) > 1 and beats[-1]["duration"] < min_beat:
        last = beats.pop()
        beats[-1]["text"] = f"{beats[-1]['text']} {last['text']}"
        beats[-1]["end"] = last["end"]
        beats[-1]["duration"] = last["end"] - beats[-1]["start"]
    return beats


def section_callout(section, max_words=8):
    """The section's key line for the on-screen text callout: the first
    sentence, used whole when it is short (<= max_words + 2 words) and
    otherwise truncated to max_words with an ellipsis. A very short
    first sentence (e.g. a one-word exclamation like "रुकिए!") is
    extended with the next sentence so the callout says something.
    Returns "" when the section has no text."""
    sentences = split_sentences(section.get("text", ""))
    if not sentences:
        return ""
    first = sentences[0].strip()
    index = 1
    while len(first.split()) < 4 and index < len(sentences):
        first = f"{first} {sentences[index].strip()}"
        index += 1
    words = first.split()
    if len(words) <= max_words + 2:
        return first
    return " ".join(words[:max_words]) + " …"


def srt_timestamp(seconds):
    """Format seconds as an SRT timestamp (HH:MM:SS,mmm)."""
    total_ms = int(round(seconds * 1000))
    hours, rem = divmod(total_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, ms = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def write_srt(captions, path):
    """Write [(start, end, text), ...] as an .srt file."""
    with open(path, "w", encoding="utf-8") as handle:
        for index, (start, end, text) in enumerate(captions, 1):
            handle.write(
                f"{index}\n{srt_timestamp(start)} --> {srt_timestamp(end)}\n"
                f"{text}\n\n"
            )


def ass_timestamp(seconds):
    """Format seconds as an ASS timestamp (H:MM:SS.cc)."""
    total_cs = int(round(seconds * 100))
    hours, rem = divmod(total_cs, 360_000)
    minutes, rem = divmod(rem, 6_000)
    secs, cs = divmod(rem, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{cs:02d}"


def write_ass(captions, path, font_name, font_size=40, margin_v=70,
              callout=None):
    """Write [(start, end, text), ...] as a styled .ass file.

    ASS is used for burn-in (instead of SRT) because it carries its own
    PlayRes: with SRT, libass assumes a 384x288 canvas and scales the
    font ~3.75x at 1080p, making captions huge and misplaced. With
    PlayRes 1920x1080 the size and margins are exact.

    callout is an optional (text, start, end) tuple: a bold key line
    shown top-centre (alignment 8, MarginV 100) inside a
    semi-transparent box — far above the bottom caption band, so it
    never overlaps the captions. It uses the same font as the
    captions, so Devanagari renders correctly.
    """
    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        "PlayResX: 1920\n"
        "PlayResY: 1080\n"
        "ScaledBorderAndShadow: yes\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{font_name},{font_size},"
        "&H00FFFFFF,&H000000FF,&H80000000,&H00000000,"
        "0,0,0,0,100,100,0,0,1,2,1,2,40,40,"
        f"{margin_v},1\n"
        f"Style: Callout,{font_name},{font_size + 14},"
        "&H00FFFFFF,&H000000FF,&H80000000,&H80000000,"
        "1,0,0,0,100,100,0,0,3,2,0,8,60,60,100,1\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, "
        "MarginV, Effect, Text\n"
    )
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(header)
        for start, end, text in captions:
            safe = (text.replace("\\", "\\\\")
                    .replace("{", "\\{").replace("}", "\\}")
                    .replace("\n", "\\N"))
            handle.write(
                f"Dialogue: 0,{ass_timestamp(start)},{ass_timestamp(end)},"
                f"Default,,0,0,0,,{safe}\n"
            )
        if callout:
            text, start, end = callout
            if text and end > start:
                safe = (text.replace("\\", "\\\\")
                        .replace("{", "\\{").replace("}", "\\}"))
                handle.write(
                    f"Dialogue: 1,{ass_timestamp(start)},"
                    f"{ass_timestamp(end)},Callout,,0,0,0,,{safe}\n"
                )
