"""AI-generated section images for Video Studio v2 (Pollinations.ai).

Primary visual source: Pollinations.ai image API (free, no API key).
For each script section we translate the first 1-2 Hindi sentences to
English (Google Translate gtx endpoint, also free/no key), build a
prompt "<scene>, <style suffix>, detailed, no text, no watermark" and
generate a 1280x720 image with the flux model.

Known Pollinations quirks handled here:
- Output is capped at ~1024px (a 1280x720 request returns 1024x576).
- `nologo=true` does NOT remove the small "pollinations.ai" watermark
  in the bottom-right corner, so the bottom 8% is cropped off every
  image right after download.
- Images are upscaled to 1920x1080 (Lanczos + mild unsharp) so the
  Ken Burns zoom in the renderer never sees a low-res source.

If generation fails (network/API), plan_section_images returns
(None, []) and the caller falls back to the existing Openverse fetch,
then to a title card.
"""

import os
import random
import sys
import time
import urllib.parse

import requests
from PIL import Image, ImageFilter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sections as section_mod
import images as images_mod

POLLINATIONS_API = "https://image.pollinations.ai/prompt/"
GTX_API = "https://translate.googleapis.com/translate_a/single"
MYMEMORY_API = "https://api.mymemory.translated.net/get"
USER_AGENT = "VideoStudio/1.0 (free open-source video generator)"

IMAGE_WIDTH, IMAGE_HEIGHT = 1920, 1080
WATERMARK_CROP_FRACTION = 0.08  # bottom strip removed (Pollinations logo)
LONG_SECTION_SECONDS = 25.0     # longer sections get 2 images
MAX_SCENE_CHARS = 400           # keep the prompt URL short
REQUEST_SLEEP = 1.0             # politeness gap between generations

# Style key -> prompt suffix. config.json can override/extend these
# (config["styles"][key]["suffix"]); these are the built-in defaults.
DEFAULT_STYLES = {
    "cinematic": "cinematic realistic photo, dramatic lighting, "
                 "high detail, sharp focus",
    "madhubani": "Madhubani painting, traditional Indian Madhubani folk "
                 "art from Bihar, flat vibrant colors, bold black outlines, "
                 "intricate geometric patterns, red yellow green orange "
                 "palette, detailed folk motifs, handmade paper texture",
    "realistic": "photorealistic photo, natural light, high detail, "
                 "sharp focus",
    "cartoon3d": "3D animated movie style, Pixar-like, vibrant colors, "
                 "soft studio lighting, high detail",
    "anime": "anime style illustration, vibrant colors, detailed cel "
             "shading, Japanese animation",
    "watercolor": "watercolor painting style, soft brush strokes, "
                  "gentle colors, artistic, detailed",
    "oilpainting": "classical oil painting style, rich colors, visible "
                   "brushwork, detailed",
    "cyberpunk": "cyberpunk style, neon lights, futuristic glow, "
                 "dramatic lighting, high detail",
}

# Styles whose prompt needs a template instead of a plain suffix.
# "{scene}" is replaced with the translated scene description.
# Madhubani is scene-FIRST: leading with the style ("Madhubani folk
# art painting depicting...") lets the model drift to generic folk
# motifs (deities, mandalas); leading with the concrete scene grounds
# the subject while the style words still drive the look (verified
# 2026-11-22: scene-first gave recognisable planes-over-sea where the
# style-first prompt gave a murky abstract for the same scene).
DEFAULT_TEMPLATES = {
    "madhubani": "{scene}, Madhubani folk art painting, traditional "
                 "Indian Madhubani style from Bihar, flat vibrant colors, "
                 "bold black outlines, intricate geometric patterns, red "
                 "yellow green orange, no people, no faces, no woman, "
                 "no man, no deity, no human figure, no portrait, "
                 "no text, no watermark",
}

# Per-process translation cache (one process == one render job).
_translation_cache = {}


def style_suffix(style, config=None):
    """Prompt suffix for a style key (config overrides built-ins)."""
    if config:
        entry = (config.get("styles") or {}).get(style)
        if isinstance(entry, dict) and entry.get("suffix"):
            return entry["suffix"]
        if isinstance(entry, str) and entry:
            return entry
    return DEFAULT_STYLES.get(style, DEFAULT_STYLES["cinematic"])


