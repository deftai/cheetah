# No JavaScript runtime in this image on purpose: the slice-6 spike fetched captions without one
# (yt-dlp 2026.08.19, default player client). Re-check that whenever yt-dlp is bumped.
# Python 3.12.4 is the minimum (main.py refuses to start on older); the 3.12 tag stays at or above it.
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY requirements.txt ./
RUN pip install -r requirements.txt \
    && useradd -r -u 1001 appuser

COPY transcript_worker/ ./transcript_worker/

USER appuser
CMD ["python", "-m", "transcript_worker"]
