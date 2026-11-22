"""Real moving stock footage for Video Studio v3 (NotebookLM style).

For each visual beat (~7s of narration) we try to find a real video
clip, so the finished video mixes moving footage with AI images
instead of showing one still image per ~25s section.

Source priority:
  1. Pexels Videos API — best coverage and quality, direct mp4 links.
     Free key (email-only signup, 200 req/hr), but a key is required,
     so this is used ONLY when PEXELS_API_KEY is set (env var or
     config.json). Without a key it is skipped silently.
  2. Pixabay Videos API — same pattern, used when PIXABAY_API_KEY is
     set. When no raw key is set, Pixabay is still tried through the
     vault-stored custom.pixabay connector helper (see
     PIXABAY_HELPER): the key lives in the Secure Vault and is never
     readable, so the helper is the only way to use it on this
     machine. The raw-key path stays FIRST because it is the deploy
     path (Hugging Face Space secret); the helper is a local-Muse
     fallback only, used only when the helper file exists.
  3. Wikimedia Commons — no key needed, free. 1080p originals are
     often 12-54MB, so originals over 20MB are skipped in favour of
     the smaller transcoded derivatives (480p webm) when available.

When no source has a usable clip, fetch_clip returns (None, None)
and the caller falls back to the v2 AI image for that beat.

Downloads are cached under the project cache dir (keyed by query)
and capped at MAX_DOWNLOAD_BYTES; oversized files are skipped, not
downloaded. prepare_beat_clip trims/loops a cached clip to the exact
beat length and scales it to 1920x1080 with NO audio (the clip's own
audio is muted — narration + music bed stay).
"""

import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.parse

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ai_images as ai_images_mod
import images as images_mod
import render as render_mod

PEXELS_API = "https://api.pexels.com/v1/videos/search"
PIXABAY_API = "https://pixabay.com/api/videos/"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"
USER_AGENT = "VideoStudio/1.0 (free open-source video generator)"
# Vault-stored Pixabay key, reached through the custom.pixabay
# connector helper (the key itself is never readable on this machine).
# Local-Muse fallback only: deploys use the raw PIXABAY_API_KEY path.
PIXABAY_HELPER = os.path.expanduser(
    "~/workspace/custom-connectors/custom.pixabay/bin/pixabay.py")

MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024   # never download a bigger file
PREFER_BYTES = 20 * 1024 * 1024         # prefer originals at/under this
SEARCH_SLEEP = 0.5                      # politeness gap between searches
MAX_CANDIDATES = 6                      # candidates tried per query
WIKI_COOLDOWN_S = 60                    # Wikimedia pause after 429s

_last_search_at = 0.0
# Circuit breaker for upload.wikimedia.org rate limits: after two
# consecutive downloads end in HTTP 429, Wikimedia is skipped for
# WIKI_COOLDOWN_S seconds (beats fall back to AI images instead of
# burning the render in retry loops). A successful download resets it.
_wiki_cooldown_until = 0.0
_wiki_consec_429 = 0


def _rate_limit():
    global _last_search_at
    now = time.time()
    wait = SEARCH_SLEEP - (now - _last_search_at)
    if wait > 0:
        time.sleep(wait)
    _last_search_at = time.time()


def _strip_tracking_params(url):
    """Remove query params (Commons adds ?utm_source=... tracking)."""
    parsed = urllib.parse.urlparse(url)
    return urllib.parse.urlunparse(
        (parsed.scheme, parsed.netloc, parsed.path, "", "", ""))


def _note_wiki_429():
    """Record a Wikimedia download that ended in HTTP 429. Two in a
    row trips the circuit breaker (60s cooldown)."""
    global _wiki_cooldown_until, _wiki_consec_429
    _wiki_consec_429 += 1
    if _wiki_consec_429 >= 2:
        _wiki_cooldown_until = time.time() + WIKI_COOLDOWN_S
        _wiki_consec_429 = 0
        print(f"    Wikimedia rate-limited repeatedly; pausing stock "
              f"video for {WIKI_COOLDOWN_S}s")


def _wiki_in_cooldown():
    return time.time() < _wiki_cooldown_until


