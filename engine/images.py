"""Visuals for Video Studio.

Primary source: Openverse (api.openverse.org) — free, no API key needed
for basic use. Only images with permissive (commercial-use) licences are
used, and attribution is recorded for every image.

Fallback: a clean title-card image generated locally with PIL
(gradient + section title text), used when Openverse has nothing useful
for a section or the network is unavailable.

Rate limits (anonymous Openverse): ~20 requests/minute, ~200/day.
We stay well under that: one request per section, 3s apart, with a
disk cache keyed by search query.
"""

import glob
import hashlib
import json
import os
import re
import subprocess
import time

import requests
from PIL import Image, ImageDraw, ImageFont

OPENVERSE_API = "https://api.openverse.org/v1/images/"
REQUEST_SLEEP = 3.0  # seconds between Openverse requests
USER_AGENT = "VideoStudio/1.0 (free open-source video generator)"

_last_request_at = 0.0

# --- keyword extraction -------------------------------------------------

HINDI_STOPWORDS = set(
    "है हैं था थी थे हो होगा होगी होंगे यह वह ये वो जो तो भी और या पर से में "
    "ने को का की के लिए अपना अपनी अपने इस उस इन उन जब तब अब फिर क्या क्यों "
    "कैसे कहाँ कितना कोई कुछ सब नहीं ना ही तक द्वारा जैसा जैसे बिना साथ "
    "एक दो तीन चार पाँच छह सात आठ नौ दस पहले बाद ऊपर नीचे बाहर अंदर "
    "कर करते करता करती किया हुआ हुई हुए जाने जाता जाती रहे रहा रही बहुत "
    "आज कल यहाँ वहाँ जिसका जिसकी जिनका मेरा तेरा हमारा तुम्हारा उनका".split()
)

ENGLISH_STOPWORDS = set(
    "the a an and or but of to in on at for with by from as is are was were "
    "be been it its this that these those you your we our they their he she "
    "his her not no yes if then than so very just about into over after "
    "before between one two three first second".split()
)

# Common Hindi nouns -> English, so Openverse (mostly English-tagged)
# returns relevant images for Hindi scripts.
HI_EN = {
    "अंतरिक्ष": "space", "तारा": "star", "तारे": "stars", "ग्रह": "planet",
    "सूरज": "sun", "सूर्य": "sun", "चाँद": "moon", "चंद्रमा": "moon",
    "धरती": "earth", "पृथ्वी": "earth", "आकाश": "sky", "बादल": "clouds",
    "पहाड़": "mountain", "पर्वत": "mountain", "नदी": "river", "समुद्र": "ocean",
    "सागर": "sea", "पानी": "water", "जंगल": "forest", "पेड़": "tree",
    "फूल": "flower", "जानवर": "animals", "पक्षी": "birds", "मछली": "fish",
    "शेर": "lion", "हाथी": "elephant", "कुत्ता": "dog", "बिल्ली": "cat",
    "घर": "house", "शहर": "city", "गाँव": "village", "सड़क": "road",
    "गाड़ी": "car", "ट्रेन": "train", "हवाई": "airplane", "जहाज": "ship",
    "खाना": "food", "सब्जी": "vegetables", "फल": "fruits", "चाय": "tea",
    "कॉफी": "coffee", "बच्चा": "child", "बच्चे": "children", "स्कूल": "school",
    "किताब": "book", "कंप्यूटर": "computer", "मोबाइल": "smartphone",
    "फोन": "phone", "इंटरनेट": "internet", "विज्ञान": "science",
    "इतिहास": "history", "मंदिर": "temple", "मस्जिद": "mosque",
    "खेल": "sports", "क्रिकेट": "cricket", "फुटबॉल": "football",
    "संगीत": "music", "गाना": "song", "नृत्य": "dance", "फिल्म": "movie",
    "पैसा": "money", "बाजार": "market", "दुकान": "shop", "खेत": "farm",
    "किसान": "farmer", "डॉक्टर": "doctor", "अस्पताल": "hospital",
    "सूर्यास्त": "sunset", "सुबह": "morning", "रात": "night",
    "बारिश": "rain", "बर्फ": "snow", "आग": "fire", "हवा": "wind",
    "ब्लैक": "black", "होल": "hole", "गैलेक्सी": "galaxy",
    "रॉकेट": "rocket", "अंतरिक्ष यात्री": "astronaut", "चंद्रमा": "moon",
    "धरती": "earth", "ब्रह्मांड": "universe", "प्रकाश": "light",
    "समय": "time", "तकनीक": "technology", "रोबोट": "robot",
}