def style_template(style, config=None):
    """Template string for a style key, or None (config overrides
    built-ins)."""
    if config:
        entry = (config.get("styles") or {}).get(style)
        if isinstance(entry, dict) and entry.get("template"):
            return entry["template"]
    return DEFAULT_TEMPLATES.get(style)


def build_prompt(scene_en, style, config=None):
    """Build the full image prompt for a scene + style.

    Template styles (Madhubani) use their template; everything else
    is "<scene>, <style suffix>, detailed, no text, no watermark".
    """
    template = style_template(style, config)
    if template:
        return template.format(scene=scene_en)
    return (f"{scene_en}, {style_suffix(style, config)}, detailed, "
            f"no text, no watermark")


# --- translation (Hindi -> English, free endpoints) ------------------
# Chain: Google gtx -> MyMemory -> keyword fallback (offline).
# NOTE (2026-11-22): gtx returns HTTP 429 persistently from this VM's
# shared egress IP, so MyMemory does the real work here. gtx is kept
# first because it works fine from most other machines (e.g. the
# user's own PC), and it fails fast on 429.
# MyMemory's anonymous quota is ~5000 chars/day/IP; a long video can
# exhaust it, in which case prompts fall back to keywords (still
# on-topic, just less specific).

def _translate_gtx(text):
    """Google Translate gtx endpoint. Returns English text or None.
    Gives up immediately on 429 (rate limit) instead of retrying."""
    for attempt in range(2):
        try:
            response = requests.get(
                GTX_API,
                params={"client": "gtx", "sl": "hi", "tl": "en",
                        "dt": "t", "q": text},
                headers={"User-Agent": USER_AGENT},
                timeout=20,
            )
            if response.status_code == 429:
                print("    gtx rate-limited (429); trying MyMemory")
                return None
            response.raise_for_status()
            data = response.json()
            parts = [seg[0] for seg in data[0] if seg and seg[0]]
            result = " ".join(parts).strip()
            if result:
                return result
            return None
        except Exception as exc:
            print(f"    gtx failed (attempt {attempt + 1}): {exc}")
            time.sleep(2)
    return None


