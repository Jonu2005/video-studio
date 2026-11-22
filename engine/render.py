"""ffmpeg helpers for Video Studio."""

import json
import subprocess


class RenderError(Exception):
    """Raised when an ffmpeg/ffprobe step fails."""


def run_command(cmd, description):
    """Run a command, raising RenderError with its output on failure."""
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RenderError(
            f"{description} failed (exit {result.returncode}):\n"
            f"command: {' '.join(cmd)}\n"
            f"stderr: {result.stderr[-2000:]}"
        )
    return result


def probe_media(path):
    """Return ffprobe JSON for a media file."""
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json",
         "-show_format", "-show_streams", path],
        capture_output=True, text=True)
    if result.returncode != 0:
        raise RenderError(f"ffprobe failed on {path}: {result.stderr}")
    return json.loads(result.stdout)


def media_duration(path):
    """Duration of a media file in seconds (float)."""
    info = probe_media(path)
    duration = info.get("format", {}).get("duration")
    if duration:
        return float(duration)
    for stream in info.get("streams", []):
        if stream.get("duration"):
            return float(stream["duration"])
    raise RenderError(f"could not determine duration of {path}")


def volumedetect(path):
    """Return (mean_volume_db, max_volume_db) via ffmpeg volumedetect."""
    result = subprocess.run(
        ["ffmpeg", "-i", path, "-vn", "-af", "volumedetect",
         "-f", "null", "-"],
        capture_output=True, text=True)
    mean_db, max_db = None, None
    for line in result.stderr.splitlines():
        line = line.strip()
        if "mean_volume:" in line:
            mean_db = float(line.split("mean_volume:")[1].split("dB")[0])
        elif "max_volume:" in line:
            max_db = float(line.split("max_volume:")[1].split("dB")[0])
    if mean_db is None:
        raise RenderError(f"volumedetect found no levels for {path}")
    return mean_db, max_db
