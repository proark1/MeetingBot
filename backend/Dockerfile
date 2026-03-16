FROM python:3.12-slim

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
    fonts-liberation \
    ca-certificates \
    curl \
    wget \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Flush Python stdout/stderr immediately so logs appear in Railway even if the process crashes
ENV PYTHONUNBUFFERED=1

# ── Python dependencies ───────────────────────────────────────────────────────
COPY requirements.txt requirements-crypto.txt ./
# Core deps first (fast — no heavy native extensions)
RUN pip install --no-cache-dir --prefer-binary -r requirements.txt
# Crypto deps in a separate layer (web3 + eth-account are large).
# If this step times out or fails, the app still starts and USDC payments return 503.
RUN pip install --no-cache-dir --prefer-binary -r requirements-crypto.txt || \
    echo "WARNING: Crypto packages (web3/eth-account) could not be installed. USDC payments will be unavailable."

# ── Playwright: install Chromium and its system deps ─────────────────────────
RUN playwright install chromium
# playwright install-deps fails on Debian Trixie due to missing ttf-ubuntu-font-family/ttf-unifont
# Install the Debian equivalents manually
RUN apt-get update && apt-get install -y --no-install-recommends \
    fonts-unifont \
    && rm -rf /var/lib/apt/lists/*

# ── Application source ────────────────────────────────────────────────────────
COPY . .
RUN chmod +x /app/start.sh
RUN test -f /app/frontend/index.html || (echo "ERROR: frontend/index.html not found in build context" && exit 1)

# Verify all Python imports resolve correctly — fails the build if there are errors
RUN python -c "from app.main import app; print('Import verification passed')"

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -sf "http://localhost:${PORT:-8000}/health" || exit 1

# start.sh initialises PulseAudio, then starts uvicorn
# Use shell form so ${PORT:-8000} is expanded (Railway injects PORT)
CMD ["/bin/sh", "-c", "/app/start.sh uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