def _download(url, dest_path, max_bytes=MAX_DOWNLOAD_BYTES):
    """Stream a file to dest_path, aborting over max_bytes.
    Returns dest_path, or None on failure / oversize.

    upload.wikimedia.org rate-limits this VM's shared egress IP with
    HTTP 429, so a 429 gets one retry after 5s. Retries are deliberately
    short: the throttle usually clears within seconds, and the
    circuit breaker (see _note_wiki_429) handles sustained limits —
    a beat must never burn minutes in retry loops when an AI image
    fallback is available."""
    global _wiki_consec_429
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    tmp_path = dest_path + ".tmp"
    backoffs = (0, 5)
    for attempt, backoff in enumerate(backoffs):
        if backoff:
            print(f"    rate-limited (429); retrying in {backoff}s")
            time.sleep(backoff)
        try:
            with requests.get(url, headers={"User-Agent": USER_AGENT},
                              timeout=180, stream=True) as response:
                if response.status_code == 429:
                    if attempt < len(backoffs) - 1:
                        continue
                    raise RuntimeError("HTTP 429 (rate-limited)")
                response.raise_for_status()
                total = 0
                with open(tmp_path, "wb") as handle:
                    for chunk in response.iter_content(8192):
                        total += len(chunk)
                        if total > max_bytes:
                            raise ValueError(
                                f"download exceeds {max_bytes} bytes")
                        handle.write(chunk)
            if total < 10_000:  # too small to be a real video clip
                raise ValueError(f"download too small ({total} bytes)")
            os.replace(tmp_path, dest_path)
            _wiki_consec_429 = 0  # a success clears the 429 streak
            return dest_path
        except ValueError as exc:
            # Oversize / too small: terminal for this URL, no retry.
            print(f"    stock video download rejected "
                  f"({url[:80]}): {exc}")
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            return None
        except Exception as exc:
            if attempt == 0 and "429" not in str(exc):
                # Transient blip (not a rate limit): one quick retry.
                time.sleep(3)
                continue
            print(f"    stock video download failed ({url[:80]}): {exc}")
            if "429" in str(exc) and "wikimedia.org" in url:
                _note_wiki_429()
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            return None
    return None


# --- Pexels --------------------------------------------------------------

def search_pexels(query, api_key, per_page=5):
    """Search Pexels Videos. Returns candidate dicts
    {url, width, height, duration, source, title, page_url}."""
    _rate_limit()
    response = requests.get(
        PEXELS_API,
        params={"query": query, "per_page": per_page},
        headers={"Authorization": api_key, "User-Agent": USER_AGENT},
        timeout=30,
    )
    response.raise_for_status()
    candidates = []
    for video in response.json().get("videos", []):
        # Prefer the smallest mp4 file at 1280px+ (HD without the
        # 4K download); fall back to the largest available mp4.
        files = [f for f in video.get("video_files", [])
                 if f.get("file_type") == "video/mp4" and f.get("link")]
        if not files:
            continue
        hd = [f for f in files if (f.get("width") or 0) >= 1280]
        pool = hd or files
        chosen = min(pool, key=lambda f: f.get("width") or 0)
        candidates.append({
            "url": chosen["link"],
            "width": chosen.get("width") or 0,
            "height": chosen.get("height") or 0,
            "duration": video.get("duration") or 0,
            "source": "pexels",
            "title": video.get("user", {}).get("name", ""),
            "page_url": video.get("url", ""),
        })
    return candidates


# --- Pixabay -------------------------------------------------------------

def search_pixabay(query, api_key, per_page=5):
    """Search Pixabay Videos. Returns candidate dicts (same shape as
    search_pexels)."""
    _rate_limit()
    response = requests.get(
        PIXABAY_API,
        params={"key": api_key, "q": query, "per_page": per_page},
        headers={"User-Agent": USER_AGENT},
        timeout=30,
    )
    response.raise_for_status()
    candidates = []
    for hit in response.json().get("hits", []):
        videos = hit.get("videos") or {}
        # large (1280x720) first, then medium, then small, then tiny.
        chosen = None
        for size_key in ("large", "medium", "small", "tiny"):
            entry = videos.get(size_key)
            if entry and entry.get("url"):
                chosen = entry
                break
        if not chosen:
            continue
        candidates.append({
            "url": chosen["url"],
            "width": chosen.get("width") or 0,
            "height": chosen.get("height") or 0,
            "duration": hit.get("duration") or 0,
            "source": "pixabay",
            "title": hit.get("user", ""),
            "page_url": hit.get("pageURL", ""),
        })
    return candidates


