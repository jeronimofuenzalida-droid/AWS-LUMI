FROM public.ecr.aws/docker/library/python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV HF_HOME=/tmp/hf
ENV XDG_CACHE_HOME=/tmp/.cache
ENV TORCH_HOME=/tmp/torch
ARG PRELOAD_WHISPER_MODEL=medium

RUN apt-get update \
  && apt-get install -y --no-install-recommends ffmpeg libsndfile1 \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Best-effort model prefetch to reduce first-job latency.
RUN python - <<'PY'
import os
model_name = os.environ.get("PRELOAD_WHISPER_MODEL", "").strip()
if model_name:
    try:
        from faster_whisper import WhisperModel
        WhisperModel(model_name, device="cpu", compute_type="int8")
        print(f"Prefetched whisper model: {model_name}")
    except Exception as exc:
        print(f"Whisper prefetch skipped: {exc}")
PY

COPY app.py /app/app.py

ENTRYPOINT ["python", "-u", "/app/app.py"]