WORD_RE = re.compile(r"[\w\u0900-\u097F]+", re.UNICODE)


def extract_keywords(text, lang="hi", max_words=4):
    """Pull search keywords out of a section's text.

    For Hindi, known nouns are translated to English; anything else is
    dropped (Openverse tags are mostly English), so a section with no
    known nouns yields no keywords and falls back to the title card.
    """
    words = WORD_RE.findall(text)
    if lang == "hi":
        keywords = []
        for word in words:
            # WORD_RE keeps the Devanagari danda (।, U+0964), so a word
            # ending a sentence would never match its map key — strip
            # sentence punctuation before lookup.
            word = word.rstrip("।?!.,;:")
            if word in HI_EN and HI_EN[word] not in keywords:
                keywords.append(HI_EN[word])
        # Also keep distinctive long Hindi words out — they rarely match
        # English tags, so only the translated ones are used.
        return keywords[:max_words]
    keywords = []
    for word in words:
        lower = word.lower()
        if lower not in ENGLISH_STOPWORDS and len(lower) > 2 \
                and lower not in keywords:
            keywords.append(lower)
    # Prefer longer (more distinctive) words first.
    keywords.sort(key=len, reverse=True)
    return keywords[:max_words]


# --- Openverse ----------------------------------------------------------

def _rate_limit():
    global _last_request_at
    now = time.time()
    wait = REQUEST_SLEEP - (now - _last_request_at)
    if wait > 0:
        time.sleep(wait)
    _last_request_at = time.time()


def search_openverse(query):
    """Search Openverse for one query. Returns a list of result dicts."""
    _rate_limit()
    response = requests.get(
        OPENVERSE_API,
        params={
            "q": query,
            "license_type": "commercial",
            "page_size": 8,
            "filter_dead": "false",
        },
        headers={"User-Agent": USER_AGENT},
        timeout=30,
    )
    response.raise_for_status()
    return response.json().get("results", [])


def _rank_results(results):
    """Rank candidate images: landscape first, widest first.

    No-Derivatives (ND) licences are excluded — the Ken Burns zoom and
    crop transform the image, which ND does not allow.
    """
    usable = [r for r in results
              if "nd" not in (r.get("license") or "").lower()]
    pool = usable or results
    landscape = [r for r in pool
                 if r.get("width") and r.get("height")
                 and r["width"] >= r["height"]]
    pool = landscape or pool
    return sorted(pool, key=lambda r: r.get("width") or 0, reverse=True)


def _download_image(url, dest_path):
    response = requests.get(url, headers={"User-Agent": USER_AGENT},
                            timeout=60)
    response.raise_for_status()
    with open(dest_path, "wb") as handle:
        handle.write(response.content)
    # Verify it is a real image and normalise to RGB JPEG.
    with Image.open(dest_path) as img:
        img.verify()
    with Image.open(dest_path) as img:
        rgb = img.convert("RGB")
        rgb.save(dest_path, "JPEG", quality=90)
    return dest_path


def fetch_image(query, cache_dir):
    """Fetch one image for a query (cached). Returns (path, attribution)
    or (None, None) when nothing usable was found. Tries several
    candidates: a dead link (e.g. a deleted Flickr photo) falls through
    to the next result instead of failing the section."""
    key = hashlib.sha1(query.encode("utf-8")).hexdigest()[:12]
    cached = os.path.join(cache_dir, f"ov_{key}.jpg")
    meta_path = os.path.join(cache_dir, f"ov_{key}.json")
    if os.path.exists(cached) and os.path.exists(meta_path):
        with open(meta_path, encoding="utf-8") as handle:
            return cached, json.load(handle)
    try:
        results = search_openverse(query)
    except Exception as exc:  # network/API failure -> caller falls back
        print(f"    Openverse search failed for '{query}': {exc}")
        return None, None
    os.makedirs(cache_dir, exist_ok=True)
    for result in _rank_results(results):
        if not result.get("url"):
            continue
        try:
            _download_image(result["url"], cached)
        except Exception as exc:
            print(f"    Image download failed "
                  f"({result.get('url')}): {exc}")
            if os.path.exists(cached):
                os.remove(cached)
            continue
        attribution = {
            "query": query,
            "title": result.get("title") or "",
            "creator": result.get("creator") or "",
            "license": result.get("license") or "",
            "license_url": result.get("license_url") or "",
            "source_url": result.get("foreign_landing_url") or "",
            "attribution_text": result.get("attribution") or "",
        }
        with open(meta_path, "w", encoding="utf-8") as handle:
            json.dump(attribution, handle, ensure_ascii=False, indent=2)
        return cached, attribution
    return None, None