def search_pixabay_helper(query, per_page=5):
    """Search Pixabay Videos through the vault-stored custom.pixabay
    connector helper (subprocess). Same candidate shape as
    search_pixabay, plus each candidate's known file `size` in bytes
    so _try_candidates can skip oversized files without downloading.

    File choice per hit: the smallest file at 1280px wide or more
    (medium 1280x720 is typically 12-16MB vs large 1920x1080 at
    32-38MB for a 40s clip, and the prepare step upscales to
    1920x1080 anyway); if no file reaches 1280px, the widest
    available. One retry after 5s: the connector doc records that a
    first call can fail transiently (connection timeout) and the
    immediate retry succeeds."""
    _rate_limit()
    cmd = [sys.executable, PIXABAY_HELPER, "videos",
           "--query", query, "--per-page", str(per_page)]
    last_error = None
    for attempt in (0, 1):
        if attempt:
            time.sleep(5)
        try:
            result = subprocess.run(cmd, capture_output=True, text=True,
                                    timeout=90)
        except subprocess.TimeoutExpired as exc:
            last_error = f"helper timed out: {exc}"
            continue
        if result.returncode != 0:
            last_error = (result.stderr or result.stdout or
                          f"helper exit {result.returncode}").strip()
            continue
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            last_error = f"helper returned invalid JSON: {exc}"
            continue
        candidates = []
        for hit in data.get("hits", []):
            videos = hit.get("videos") or {}
            files = [entry for entry in videos.values()
                     if entry and entry.get("url")]
            if not files:
                continue
            hd = [f for f in files if (f.get("width") or 0) >= 1280]
            pool = hd or files
            # Smallest file that meets the target (hd pool), else the
            # widest available (non-hd pool).
            if hd:
                chosen = min(pool, key=lambda f: f.get("size") or 0)
            else:
                chosen = max(pool, key=lambda f: f.get("width") or 0)
            candidates.append({
                "url": chosen["url"],
                "width": chosen.get("width") or 0,
                "height": chosen.get("height") or 0,
                "size": chosen.get("size"),
                "duration": hit.get("duration") or 0,
                "source": "pixabay",
                "title": hit.get("tags", ""),
                "page_url": hit.get("pageURL", ""),
            })
        return candidates
    raise RuntimeError(last_error or "Pixabay helper search failed")


# --- Wikimedia Commons ---------------------------------------------------

def search_wikimedia(query, limit=5):
    """Search Wikimedia Commons for videos. Returns candidate dicts:
    one per file (the 1080p original) plus one per transcoded
    derivative (smaller webm/mp4 versions). Derivative sizes are
    unknown until a HEAD request, so size is None for them."""
    _rate_limit()
    response = requests.get(
        COMMONS_API,
        params={
            "action": "query", "format": "json",
            "generator": "search",
            "gsrsearch": f"{query} filetype:video",
            "gsrnamespace": 6, "gsrlimit": limit,
            "prop": "imageinfo|videoinfo",
            "iiprop": "url|size|mime",
            "viprop": "derivatives",
        },
        headers={"User-Agent": USER_AGENT},
        timeout=30,
    )
    response.raise_for_status()
    candidates = []
    pages = (response.json().get("query", {}).get("pages", {}) or {})
    for page in pages.values():
        info = (page.get("imageinfo") or [None])[0]
        if not info or not info.get("url"):
            continue
        title = (page.get("title") or "").replace("File:", "")
        page_url = ("https://commons.wikimedia.org/wiki/"
                    + urllib.parse.quote("File:" + title))
        base = {"source": "wikimedia", "title": title,
                "page_url": page_url}
        # The original file (usually 1080p .ogv).
        candidates.append({
            **base,
            "url": _strip_tracking_params(info["url"]),
            "width": info.get("width") or 0,
            "height": info.get("height") or 0,
            "size": info.get("size"),
            "kind": "original",
        })
        # Transcoded derivatives (smaller, often 480p webm).
        videoinfo = page.get("videoinfo") or []
        derivatives = []
        if isinstance(videoinfo, list):
            for entry in videoinfo:
                derivatives.extend(entry.get("derivatives") or [])
        for der in derivatives:
            if not der.get("src"):
                continue
            mime = (der.get("type") or "").lower()
            # vp9 webm first (best quality per byte), then mp4.
            fmt_rank = 0 if "webm" in mime else 1
            candidates.append({
                **base,
                "url": _strip_tracking_params(der["src"]),
                "width": der.get("width") or 0,
                "height": der.get("height") or 0,
                "size": None,
                "kind": "derivative",
                "fmt_rank": fmt_rank,
            })
    return candidates


