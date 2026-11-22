#!/usr/bin/env python3
"""Video Studio public-website server (Python standard library only).

This is the Hugging Face deploy copy. Differences from the local server:

- Google login (OAuth2) is required to make or see videos. Sign-in is
  sign-up; no passwords are stored anywhere.
- Jobs are private per user (namespaced by Google user id). Job IDs are
  full uuid4 values, and every read/download checks ownership.
- Rate limit: 3 videos per user per day, counted server-side.
- Each finished video gets a 30-second 480p watermarked preview
  (preview.mp4). The full 1080p download costs Rs 1 via Razorpay and is
  served only after a verified payment (paid.json). The preview is
  watch-only: it is served on a short-lived signed URL (10 minutes),
  with no-store headers and no download control in the player.
  (Screen recording cannot be blocked by any website.)
- Renders run strictly one at a time, FIFO by job creation, because
  the free server is tiny (0.1 CPU). Waiting jobs report their queue
  position. Jobs left queued/running by a server restart are
  re-queued automatically on startup, so no visitor's video is lost
  when the free server sleeps.

All secrets come from environment variables (set as Hugging Face Space
secrets): GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, OAUTH_REDIRECT_URI,
SESSION_SECRET, RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET. Without the
Google keys the server still starts, but login shows a clear
"login not configured yet" state. Without Razorpay keys the pay button
shows "payment not set up yet". No secret is ever written to a file.

Run:
    python3 server/app.py [--port 7860] [--host 0.0.0.0]
"""

import argparse
import base64
import hashlib
import hmac
import json
import os
import queue
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JOBS_DIR = os.path.join(PROJECT_ROOT, "jobs")
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "static")
ENGINE = os.path.join(PROJECT_ROOT, "engine", "make_video.py")
CONFIG_PATH = os.path.join(PROJECT_ROOT, "config.json")

# Free-tier safeguard: cap scripts at ~400 words so one render stays
# around 2-3 minutes of video.
MAX_SCRIPT_WORDS = 400
# Free creation limit per user per day.
DAILY_VIDEO_LIMIT = 3
# Preview links live for 10 minutes; the player page refreshes them.
PREVIEW_TOKEN_MINUTES = 10
# ffmpeg threads cap for the small free server (0.1 CPU).
FFMPEG_THREADS = "2"
SESSION_DAYS = 30

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
RAZORPAY_ORDERS_URL = "https://api.razorpay.com/v1/orders"

JOB_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
ALLOWED_FILES = {
    "video.mp4", "video.srt", "video.attributions.json",
    "video.summary.json", "render.log", "script.txt",
}
# Files that are free for the owner; video.mp4 additionally needs payment.
FREE_FILES = ALLOWED_FILES - {"video.mp4"}

# Fonts for the PREVIEW watermark (DejaVu ships in the Docker image).
WATERMARK_FONTS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
]


def env(name):
    value = os.environ.get(name, "").strip()
    return value or None


GOOGLE_CLIENT_ID = env("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = env("GOOGLE_CLIENT_SECRET")
OAUTH_REDIRECT_URI = env("OAUTH_REDIRECT_URI")
RAZORPAY_KEY_ID = env("RAZORPAY_KEY_ID")
RAZORPAY_KEY_SECRET = env("RAZORPAY_KEY_SECRET")
SESSION_SECRET = env("SESSION_SECRET")
if not SESSION_SECRET:
    # Sessions still work, but they stop being valid after a restart.
    SESSION_SECRET = secrets.token_hex(32)
    print("WARNING: SESSION_SECRET not set; using a random per-process "
          "secret (logins will not survive a restart).", file=sys.stderr)


def oauth_configured():
    return bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET
                and OAUTH_REDIRECT_URI)


def payments_configured():
    return bool(RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET)


def known_styles():
    """Style keys from config.json, falling back to the built-ins."""
    try:
        with open(CONFIG_PATH, encoding="utf-8") as handle:
            styles = json.load(handle).get("styles") or {}
        if styles:
            return set(styles.keys())
    except (OSError, ValueError):
        pass
    return {"cinematic", "madhubani", "realistic", "cartoon3d", "anime",
            "watercolor", "oilpainting", "cyberpunk"}


job_queue = queue.Queue()
os.makedirs(JOBS_DIR, exist_ok=True)

# The job the single worker thread is rendering right now (None when
# idle). Guarded by a lock because the HTTP threads read it.
_current_job_id = None
_current_job_lock = threading.Lock()