# --- title-card fallback ------------------------------------------------

FONT_CANDIDATES = {
    "hi": [
        "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Bold.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf",
    ],
    "en": [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
    ],
}

GRADIENTS = [
    ((18, 32, 62), (58, 28, 88)),     # navy -> purple
    ((12, 52, 48), (16, 34, 66)),     # teal -> navy
    ((66, 26, 26), (32, 20, 52)),     # maroon -> dark purple
    ((24, 44, 24), (18, 40, 58)),     # green -> navy
]


def _fc_match_file(query):
    """Resolve a fontconfig query to a .ttf path, or None.

    fontconfig knows the actually-installed fonts, so this is the
    authoritative lookup; the hardcoded paths below are only a
    fallback for systems without fontconfig.
    """
    try:
        result = subprocess.run(
            ["fc-match", "--format", "%{file}", query],
            capture_output=True, text=True, timeout=10)
        path = result.stdout.strip()
        if path and os.path.exists(path):
            return path
    except Exception:
        pass
    return None


def resolve_font_path(lang, bold=True):
    """Find a .ttf that can render `lang`'s script.

    Chain for Hindi: fc-match "Noto Sans Devanagari" (bold/regular) ->
    known install paths -> any installed NotoSansDevanagari file ->
    any font covering the Devanagari block (U+0905) -> None.
    For English: fc-match "DejaVu Sans" -> known paths -> None.
    """
    if lang == "hi":
        weight = "bold" if bold else "regular"
        candidates = [
            _fc_match_file(f"Noto Sans Devanagari:weight={weight}"),
            _fc_match_file("Noto Sans Devanagari"),
        ]
        candidates.extend(FONT_CANDIDATES["hi"])
        candidates.extend(sorted(glob.glob(
            "/usr/share/fonts/**/NotoSansDevanagari-*.ttf",
            recursive=True)))
        # Last resort: any font with Devanagari coverage.
        candidates.append(_fc_match_file(":charset=0905"))
    else:
        weight = "bold" if bold else "regular"
        candidates = [
            _fc_match_file(f"DejaVu Sans:weight={weight}"),
            _fc_match_file("DejaVu Sans"),
        ]
        candidates.extend(FONT_CANDIDATES["en"])
    for path in candidates:
        if path and os.path.exists(path):
            return path
    return None


def _load_font(lang, size, bold=True):
    """Load a font for `lang` at `size`, or None when nothing usable
    was found. For Hindi this NEVER returns a Latin-only font (that is
    what drew tofu boxes on old title cards): the caller draws the
    card without text instead."""
    path = resolve_font_path(lang, bold=bold)
    if path:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    if lang == "hi":
        print("    WARNING: no Devanagari-capable font found — "
              "title card will have no text (no tofu boxes)")
        return None
    try:
        return ImageFont.truetype(FONT_CANDIDATES["en"][0], size)
    except OSError:
        return ImageFont.load_default()


def _wrap_for_card(draw, text, font, max_width):
    words, lines, current = text.split(), [], ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if draw.textlength(candidate, font=font) <= max_width:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def make_title_card(title, subtitle, out_path, lang="hi", variant=0):
    """Generate a 1920x1080 title-card image with PIL."""
    width, height = 1920, 1080
    top, bottom = GRADIENTS[variant % len(GRADIENTS)]
    card = Image.new("RGB", (width, height))
    pixels = card.load()
    for y in range(height):
        ratio = y / height
        pixels_row = tuple(
            int(top[i] + (bottom[i] - top[i]) * ratio) for i in range(3)
        )
        for x in range(width):
            pixels[x, y] = pixels_row
    draw = ImageDraw.Draw(card)
    # Accent bar on the left.
    draw.rectangle([140, 380, 156, 700], fill=(255, 196, 0))
    title_font = _load_font(lang, 96)
    if title_font is None:
        # No usable font for this script: a textless card beats tofu.
        card.save(out_path, "JPEG", quality=90)
        return out_path
    lines = _wrap_for_card(draw, title, title_font, 1500)
    y = 420
    for line in lines[:3]:
        draw.text((200, y), line, font=title_font, fill=(255, 255, 255))
        y += 120
    if subtitle:
        sub_font = _load_font(lang, 44)
        if sub_font is not None:
            sub_lines = _wrap_for_card(draw, subtitle, sub_font, 1500)
            y += 30
            for line in sub_lines[:2]:
                draw.text((200, y), line, font=sub_font,
                          fill=(210, 210, 210))
                y += 60
    card.save(out_path, "JPEG", quality=90)
    return out_path
