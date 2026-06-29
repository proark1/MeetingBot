FROM python:3.12-slim-bookworm

# ── System dependencies ───────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Audio capture
    ffmpeg \
    pulseaudio \
    pulseaudio-utils \
    # Virtual display (headed Chromium is more compatible with Google Meet/Teams)
    xvfb \
    x11-utils \
    # Chromium / Playwright runtime libraries
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libdbus-1-3 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    libpango-1.0-0 \
    libcairo2 \
    libglib2.0-0 \
    libx11-6 \
    libx11-xcb1 \
    libxcb1 \
    libxext6 \
    libxmlsec1 \
    libxmlsec1-openssl \
    fonts-liberation \
    ca-certificates \
    curl \
    wget \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Flush Python stdout/stderr immediately so logs appear in Railway even if the process crashes
ENV PYTHONUNBUFFERED=1
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# ── Python dependencies ───────────────────────────────────────────────────────
COPY backend/requirements.txt requirements.txt
COPY backend/requirements-crypto.txt requirements-crypto.txt
# Core deps first (fast — no heavy native extensions)
RUN pip install --no-cache-dir --prefer-binary -r requirements.txt
# Crypto deps in a separate layer (web3 + eth-account are large); fail the
# image build if declared payment dependencies cannot be installed.
RUN pip install --no-cache-dir --prefer-binary -r requirements-crypto.txt

# ── Playwright: install Chromium and its system deps ─────────────────────────
RUN playwright install chromium && chmod -R a+rX /ms-playwright
# Install additional font coverage used by headed Chromium sessions.
RUN apt-get update && apt-get install -y --no-install-recommends \
    fonts-unifont \
    && rm -rf /var/lib/apt/lists/*

# ── Application source ────────────────────────────────────────────────────────
RUN groupadd --system meetingbot \
    && useradd --system --gid meetingbot --home-dir /home/meetingbot --create-home --shell /usr/sbin/nologin meetingbot
COPY --chown=meetingbot:meetingbot backend/app/ app/
COPY --chown=meetingbot:meetingbot frontend/ frontend/
COPY --chown=meetingbot:meetingbot backend/start.sh start.sh
RUN chmod +x /app/start.sh \
    && mkdir -p /app/data/recordings /app/data/screenshots /app/data/debug /tmp/runtime-meetingbot \
    && chown -R meetingbot:meetingbot /app /tmp/runtime-meetingbot /ms-playwright /home/meetingbot \
    && chmod 700 /tmp/runtime-meetingbot
RUN test -f /app/frontend/index.html || (echo "ERROR: frontend/index.html not found in build context" && exit 1)

# Verify all Python imports resolve correctly — fails the build if there are errors
USER meetingbot
RUN python -c "from app.main import app; print('Import verification passed')"

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -sf "http://localhost:${PORT:-8000}/health" || exit 1

# start.sh initialises PulseAudio, then starts uvicorn
# Use shell form so ${PORT:-8000} is expanded (Railway injects PORT)
CMD ["/bin/sh", "-c", "/app/start.sh uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
