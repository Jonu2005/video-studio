# Video Studio — free hosting on DeployVera (Docker, Coolify-based).
#
# What this image does: installs Python + ffmpeg + espeak-ng, downloads
# the free Piper voice models (~127MB, MIT), and runs the Video Studio
# server on the port from $PORT (default 7860).
#
# Narration uses Piper, not Kokoro (Nov 2026): Kokoro needs ~580MB
# peak RSS on CPU — more than the 512MB free container, so renders
# stalled forever on the narration step. Piper needs ~249MB.

FROM python:3.10-slim

# System tools: ffmpeg (video), espeak-ng (TTS phonemes fallback),
# fonts for Hindi + English captions.
# NOTE (23 Nov 2026): Debian 13 (Trixie) moved the espeak data dir from
# /usr/share/espeak-ng-data to /usr/lib/x86_64-linux-gnu/espeak-ng-data,
# and espeak-ng does NOT depend on espeak-ng-data here — install it
# explicitly and check the NEW path (the old path failed 2 builds).
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg espeak-ng espeak-ng-data libespeak-ng1 \
    fonts-noto-core fonts-dejavu-core curl \
    && rm -rf /var/lib/apt/lists/* \
    && test -d /usr/lib/x86_64-linux-gnu/espeak-ng-data \
    && test -f /usr/lib/x86_64-linux-gnu/libespeak-ng.so.1 \
    && espeak-ng --version

WORKDIR /app

# Python deps (engine + Piper TTS).
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Piper voice models (free, MIT) from the rhasspy/piper-voices hub:
# Hindi priyamvada (female) + English amy (female), medium quality.
RUN mkdir -p /app/models/piper && \
    curl -L -o /app/models/piper/hi_IN-priyamvada-medium.onnx \
      https://huggingface.co/rhasspy/piper-voices/resolve/main/hi/hi_IN/priyamvada/medium/hi_IN-priyamvada-medium.onnx && \
    curl -L -o /app/models/piper/hi_IN-priyamvada-medium.onnx.json \
      https://huggingface.co/rhasspy/piper-voices/resolve/main/hi/hi_IN/priyamvada/medium/hi_IN-priyamvada-medium.onnx.json && \
    curl -L -o /app/models/piper/en_US-amy-medium.onnx \
      https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/amy/medium/en_US-amy-medium.onnx && \
    curl -L -o /app/models/piper/en_US-amy-medium.onnx.json \
      https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/amy/medium/en_US-amy-medium.onnx.json && \
    ls -la /app/models/piper/

# App code.
COPY engine/ ./engine/
COPY server/ ./server/
COPY config.json ./
COPY music/ ./music/
RUN mkdir -p jobs

ENV PIPER_VOICE_DIR=/app/models/piper \
    ESPEAK_LIB=/usr/lib/x86_64-linux-gnu/libespeak-ng.so.1 \
    ESPEAK_DATA=/usr/lib/x86_64-linux-gnu/espeak-ng-data \
    VIDEO_STUDIO_NARRATOR_PYTHON=python3 \
    PYTHONUNBUFFERED=1

EXPOSE 7860
CMD ["sh", "-c", "python3 server/app.py --host 0.0.0.0 --port ${PORT:-7860}"]