def _translate_mymemory(text):
    """MyMemory free API (no key, ~5000 chars/day/IP anonymous).
    Returns English text or None."""
    for attempt in range(2):
        try:
            response = requests.get(
                MYMEMORY_API,
                params={"q": text, "langpair": "hi|en"},
                headers={"User-Agent": USER_AGENT},
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()
            if data.get("responseStatus") != 200:
                print(f"    MyMemory status {data.get('responseStatus')}: "
                      f"{data.get('responseDetails')}")
                return None
            result = ((data.get("responseData") or {})
                      .get("translatedText", "") or "").strip()
            if result and "MYMEMORY WARNING" not in result \
                    and "QUERY LENGTH" not in result:
                return result
            return None
        except Exception as exc:
            print(f"    MyMemory failed (attempt {attempt + 1}): {exc}")
            time.sleep(2)
    return None


def translate_hi_en(text):
    """Translate Hindi text to English (gtx, then MyMemory).
    Returns the English text, or None on failure. Cached per job."""
    key = (text or "").strip()
    if not key:
        return None
    if key in _translation_cache:
        return _translation_cache[key]
    time.sleep(0.5)  # politeness gap between translation requests
    result = _translate_gtx(key)
    if not result:
        result = _translate_mymemory(key)
    _translation_cache[key] = result
    return result


# Extra Hindi->English nouns for the offline keyword fallback, on top
# of images.HI_EN (which is left untouched). Includes plurals and
# inflected forms the base map misses (मछलियाँ, जहाज़ों, ...).
_EXTRA_HI_EN = {
    "गहराई": "deep", "रहस्य": "mystery", "त्रिभुज": "triangle",
    "बरमूडा": "Bermuda", "विमान": "airplane", "जहाज़": "ship",
    "लहरें": "waves", "प्रवाल": "coral", "चमक": "glow",
    "गुफा": "cave", "द्वीप": "island", "तूफान": "storm",
    "खाई": "trench", "अटलांटिक": "Atlantic", "प्रशांत": "Pacific",
    "जीव": "creatures", "शार्क": "shark", "व्हेल": "whale",
    "ऑक्टोपस": "octopus", "कछुआ": "turtle", "डूबा": "sunken",
    "नीला": "blue", "रोशनी": "light", "अंधेरा": "dark",
    "मछलियाँ": "fish", "झींगे": "shrimp", "चमकने": "glowing",
    "चमकता": "glowing", "चमकती": "glowing", "गायब": "vanishing",
    "गहरा": "deep", "गहरी": "deep", "विमानों": "airplanes",
    "जहाज़ों": "ships", "लहरों": "waves", "तट": "shore",
    "किनारा": "shore", "रात": "night", "नीली": "blue",
    "मूंगा": "coral", "डॉल्फिन": "dolphin", "जीवों": "creatures",
    "मछली": "fish", "खोजा": "explored", "दुनिया": "world",
    "पानी": "water", "तारे": "stars", "चाँद": "moon",
    "गैस": "gas", "मीथेन": "methane", "प्रजातियाँ": "species",
    "वैज्ञानिक": "scientist", "नौसेना": "navy",
}

# Merged Hindi->English keyword map (base + extras).
_KEYWORD_MAP = dict(images_mod.HI_EN)
_KEYWORD_MAP.update(_EXTRA_HI_EN)


def _keywords_fallback(scene_text, title):
    """Offline fallback: English keywords from the Hindi scene + title.
    Image models take keyword prompts well, so this still yields
    on-topic images when both translators are down."""
    keywords = []
    for word in images_mod.WORD_RE.findall(f"{scene_text} {title}"):
        # WORD_RE keeps the Devanagari danda (।, U+0964), so a word
        # ending a sentence ("झींगे।") would never match its map key
        # ("झींगे") — strip sentence punctuation before lookup.
        word = word.rstrip("।?!.,;:")
        if word in _KEYWORD_MAP and _KEYWORD_MAP[word] not in keywords:
            keywords.append(_KEYWORD_MAP[word])
    return " ".join(keywords[:6])


def _score_sentence(sentence, lang):
    """How visually concrete a sentence is: number of keyword hits.
    Used to pick the most picturable sentences for the image prompt
    instead of blindly taking the first 1-2 (which are often abstract
    narration like "there is so much darkness and pressure...")."""
    if lang == "hi":
        return sum(1 for w in images_mod.WORD_RE.findall(sentence)
                   if w in _KEYWORD_MAP)
    words = images_mod.WORD_RE.findall(sentence.lower())
    return sum(1 for w in words
               if w not in images_mod.ENGLISH_STOPWORDS and len(w) > 3)


def _best_sentences(sentences, lang, max_sentences=2):
    """Pick the most concrete sentences (most keyword hits), keeping
    original order, capped at MAX_SCENE_CHARS."""
    ranked = sorted(range(len(sentences)),
                    key=lambda i: (-_score_sentence(sentences[i], lang), i))
    chosen = sorted(ranked[:max_sentences])
    text = ""
    for i in chosen:
        candidate = f"{text} {sentences[i]}".strip()
        if len(candidate) > MAX_SCENE_CHARS and text:
            break
        text = candidate
    return text or _first_sentences(sentences)


# --- image generation ---------------------------------------------------

def _process_image(src_path, dest_path):
    """Crop the watermark strip, upscale to 1920x1080, sharpen, save."""
    with Image.open(src_path) as img:
        img.load()
        rgb = img.convert("RGB")
    width, height = rgb.size
    # Pollinations puts its small logo at the very bottom-right even
    # with nologo=true; cropping the bottom 8% removes it entirely.
    crop_height = int(height * (1.0 - WATERMARK_CROP_FRACTION))
    rgb = rgb.crop((0, 0, width, crop_height))
    # Cover-resize to 1920x1080 (scale up, centre-crop).
    scale = max(IMAGE_WIDTH / rgb.width, IMAGE_HEIGHT / rgb.height)
    new_w = max(IMAGE_WIDTH, int(round(rgb.width * scale)))
    new_h = max(IMAGE_HEIGHT, int(round(rgb.height * scale)))
    rgb = rgb.resize((new_w, new_h), Image.LANCZOS)
    left = (new_w - IMAGE_WIDTH) // 2
    top = (new_h - IMAGE_HEIGHT) // 2
    rgb = rgb.crop((left, top, left + IMAGE_WIDTH, top + IMAGE_HEIGHT))
    rgb = rgb.filter(ImageFilter.UnsharpMask(radius=2, percent=60,
                                             threshold=3))
    rgb.save(dest_path, "JPEG", quality=92)
    return dest_path


def generate_image(prompt, out_path, retries=2):
    """Generate one image from Pollinations. Returns out_path, or None
    after all retries fail."""
    seed = random.randint(0, 1_000_000)
    url = POLLINATIONS_API + urllib.parse.quote(prompt)
    params = {"width": 1280, "height": 720, "model": "flux",
              "nologo": "true", "seed": seed, "private": "true"}
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    for attempt in range(retries + 1):
        tmp_path = out_path + ".tmp"
        try:
            response = requests.get(url, params=params,
                                    headers={"User-Agent": USER_AGENT},
                                    timeout=180)
            response.raise_for_status()
            if "image" not in response.headers.get("Content-Type", ""):
                raise ValueError(
                    "Pollinations returned non-image content "
                    f"({response.headers.get('Content-Type')})")
            with open(tmp_path, "wb") as handle:
                handle.write(response.content)
            _process_image(tmp_path, out_path)
            os.remove(tmp_path)
            return out_path
        except Exception as exc:
            print(f"    Pollinations failed (attempt {attempt + 1}): {exc}")
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            time.sleep(3)
    return None


# --- per-section planning ------------------------------------------------

def _first_sentences(sentences, max_sentences=2):
    """Join the first 1-2 sentences, capped at MAX_SCENE_CHARS."""
    text = ""
    for sentence in sentences[:max_sentences]:
        candidate = f"{text} {sentence}".strip()
        if len(candidate) > MAX_SCENE_CHARS and text:
            break
        text = candidate
    return text


def _split_halves(sentences):
    """Split a sentence list into two halves by character count.
    Returns (first, second); second is [] when there is nothing
    sensible to split (single sentence)."""
    if len(sentences) < 2:
        return sentences, []
    total = sum(len(s) for s in sentences)
    first, chars = [], 0
    for sentence in sentences:
        if first and chars + len(sentence) > total / 2:
            break
        first.append(sentence)
        chars += len(sentence)
    second = sentences[len(first):]
    return first, second


def _scene_to_english(scene_text, title, lang):
    """Turn a Hindi (or English) scene description into an English
    prompt scene. Returns (scene_en, translated): translated is False
    when the scene is already just keywords (translation failed).
    Fallback chain for Hindi: scene translation -> title translation
    -> English keywords from scene+title -> generic."""
    if lang != "hi":
        return scene_text, True
    english = translate_hi_en(scene_text)
    if english:
        return english, True
    english = translate_hi_en(title)
    if english:
        return english, True
    keywords = _keywords_fallback(scene_text, title)
    if keywords:
        return keywords, False
    return "documentary scene", False


def _anchor_keywords(scene_text, title, lang):
    """Concrete English keywords for a scene, used to anchor the
    image subject (translated narration is often abstract, which lets
    the model drift to generic subjects)."""
    if lang == "hi":
        return _keywords_fallback(scene_text, title)
    return " ".join(images_mod.extract_keywords(
        f"{scene_text} {title}", "en", max_words=6))


def plan_beat_image(beat_text, title, lang, style, out_path,
                    config=None, reuse_images=False):
    """Generate one AI image for a single visual beat (v3).

    Returns (path, attribution) or (None, None) on failure — the
    caller then falls back to Openverse / title card for that beat.
    With reuse_images=True an existing out_path is kept instead of
    regenerating (lets a bad beat image be hand-replaced).
    """
    sentences = section_mod.split_sentences(beat_text)
    scene = _best_sentences(sentences, lang)
    if not scene:
        return None, None
    scene_en, translated = _scene_to_english(scene, title, lang)
    if translated:
        anchors = _anchor_keywords(scene, title, lang)
        if anchors:
            scene_en = f"{scene_en}, {anchors}"
    prompt = build_prompt(scene_en, style, config)
    if reuse_images and os.path.exists(out_path):
        print(f"    AI beat image ({style}): reusing existing image")
        return out_path, {
            "source": "pollinations-ai",
            "model": "flux",
            "style": style,
            "prompt": prompt,
            "query": scene_en,
            "reused": True,
        }
    print(f"    AI beat image ({style}): {scene_en[:70]}...")
    path = generate_image(prompt, out_path)
    if not path:
        return None, None
    time.sleep(REQUEST_SLEEP)
    return path, {
        "source": "pollinations-ai",
        "model": "flux",
        "style": style,
        "prompt": prompt,
        "query": scene_en,
    }


def plan_section_images(section, title, lang, style, ai_dir,
                        section_index, duration, config=None,
                        reuse_images=False):
    """Plan the AI images for one section.

    Returns (plan, attributions) where plan is a list of
    {"path":..., "share":...} dicts (shares sum to 1.0), or
    (None, []) when no AI image could be generated — the caller then
    falls back to Openverse / title card for the whole section.

    Long sections (>25s) get 2 images, prompted from the first-half
    vs second-half sentences, with screen time split by character
    count. If only one of the two succeeds, it covers the whole
    section instead of failing over to stock.

    With reuse_images=True, an existing ai_XX_Y.jpg is kept instead
    of regenerating — lets a bad image be hand-replaced and the video
    re-rendered without losing the good ones.
    """
    sentences = section_mod.split_sentences(section["text"])
    if duration > LONG_SECTION_SECONDS:
        first_half, second_half = _split_halves(sentences)
        groups = [g for g in (first_half, second_half) if g]
    else:
        groups = [sentences]
    plan, attributions = [], []
    weights = []
    for slot, group in enumerate(groups):
        # Most concrete sentences of the group (not blindly the first
        # 1-2): narration openers are often abstract, while a later
        # sentence names the actual visual ("glowing fish and
        # transparent shrimp").
        scene = _best_sentences(group, lang)
        if not scene:
            continue
        scene_en, translated = _scene_to_english(scene, title, lang)
        if translated:
            # Anchor the subject with concrete keywords: translated
            # narration is often abstract ("there is so much darkness
            # and pressure..."), and without an anchor the model drifts
            # to generic subjects (deities, decorative patterns).
            anchors = _anchor_keywords(scene, title, lang)
            if anchors:
                scene_en = f"{scene_en}, {anchors}"
        prompt = build_prompt(scene_en, style, config)
        out_path = os.path.join(
            ai_dir, f"ai_{section_index:02d}_{slot}.jpg")
        if reuse_images and os.path.exists(out_path):
            print(f"    AI image s{section_index:02d}.{slot} "
                  f"({style}): reusing existing image")
            plan.append({"path": out_path, "share": 0.0})
            weights.append(sum(len(s) for s in group))
            attributions.append({
                "source": "pollinations-ai",
                "model": "flux",
                "style": style,
                "prompt": prompt,
                "query": scene_en,
                "reused": True,
            })
            continue
        print(f"    AI image s{section_index:02d}.{slot} "
              f"({style}): {scene_en[:70]}...")
        path = generate_image(prompt, out_path)
        if path:
            plan.append({"path": path, "share": 0.0})
            weights.append(sum(len(s) for s in group))
            attributions.append({
                "source": "pollinations-ai",
                "model": "flux",
                "style": style,
                "prompt": prompt,
                "query": scene_en,
            })
        time.sleep(REQUEST_SLEEP)
    if not plan:
        return None, []
    total_weight = sum(weights) or len(plan)
    for item, weight in zip(plan, weights):
        item["share"] = weight / total_weight
    return plan, attributions