def _title_score(title, query_words):
    """How many query words appear in a file's title (case-insensitive).
    Commons full-text search matches description text too, so a query
    like "ocean mystery deep" can return a Bill Clinton speech — title
    overlap is a cheap relevance signal for ranking."""
    lowered = (title or "").lower()
    return sum(1 for word in query_words if word.lower() in lowered)


def _rank_wikimedia(candidates, query=""):
    """Order Commons candidates: most title-relevant first, then small
    1080p originals, then the widest derivatives, then any remaining
    original <= 25MB, then small derivatives."""
    query_words = [w for w in query.split() if len(w) > 2]
    originals = [c for c in candidates if c.get("kind") == "original"]
    derivatives = [c for c in candidates if c.get("kind") == "derivative"]
    small_orig = sorted(
        [c for c in originals
         if c.get("size") and c["size"] <= PREFER_BYTES],
        key=lambda c: c.get("width") or 0, reverse=True)
    big_orig = sorted(
        [c for c in originals
         if c.get("size") and PREFER_BYTES < c["size"] <= MAX_DOWNLOAD_BYTES],
        key=lambda c: c.get("width") or 0, reverse=True)
    wide_der = sorted(
        [c for c in derivatives if (c.get("width") or 0) >= 480],
        key=lambda c: (c.get("fmt_rank", 1), -(c.get("width") or 0)))
    small_der = sorted(
        [c for c in derivatives if (c.get("width") or 0) < 480],
        key=lambda c: (c.get("fmt_rank", 1), -(c.get("width") or 0)))
    ordered = small_orig + wide_der + big_orig + small_der
    # Relevance first (stable sort keeps the size order within a score).
    ordered.sort(key=lambda c: -_title_score(c.get("title", ""),
                                             query_words))
    return ordered


# --- query + fetch ---------------------------------------------------------

def beat_query(beat_text, title, lang):
    """English search keywords for one beat. Offline only (no
    translation API call — stock search works fine with keywords, and
    this keeps the MyMemory quota for AI image prompts)."""
    if lang == "hi":
        query = ai_images_mod._keywords_fallback(beat_text, title)
    else:
        query = " ".join(images_mod.extract_keywords(
            f"{beat_text} {title}", "en", max_words=4))
    return query or ""


def _try_candidates(candidates, cache_path_base, meta):
    """Download the first candidate that fits the size cap and is a
    real video. Returns (path, info) or (None, None).

    No HEAD request is made first: the streaming download already
    aborts over the size cap, and every extra request adds pressure
    on rate-limited hosts (upload.wikimedia.org 429s this VM's
    shared IP). At most MAX_CANDIDATES are tried per query."""
    for cand in candidates[:MAX_CANDIDATES]:
        size = cand.get("size")  # known for Commons originals only
        if size and size > MAX_DOWNLOAD_BYTES:
            continue
        ext = os.path.splitext(
            urllib.parse.urlparse(cand["url"]).path)[1] or ".mp4"
        dest = cache_path_base + ext.lower()
        if os.path.exists(dest):
            return dest, {**meta, **cand}
        path = _download(cand["url"], dest)
        if not path:
            if _wiki_in_cooldown() and "wikimedia.org" in cand["url"]:
                # Circuit breaker tripped: stop trying candidates for
                # this query, the next beats will retry after cooldown.
                return None, None
            time.sleep(1)  # don't hammer a rate-limited host
            continue
        # Verify ffmpeg can read it (a corrupt/partial file fails here).
        try:
            render_mod.media_duration(path)
        except Exception as exc:
            print(f"    stock clip unreadable, discarding: {exc}")
            os.remove(path)
            continue
        return path, {**meta, **cand}
    return None, None