def queue_snapshot():
    """Job IDs waiting in the FIFO queue, in order (thread-safe)."""
    with job_queue.mutex:
        return [item[0] for item in job_queue.queue]


def queue_position(job_id):
    """1-based position among waiting jobs, or None if not waiting.

    The job currently rendering counts as ahead of every waiting job,
    so position 2 means "one video is rendering ahead of you".
    """
    waiting = queue_snapshot()
    if job_id in waiting:
        ahead = waiting.index(job_id)
        if current_job_id() is not None:
            ahead += 1
        return ahead + 1
    return None


def current_job_id():
    with _current_job_lock:
        return _current_job_id


# --- sessions ------------------------------------------------------------

def _b64encode(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(text):
    padded = text + "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def make_session_token(user):
    """Signed session token: base64(payload).hex-hmac."""
    payload = {"sub": user["sub"], "email": user.get("email", ""),
               "name": user.get("name", ""),
               "exp": int(time.time()) + SESSION_DAYS * 86400}
    raw = json.dumps(payload, separators=(",", ":"),
                     ensure_ascii=False).encode("utf-8")
    encoded = _b64encode(raw)
    sig = hmac.new(SESSION_SECRET.encode("utf-8"),
                   encoded.encode("ascii"),
                   hashlib.sha256).hexdigest()
    return encoded + "." + sig


def parse_session_token(token):
    """Return the payload dict, or None when invalid/expired."""
    if not token or "." not in token:
        return None
    encoded, _, sig = token.rpartition(".")
    expected = hmac.new(SESSION_SECRET.encode("utf-8"),
                        encoded.encode("ascii"),
                        hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return None
    try:
        payload = json.loads(_b64decode(encoded))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict) or not payload.get("sub"):
        return None
    if int(payload.get("exp", 0)) < int(time.time()):
        return None
    return payload


def session_cookie_header(token, max_age=None):
    if max_age is None:
        max_age = SESSION_DAYS * 86400
    return ("session=%s; HttpOnly; Secure; SameSite=Lax; Path=/; "
            "Max-Age=%d" % (token, max_age))


def clear_session_cookie_header():
    return ("session=; HttpOnly; Secure; SameSite=Lax; Path=/; "
            "Max-Age=0")


def parse_cookies(header):
    cookies = {}
    for part in (header or "").split(";"):
        part = part.strip()
        if "=" in part:
            name, _, value = part.partition("=")
            cookies[name.strip()] = value.strip()
    return cookies


# --- preview tokens --------------------------------------------------------

def make_preview_token(job_id):
    """Short-lived signed token for one job's preview (10 minutes).

    Keyed by SESSION_SECRET (a random per-process key when unset), so a
    token cannot be forged or moved to another job. Format:
    <expiry-epoch>.<hex-hmac>.
    """
    exp = int(time.time()) + PREVIEW_TOKEN_MINUTES * 60
    message = ("%s.%d" % (job_id, exp)).encode("utf-8")
    sig = hmac.new(SESSION_SECRET.encode("utf-8"), message,
                   hashlib.sha256).hexdigest()
    return "%d.%s" % (exp, sig)


def verify_preview_token(job_id, token):
    """True only for a genuine, unexpired token for this job."""
    if not token or "." not in token:
        return False
    exp_text, _, sig = token.rpartition(".")
    try:
        exp = int(exp_text)
    except ValueError:
        return False
    if exp < int(time.time()):
        return False
    message = ("%s.%d" % (job_id, exp)).encode("utf-8")
    expected = hmac.new(SESSION_SECRET.encode("utf-8"), message,
                        hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)


def preview_url(job_id):
    """Signed preview URL for a finished preview, or None."""
    if not os.path.exists(os.path.join(job_dir(job_id), "preview.mp4")):
        return None
    token = make_preview_token(job_id)
    return "/preview/%s?token=%s" % (
        job_id, urllib.parse.quote(token, safe=""))


# --- job helpers ---------------------------------------------------------

def job_dir(job_id):
    return os.path.join(JOBS_DIR, job_id)


def write_status(job_id, status, message="", progress=0.0, extra=None):
    data = {"status": status, "message": message, "progress": progress,
            "updated": time.strftime("%Y-%m-%d %H:%M:%S")}
    if extra:
        data.update(extra)
    with open(os.path.join(job_dir(job_id), "status.json"), "w",
              encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False)


def read_status(job_id):
    path = os.path.join(job_dir(job_id), "status.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    return {"status": "unknown", "message": "", "progress": 0.0}


def read_meta(job_id):
    path = os.path.join(job_dir(job_id), "meta.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    return {}


def is_paid(job_id):
    return os.path.exists(os.path.join(job_dir(job_id), "paid.json"))


def owns_job(job_id, user):
    """True only when the job exists and belongs to this user."""
    if not user or not JOB_ID_RE.match(job_id):
        return False
    if not os.path.isdir(job_dir(job_id)):
        return False
    return read_meta(job_id).get("owner") == user["sub"]


def videos_used_today(owner):
    """How many videos this user created today (UTC date)."""
    today = time.strftime("%Y-%m-%d", time.gmtime())
    count = 0
    try:
        entries = os.listdir(JOBS_DIR)
    except OSError:
        return 0
    for job_id in entries:
        path = job_dir(job_id)
        if not os.path.isdir(path):
            continue
        meta = read_meta(job_id)
        if meta.get("owner") != owner:
            continue
        created = str(meta.get("created", ""))[:10]
        if created == today:
            count += 1
    return count


def list_jobs(owner):
    jobs = []
    waiting = queue_snapshot()
    # The job currently rendering counts as ahead of every waiting job.
    running_ahead = 1 if current_job_id() is not None else 0
    for job_id in sorted(os.listdir(JOBS_DIR), reverse=True):
        path = job_dir(job_id)
        if not os.path.isdir(path):
            continue
        meta = read_meta(job_id)
        if meta.get("owner") != owner:
            continue
        status = read_status(job_id)
        summary = {}
        summary_path = os.path.join(path, "video.summary.json")
        if os.path.exists(summary_path):
            with open(summary_path, encoding="utf-8") as handle:
                summary = json.load(handle)
        position = (waiting.index(job_id) + 1 + running_ahead
                    if job_id in waiting else None)
        jobs.append({
            "id": job_id,
            "title": meta.get("title", job_id),
            "lang": meta.get("lang", "hi"),
            "style": meta.get("style", "cinematic"),
            "created": meta.get("created", ""),
            "status": status.get("status", "unknown"),
            "message": status.get("message", ""),
            "progress": status.get("progress", 0.0),
            "queue_position": position,
            "queue_ahead": (position - 1) if position else 0,
            "has_video": os.path.exists(os.path.join(path, "video.mp4")),
            "has_preview": os.path.exists(
                os.path.join(path, "preview.mp4")),
            "preview_url": preview_url(job_id),
            "paid": is_paid(job_id),
            "duration_sec": summary.get("duration_sec"),
        })
    # Job IDs are random uuids, so sort by creation time, newest first.
    jobs.sort(key=lambda job: job.get("created", ""), reverse=True)
    return jobs


# --- preview --------------------------------------------------------------

def _watermark_font():
    for candidate in WATERMARK_FONTS:
        if os.path.exists(candidate):
            return candidate
    return None


def make_preview(directory):
    """Build preview.mp4: first 30s, 480p, PREVIEW watermark.

    Returns True on success. A preview failure must never fail the job,
    so callers log the False and carry on.
    """
    src = os.path.join(directory, "video.mp4")
    out = os.path.join(directory, "preview.mp4")
    font = _watermark_font()
    big = ("text='PREVIEW':fontsize=72:fontcolor=white@0.85:borderw=2:"
           "bordercolor=black@0.9:box=1:boxcolor=black@0.35:boxborderw=16:"
           "x=(w-text_w)/2:y=(h-text_h)/2")
    small = ("text='Video Studio':fontsize=26:fontcolor=white@0.8:"
             "borderw=1:bordercolor=black@0.8:x=16:y=16")
    if font:
        vf = ("scale=854:480:force_original_aspect_ratio=decrease,"
              "pad=854:480:(ow-iw)/2:(oh-ih)/2,"
              "drawtext=fontfile=%s:%s,drawtext=fontfile=%s:%s"
              % (font, big, font, small))
        attempts = [vf]
    else:
        # No font file found: try drawtext with the fontconfig default.
        attempts = [
            ("scale=854:480:force_original_aspect_ratio=decrease,"
             "pad=854:480:(ow-iw)/2:(oh-ih)/2,"
             "drawtext=%s,drawtext=%s" % (big, small)),
            # Last resort: a visible band, no text.
            ("scale=854:480:force_original_aspect_ratio=decrease,"
             "pad=854:480:(ow-iw)/2:(oh-ih)/2,"
             "drawbox=x=0:y=200:w=854:h=80:color=black@0.5:t=fill"),
        ]
    for vf in attempts:
        cmd = ["ffmpeg", "-y", "-v", "error", "-i", src, "-t", "30",
               "-vf", vf,
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
               "-threads", FFMPEG_THREADS,
               "-c:a", "aac", "-movflags", "+faststart", out]
        result = subprocess.run(cmd, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, text=True,
                                timeout=300)
        if result.returncode == 0 and os.path.exists(out):
            return True
        print("preview attempt failed: %s"
              % result.stderr.strip().splitlines()[-1:]
              if result.stderr else "preview attempt failed",
              file=sys.stderr)
    return False


# --- render worker --------------------------------------------------------

def run_render(job_id, title, lang, music, style="cinematic"):
    """Run the engine for one job (called from the worker thread)."""
    directory = job_dir(job_id)
    script_path = os.path.join(directory, "script.txt")
    out_path = os.path.join(directory, "video.mp4")
    log_path = os.path.join(directory, "render.log")
    work_status = os.path.join(directory, "video.work", "status.json")
    cmd = [sys.executable, ENGINE, "--script", script_path,
           "--title", title, "--out", out_path, "--lang", lang,
           "--style", style]
    if music == "none":
        cmd += ["--music", "none"]
    write_status(job_id, "running", "Render shuru ho gaya...", 0.02)
    with open(log_path, "w", encoding="utf-8") as log_file:
        process = subprocess.Popen(
            cmd, stdout=log_file, stderr=subprocess.STDOUT,
            cwd=PROJECT_ROOT, text=True)
        # Mirror the engine's own progress into our status file.
        while process.poll() is None:
            if os.path.exists(work_status):
                try:
                    with open(work_status, encoding="utf-8") as handle:
                        engine_status = json.load(handle)
                    write_status(
                        job_id, "running",
                        engine_status.get("message", "Render chal raha hai..."),
                        engine_status.get("progress", 0.0))
                except (OSError, ValueError):
                    pass
            time.sleep(5)
    if process.returncode == 0 and os.path.exists(out_path):
        write_status(job_id, "running", "Preview ban raha hai...", 0.98)
        preview_ok = make_preview(directory)
        summary = {}
        summary_path = os.path.join(directory, "video.summary.json")
        if os.path.exists(summary_path):
            with open(summary_path, encoding="utf-8") as handle:
                summary = json.load(handle)
        mins = (summary.get("duration_sec") or 0) / 60
        message = f"Video tayyar hai ({mins:.1f} minute)"
        if not preview_ok:
            message += " (preview nahi ban paya — phir bhi download kar sakte hain)"
        write_status(job_id, "done", message, 1.0)
    else:
        write_status(job_id, "failed",
                     "Render fail ho gaya — log mein detail dekhein", 0.0)


def worker():
    """Single render worker: strictly one render at a time, FIFO.

    One worker thread + one queue means a new arrival can never starve
    or overtake a job that is already waiting or running.
    """
    global _current_job_id
    while True:
        job_id, title, lang, music, style = job_queue.get()
        with _current_job_lock:
            _current_job_id = job_id
        try:
            run_render(job_id, title, lang, music, style)
        except Exception as exc:  # never kill the worker thread
            write_status(job_id, "failed", f"Error: {exc}", 0.0)
        finally:
            with _current_job_lock:
                _current_job_id = None
        job_queue.task_done()


def recover_interrupted_jobs():
    """Re-queue jobs left queued/running by a server restart.

    The queue lives in memory, so when the free server sleeps or
    restarts, waiting and in-flight jobs would otherwise be stranded
    forever with a stale status. Re-queue them in creation order so
    every visitor's video still gets made. A re-run overwrites any
    partial render output (the engine uses ffmpeg -y).
    """
    pending = []
    try:
        entries = os.listdir(JOBS_DIR)
    except OSError:
        return
    for job_id in entries:
        if not os.path.isdir(job_dir(job_id)):
            continue
        if read_status(job_id).get("status") not in ("queued", "running"):
            continue
        meta = read_meta(job_id)
        pending.append((meta.get("created", ""), job_id, meta))
    pending.sort(key=lambda item: item[0])
    for _, job_id, meta in pending:
        write_status(job_id, "queued",
                     "Server restart hua — video phir se queue mein hai",
                     0.0)
        job_queue.put((job_id, meta.get("title", job_id),
                       meta.get("lang", "hi"),
                       meta.get("music", "default"),
                       meta.get("style", "cinematic")))
    if pending:
        print("Recovered %d interrupted job(s) into the render queue."
              % len(pending), file=sys.stderr)


# --- Google OAuth ---------------------------------------------------------

def google_auth_url(state):
    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": OAUTH_REDIRECT_URI,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "access_type": "online",
        "prompt": "select_account",
    }
    return GOOGLE_AUTH_URL + "?" + urllib.parse.urlencode(params)


def exchange_code_for_user(code):
    """Exchange an auth code for the Google user profile (dict)."""
    data = urllib.parse.urlencode({
        "code": code,
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "redirect_uri": OAUTH_REDIRECT_URI,
        "grant_type": "authorization_code",
    }).encode("utf-8")
    request = urllib.request.Request(
        GOOGLE_TOKEN_URL, data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(request, timeout=30) as response:
        token = json.loads(response.read().decode("utf-8"))
    access_token = token.get("access_token")
    if not access_token:
        raise ValueError("Google ne access token nahi diya")
    request = urllib.request.Request(
        GOOGLE_USERINFO_URL,
        headers={"Authorization": "Bearer " + access_token})
    with urllib.request.urlopen(request, timeout=30) as response:
        user = json.loads(response.read().decode("utf-8"))
    if not user.get("sub"):
        raise ValueError("Google userinfo mein user id nahi mila")
    return user


# --- Razorpay -------------------------------------------------------------

def create_razorpay_order(job_id):
    """Create a Rs 1 Razorpay order. Returns the order dict."""
    payload = json.dumps({
        "amount": PRICE_PAISE,
        "currency": "INR",
        "receipt": "vs-" + job_id[:30],
        "notes": {"job_id": job_id},
    }).encode("utf-8")
    basic = base64.b64encode(
        ("%s:%s" % (RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET)
         ).encode("utf-8")).decode("ascii")
    request = urllib.request.Request(
        RAZORPAY_ORDERS_URL, data=payload,
        headers={"Content-Type": "application/json",
                 "Authorization": "Basic " + basic})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:300]
        raise ValueError("Razorpay order nahi ban paya (%s): %s"
                         % (exc.code, body))


def verify_razorpay_signature(order_id, payment_id, signature):
    """True when the Razorpay payment signature checks out."""
    message = ("%s|%s" % (order_id, payment_id)).encode("utf-8")
    expected = hmac.new(RAZORPAY_KEY_SECRET.encode("utf-8"), message,
                        hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")


# --- HTTP handler ---------------------------------------------------------

LEGAL_PAGES = {
    "/about": "about.html",
    "/privacy": "privacy.html",
    "/terms": "terms.html",
    "/refund": "refund.html",
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(),
                                        fmt % args))

    def send_json(self, data, code=200, extra_headers=None):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for name, value in (extra_headers or []):
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, html, code=200, extra_headers=None):
        body = html.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for name, value in (extra_headers or []):
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def redirect(self, location, extra_headers=None):
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        for name, value in (extra_headers or []):
            self.send_header(name, value)
        self.end_headers()

    def current_user(self):
        cookies = parse_cookies(self.headers.get("Cookie"))
        return parse_session_token(cookies.get("session", ""))

    def require_user(self):
        """Return the logged-in user, or send 401 and return None."""
        user = self.current_user()
        if not user:
            self.send_json(
                {"error": "pehle Google se login karein",
                 "login_required": True}, 401)
            return None
        return user

    def do_GET(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = urllib.parse.parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            return self.serve_file(os.path.join(STATIC_DIR, "index.html"),
                                   "text/html; charset=utf-8")
        if path in LEGAL_PAGES:
            return self.serve_file(
                os.path.join(STATIC_DIR, LEGAL_PAGES[path]),
                "text/html; charset=utf-8")
        if path == "/api/me":
            user = self.current_user()
            if not user:
                return self.send_json({
                    "logged_in": False,
                    "oauth_configured": oauth_configured(),
                    "payments_configured": payments_configured()})
            used = videos_used_today(user["sub"])
            return self.send_json({
                "logged_in": True,
                "email": user.get("email", ""),
                "name": user.get("name", ""),
                "oauth_configured": oauth_configured(),
                "payments_configured": payments_configured(),
                "videos_used_today": used,
                "videos_limit": DAILY_VIDEO_LIMIT,
                "videos_remaining": max(0, DAILY_VIDEO_LIMIT - used)})
        if path == "/auth/login":
            if not oauth_configured():
                return self.send_html(
                    "<!DOCTYPE html><meta charset=utf-8><title>Video Studio"
                    "</title><body style='font-family:system-ui;padding:32px'"
                    "><h1>Login abhi set up nahi hua hai</h1><p>Is website "
                    "par Google login abhi configure nahi hua hai. Site ke "
                    "owner ko Google OAuth keys lagani hain (DEPLOY.md "
                    "dekhein). Tab tak video nahi ban sakti.</p><p><a href="
                    "'/'>Wapas jao</a></p></body>", 200)
            state = secrets.token_urlsafe(24)
            cookie = ("oauth_state=%s; HttpOnly; Secure; SameSite=Lax; "
                      "Path=/auth/callback; Max-Age=600" % state)
            return self.redirect(google_auth_url(state),
                                 [("Set-Cookie", cookie)])
        if path == "/auth/callback":
            return self.handle_oauth_callback(query)
        if path == "/auth/logout":
            return self.redirect(
                "/", [("Set-Cookie", clear_session_cookie_header())])
        if path == "/api/jobs":
            user = self.require_user()
            if not user:
                return
            return self.send_json({"jobs": list_jobs(user["sub"])})
        match = re.match(r"^/api/jobs/([A-Za-z0-9_-]+)$", path)
        if match:
            user = self.require_user()
            if not user:
                return
            job_id = match.group(1)
            if not owns_job(job_id, user):
                return self.send_json({"error": "job not found"}, 404)
            log_tail = []
            log_path = os.path.join(job_dir(job_id), "render.log")
            if os.path.exists(log_path):
                with open(log_path, encoding="utf-8",
                          errors="replace") as handle:
                    log_tail = handle.read().splitlines()[-60:]
            position = queue_position(job_id)
            return self.send_json({
                "id": job_id, **read_meta(job_id),
                **read_status(job_id), "log": log_tail,
                "queue_position": position,
                "queue_ahead": (position - 1) if position else 0,
                "has_video": os.path.exists(
                    os.path.join(job_dir(job_id), "video.mp4")),
                "has_preview": os.path.exists(
                    os.path.join(job_dir(job_id), "preview.mp4")),
                "preview_url": preview_url(job_id),
                "paid": is_paid(job_id),
            })
        match = re.match(r"^/preview/([A-Za-z0-9_-]+)$", path)
        if match:
            user = self.require_user()
            if not user:
                return
            job_id = match.group(1)
            token = query.get("token", [None])[0]
            if not verify_preview_token(job_id, token):
                return self.send_json(
                    {"error": "preview link expire ho gaya — page refresh "
                              "karke phir se dekhein"}, 401)
            if not owns_job(job_id, user):
                return self.send_json({"error": "job not found"}, 404)
            file_path = os.path.join(job_dir(job_id), "preview.mp4")
            if not os.path.exists(file_path):
                return self.send_json({"error": "preview not ready"}, 404)
            return self.serve_file(
                file_path, "video/mp4",
                extra_headers=[
                    ("Cache-Control", "no-store"),
                    ("X-Content-Type-Options", "nosniff"),
                    ("Content-Disposition", "inline")])
        match = re.match(r"^/download/([A-Za-z0-9_-]+)/([A-Za-z0-9_.-]+)$",
                         path)
        if match:
            user = self.require_user()
            if not user:
                return
            job_id, filename = match.groups()
            if filename not in ALLOWED_FILES:
                return self.send_json({"error": "file not allowed"}, 403)
            if not owns_job(job_id, user):
                return self.send_json({"error": "file not found"}, 404)
            if filename == "video.mp4" and not is_paid(job_id):
                return self.send_json(
                    {"error": "full HD download ke liye pehle Rs 1 pay karein",
                     "payment_required": True}, 402)
            file_path = os.path.join(job_dir(job_id), filename)
            if not os.path.exists(file_path):
                return self.send_json({"error": "file not found"}, 404)
            content_type = "video/mp4" if filename.endswith(".mp4") \
                else "application/octet-stream"
            return self.serve_file(file_path, content_type)
        return self.send_json({"error": "not found"}, 404)

    def handle_oauth_callback(self, query):
        error = query.get("error", [None])[0]
        if error:
            return self.send_html(
                "<!DOCTYPE html><meta charset=utf-8><title>Video Studio"
                "</title><body style='font-family:system-ui;padding:32px'>"
                "<h1>Login fail ho gaya</h1><p>Google se login poora nahi "
                "hua. Phir se koshish karein.</p><p><a href='/'>Wapas jao"
                "</a></p></body>", 400)
        code = query.get("code", [None])[0]
        state = query.get("state", [None])[0]
        cookies = parse_cookies(self.headers.get("Cookie"))
        expected_state = cookies.get("oauth_state", "")
        if not code or not state or not expected_state \
                or not hmac.compare_digest(state, expected_state):
            return self.send_html(
                "<!DOCTYPE html><meta charset=utf-8><title>Video Studio"
                "</title><body style='font-family:system-ui;padding:32px'>"
                "<h1>Login fail ho gaya</h1><p>Security check fail hua "
                "(state match nahi hua). Phir se login karein.</p><p><a "
                "href='/'>Wapas jao</a></p></body>", 400)
        try:
            user = exchange_code_for_user(code)
        except Exception as exc:
            sys.stderr.write("OAuth exchange failed: %s\n" % exc)
            return self.send_html(
                "<!DOCTYPE html><meta charset=utf-8><title>Video Studio"
                "</title><body style='font-family:system-ui;padding:32px'>"
                "<h1>Login fail ho gaya</h1><p>Google se baat nahi ho payi. "
                "Thodi der baad phir se koshish karein.</p><p><a href='/'>"
                "Wapas jao</a></p></body>", 502)
        token = make_session_token(user)
        clear_state = ("oauth_state=; HttpOnly; Secure; SameSite=Lax; "
                       "Path=/auth/callback; Max-Age=0")
        return self.redirect("/", [("Set-Cookie", session_cookie_header(token)),
                                   ("Set-Cookie", clear_state)])

    def do_POST(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        length = int(self.headers.get("Content-Length", 0) or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self.send_json({"error": "invalid JSON"}, 400)

        if path == "/api/jobs":
            return self.create_job(payload)
        match = re.match(r"^/api/jobs/([A-Za-z0-9_-]+)/pay$", path)
        if match:
            return self.create_payment(match.group(1))
        match = re.match(r"^/api/jobs/([A-Za-z0-9_-]+)/verify$", path)
        if match:
            return self.verify_payment(match.group(1), payload)
        return self.send_json({"error": "not found"}, 404)

    def create_job(self, payload):
        user = self.require_user()
        if not user:
            return
        title = str(payload.get("title", "")).strip()
        script = str(payload.get("script", "")).strip()
        lang = str(payload.get("lang", "hi")).strip()
        music = str(payload.get("music", "default")).strip()
        style = str(payload.get("style", "cinematic")).strip()
        if style not in known_styles():
            style = "cinematic"
        if not title:
            return self.send_json({"error": "title chahiye"}, 400)
        words = len(script.split())
        if words < 30:
            return self.send_json(
                {"error": "script bahut chhoti hai (kam se kam 30 shabd)"},
                400)
        if words > MAX_SCRIPT_WORDS:
            return self.send_json(
                {"error": f"script bahut lambi hai (zyada se zyada "
                          f"{MAX_SCRIPT_WORDS} shabd, lagbhag 2-3 minute "
                          f"ki video)"},
                400)
        if lang not in ("hi", "en"):
            return self.send_json({"error": "lang hi ya en hona chahiye"},
                                  400)
        used = videos_used_today(user["sub"])
        if used >= DAILY_VIDEO_LIMIT:
            return self.send_json(
                {"error": f"aaj ki limit poori ho gayi ({DAILY_VIDEO_LIMIT} "
                          f"videos/day) — kal phir se bana sakte hain",
                 "limit_reached": True}, 429)
        job_id = str(uuid.uuid4())
        directory = job_dir(job_id)
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, "script.txt"), "w",
                  encoding="utf-8") as handle:
            handle.write(script)
        with open(os.path.join(directory, "meta.json"), "w",
                  encoding="utf-8") as handle:
            json.dump({"title": title, "lang": lang, "style": style,
                       "music": music,
                       "owner": user["sub"],
                       "created": time.strftime("%Y-%m-%d %H:%M:%S",
                                                 time.gmtime())},
                      handle, ensure_ascii=False)
        write_status(job_id, "queued",
                     "Queue mein hai — render jald shuru hoga", 0.0)
        job_queue.put((job_id, title, lang, music, style))
        position = queue_position(job_id)
        return self.send_json({"job_id": job_id, "status": "queued",
                               "queue_position": position,
                               "queue_ahead":
                                   (position - 1) if position else 0,
                               "videos_remaining":
                                   max(0, DAILY_VIDEO_LIMIT - used - 1)})

    def create_payment(self, job_id):
        user = self.require_user()
        if not user:
            return
        if not owns_job(job_id, user):
            return self.send_json({"error": "job not found"}, 404)
        if is_paid(job_id):
            return self.send_json({"paid": True})
        if not payments_configured():
            return self.send_json(
                {"error": "payment abhi set up nahi hua hai — site ke owner "
                          "ko Razorpay keys lagani hain",
                 "payment_not_configured": True}, 503)
        if not os.path.exists(os.path.join(job_dir(job_id), "video.mp4")):
            return self.send_json({"error": "video abhi tayyar nahi hai"},
                                  400)
        try:
            order = create_razorpay_order(job_id)
        except Exception as exc:
            sys.stderr.write("Razorpay order failed: %s\n" % exc)
            return self.send_json(
                {"error": "payment shuru nahi ho paya — thodi der baad phir "
                          "se koshish karein"}, 502)
        with open(os.path.join(job_dir(job_id), "order.json"), "w",
                  encoding="utf-8") as handle:
            json.dump({"order_id": order.get("id"),
                       "amount": PRICE_PAISE,
                       "created": time.strftime("%Y-%m-%d %H:%M:%S",
                                                 time.gmtime())},
                      handle, ensure_ascii=False)
        return self.send_json({
            "order_id": order.get("id"),
            "key_id": RAZORPAY_KEY_ID,
            "amount": PRICE_PAISE,
            "currency": "INR",
            "job_id": job_id})

    def verify_payment(self, job_id, payload):
        user = self.require_user()
        if not user:
            return
        if not owns_job(job_id, user):
            return self.send_json({"error": "job not found"}, 404)
        if not payments_configured():
            return self.send_json(
                {"error": "payment abhi set up nahi hua hai"}, 503)
        order_id = str(payload.get("razorpay_order_id", ""))
        payment_id = str(payload.get("razorpay_payment_id", ""))
        signature = str(payload.get("razorpay_signature", ""))
        if not order_id or not payment_id or not signature:
            return self.send_json({"error": "payment details adhoori hain"},
                                  400)
        order_path = os.path.join(job_dir(job_id), "order.json")
        if os.path.exists(order_path):
            with open(order_path, encoding="utf-8") as handle:
                stored = json.load(handle)
            if stored.get("order_id") != order_id:
                return self.send_json(
                    {"error": "order match nahi hua"}, 400)
        if not verify_razorpay_signature(order_id, payment_id, signature):
            return self.send_json(
                {"error": "payment verify nahi hua — signature galat hai"},
                400)
        with open(os.path.join(job_dir(job_id), "paid.json"), "w",
                  encoding="utf-8") as handle:
            json.dump({"payment_id": payment_id, "order_id": order_id,
                       "amount_paise": PRICE_PAISE,
                       "paid_at": time.strftime("%Y-%m-%d %H:%M:%S",
                                                  time.gmtime())},
                      handle, ensure_ascii=False)
        return self.send_json({"paid": True})

    def serve_file(self, path, content_type, extra_headers=None):
        try:
            size = os.path.getsize(path)
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(size))
            self.send_header("Accept-Ranges", "bytes")
            for name, value in (extra_headers or []):
                self.send_header(name, value)
            self.end_headers()
            with open(path, "rb") as handle:
                while True:
                    chunk = handle.read(1024 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except OSError:
            self.send_json({"error": "file not found"}, 404)


def main():
    parser = argparse.ArgumentParser(description="Video Studio server")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    print("Video Studio (website version) chal raha hai: "
          "http://%s:%d" % (args.host, args.port))
    print("Google login configured: %s" % oauth_configured())
    print("Razorpay payment configured: %s" % payments_configured())
    print("Rokne ke liye Ctrl+C dabayein.")
    recover_interrupted_jobs()
    threading.Thread(target=worker, daemon=True).start()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nBand ho raha hai...")


if __name__ == "__main__":
    main()
