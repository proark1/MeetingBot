#!/usr/bin/env bash
# Entrypoint: start PulseAudio (required for meeting audio capture) then run the app.
set -e

# PulseAudio needs a writable runtime dir
export XDG_RUNTIME_DIR=/tmp/runtime-meetingbot
mkdir -p "$XDG_RUNTIME_DIR"
chmod 700 "$XDG_RUNTIME_DIR"

# Start PulseAudio daemon if not already running.
# --exit-idle-time=-1  keeps it alive even when no sinks are active.
# --log-target=stderr  surfaces errors in Docker logs.
if ! pulseaudio --check 2>/dev/null; then
    echo "[start.sh] Starting PulseAudio…"
    pulseaudio \
        --daemonize=yes \
        --exit-idle-time=-1 \
        --log-target=stderr \
        --log-level=warn \
        2>/dev/null || echo "[start.sh] Warning: PulseAudio failed to start (audio capture disabled)"
    sleep 1
else
    echo "[start.sh] PulseAudio already running"
fi

# Hand off to the main process (e.g. uvicorn), or use args if provided
if [ $# -gt 0 ]; then
    exec "$@"
else
    exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
fi