def fetch_clip(query, cache_dir, pexels_key=None, pixabay_key=None):
    """Fetch one stock video clip for a query (cached).

    Source priority: Pexels (key only) -> Pixabay (key only) ->
    Wikimedia Commons (no key). Returns (path, info) or (None, None)
    when nothing usable was found — the caller falls back to an AI
    image for that beat.
    """
    if not query:
        return None, None
    key = hashlib.sha1(query.encode("utf-8")).hexdigest()[:12]
    meta_path = os.path.join(cache_dir, f"sv_{key}.json")
    if os.path.exists(meta_path):
        with open(meta_path, encoding="utf-8") as handle:
            info = json.load(handle)
        cached = info.get("cached_path", "")
        if cached and os.path.exists(cached):
            return cached, info
    os.makedirs(cache_dir, exist_ok=True)
    base = os.path.join(cache_dir, f"sv_{key}")
    meta = {"query": query}

    if pexels_key:
        try:
            candidates = search_pexels(query, pexels_key)
            path, info = _try_candidates(candidates, base, meta)
            if path:
                info["cached_path"] = path
                with open(meta_path, "w", encoding="utf-8") as handle:
                    json.dump(info, handle, ensure_ascii=False, indent=2)
                return path, info
        except Exception as exc:
            print(f"    Pexels search failed for '{query}': {exc}")
    if pixabay_key:
        try:
            candidates = search_pixabay(query, pixabay_key)
            path, info = _try_candidates(candidates, base, meta)
            if path:
                info["cached_path"] = path
                with open(meta_path, "w", encoding="utf-8") as handle:
                    json.dump(info, handle, ensure_ascii=False, indent=2)
                return path, info
        except Exception as exc:
            print(f"    Pixabay search failed for '{query}': {exc}")
    elif os.path.exists(PIXABAY_HELPER):
        # No raw key: use the vault-stored key via the connector
        # helper (local Muse only; deploys use the raw-key path).
        try:
            candidates = search_pixabay_helper(query)
            path, info = _try_candidates(candidates, base, meta)
            if path:
                info["cached_path"] = path
                with open(meta_path, "w", encoding="utf-8") as handle:
                    json.dump(info, handle, ensure_ascii=False, indent=2)
                return path, info
        except Exception as exc:
            print(f"    Pixabay helper search failed for '{query}': {exc}")
    if _wiki_in_cooldown():
        # Downloads would just 429 — skip to the caller's fallback.
        print("    Wikimedia in cooldown; skipping stock video")
        return None, None
    # Commons search is literal full-text: multi-word queries often
    # return nothing ("mystery Bermuda Atlantic ship" -> 0 files) or
    # irrelevant files, while a short query works ("ocean" -> 5 files).
    # Try the full query, then the first two words, then the first word.
    words = query.split()
    tries = [query]
    if len(words) > 2:
        tries.append(" ".join(words[:2]))
    if len(words) > 1:
        tries.append(words[0])
    for attempt_query in tries:
        try:
            candidates = _rank_wikimedia(
                search_wikimedia(attempt_query), attempt_query)
        except Exception as exc:
            print(f"    Wikimedia search failed for '{attempt_query}': "
                  f"{exc}")
            return None, None
        if not candidates:
            continue
        path, info = _try_candidates(candidates, base, meta)
        if path:
            info["cached_path"] = path
            info["search_query"] = attempt_query
            with open(meta_path, "w", encoding="utf-8") as handle:
                json.dump(info, handle, ensure_ascii=False, indent=2)
            return path, info
        if _wiki_in_cooldown():
            # Downloads are being rate-limited; shorter queries would
            # hit the same wall — stop here.
            return None, None
    return None, None


# --- beat preparation ------------------------------------------------------

def prepare_beat_clip(src_path, out_path, duration):
    """Trim/loop a stock clip to exactly `duration` seconds and scale
    it to 1920x1080, 30fps, WITHOUT audio. Short clips are looped; long
    clips use a middle segment (the opening/closing seconds of stock
    clips are often static or watermarked). Returns out_path, or None
    on failure."""
    try:
        src_dur = render_mod.media_duration(src_path)
    except Exception as exc:
        print(f"    stock clip probe failed ({src_path}): {exc}")
        return None
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    cmd = ["ffmpeg", "-y", "-v", "error"]
    if src_dur < duration:
        cmd += ["-stream_loop", "-1", "-i", src_path]
    else:
        start = max((src_dur - duration) / 2.0, 0.0)
        cmd += ["-ss", f"{start:.3f}", "-i", src_path]
    cmd += [
        "-t", f"{duration:.3f}",
        "-vf", ("scale=1920:1080:force_original_aspect_ratio=increase,"
                "crop=1920:1080,fps=30,format=yuv420p"),
        "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-r", "30", out_path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"    stock clip transcode failed: "
                  f"{result.stderr[-500:]}")
            return None
    except Exception as exc:
        print(f"    stock clip transcode failed: {exc}")
        return None
    if not os.path.exists(out_path):
        return None
    return out_path
