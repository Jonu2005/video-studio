# Video Studio — free hosting on Hugging Face Spaces (Docker SDK).
#
# What this image does: installs Python + ffmpeg + espeak-ng, downloads the
# free Kokoro voice models (~190MB, Apache 2.0), and runs the Video Studio
# server on port 7860 (the port Hugging Face expects).

FROM python:3.10-slim

# System tools: ffmpeg (video), espeak-ng (Kokoro phonemes),
# fonts for Hindi + English captions.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg espeak-ng libespeak-ng1 \
    fonts-noto-core fonts-dejavu-core curl \
    && rm -rf /var/lib/apt/lists/* \
    && test -d /usr/share/espeak-ng-data \
    && test -f /usr/lib/x86_64-linux-gnu/libespeak-ng.so.1

WORKDIR /app

# Python deps (engine + Kokoro TTS).
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Kokoro voice models (free, Apache 2.0).
RUN mkdir -p /app/models && \
    curl -L -o /app/models/kokoro-v1.0.fp16.onnx \
      https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.fp16.onnx && \
    curl -L -o /app/models/voices-v1.0.bin \
      https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin && \
    ls -la /app/models/

# App code.
COPY engine/ ./engine/
COPY server/ ./server/
COPY config.json ./
COPY music/ ./music/
RUN mkdir -p jobs

ENV KOKORO_MODEL=/app/models/kokoro-v1.0.fp16.onnx \
    KOKORO_VOICES=/app/models/voices-v1.0.bin \
    ESPEAK_LIB=/usr/lib/x86_64-linux-gnu/libespeak-ng.so.1 \
    ESPEAK_DATA=/usr/share/espeak-ng-data \
    VIDEO_STUDIO_KOKORO_PYTHON=python3 \
    PYTHONUNBUFFERED=1

EXPOSE 7860
CMD ["sh", "-c", "python3 server/app.py --host 0.0.0.0 --port ${PORT:-7860}"]
