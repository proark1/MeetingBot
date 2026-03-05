FROM python:3.12-slim

# ── System dependencies ───────────────────────────────────────────────────────
# ffmpeg      — audio recording from PulseAudio virtual sink
# pulseaudio  — virtual audio sink for capturing meeting audio
# xvfb        — virtual framebuffer (headed Chromium needs a display)
# Chromium libs — required by Playwright's bundled Chromium
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    pulseaudio \
    pulseaudio-utils \
    xvfb \
    x11-utils \
    # Chromium / Playwright system deps
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
    wget \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ── Python dependencies ───────────────────────────────────────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Playwright: install Chromium browser ──────────────────────────────────────
RUN playwright install chromium
RUN playwright install-deps chromium

# ── Application source ────────────────────────────────────────────────────────
COPY . .

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
