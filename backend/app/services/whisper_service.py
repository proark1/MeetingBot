"""Local Whisper transcription service.

Privacy-preserving alternative to Gemini transcription — audio never leaves
the server. Requires the `openai-whisper` or `faster-whisper` package.

Configuration (see config.py):
  WHISPER_ENABLED  — set True to allow bots to use Whisper
  WHISPER_MODEL    — model size: tiny, base, small, medium, large (default: base)
  WHISPER_DEVICE   — "cpu" or "cuda" (default: cpu)

Usage:
  Set `transcription_provider: "whisper"` when creating a bot.
  The system falls back to Gemini if Whisper is disabled or unavailable.
"""

import asyncio
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# Module-level model cache so we only load the model once.
_whisper_model = None
_whisper_model_name: Optional[str] = None


def _detect_backend() -> Optional[str]:
    """Probe installed Whisper packages once at import time.

    Returns ``"faster"`` (preferred), ``"openai"``, or ``None`` if neither is
    installed. Cached at module load so callers don't re-run import probing on
    every transcription request.
    """
    try:
        from faster_whisper import WhisperModel  # noqa: F401
        return "faster"
    except ImportError:
        pass
    try:
        import whisper  # noqa: F401
        return "openai"
    except ImportError:
        return None


# Detected once at module load — package availability doesn't change at runtime.
_WHISPER_BACKEND: Optional[str] = _detect_backend()


def _get_settings():
    from app.config import settings
    return settings


def is_whisper_available() -> bool:
    """Return True if Whisper is enabled and a backend package is installed."""
    s = _get_settings()
    if not s.WHISPER_ENABLED:
        return False
    return _WHISPER_BACKEND is not None


def _load_model():
    """Load (and cache) the Whisper model."""
    global _whisper_model, _whisper_model_name
    s = _get_settings()
    model_name = s.WHISPER_MODEL
    device = s.WHISPER_DEVICE

    if _whisper_model is not None and _whisper_model_name == model_name:
        return _whisper_model

    # Prefer faster-whisper for GPU efficiency; fall back to openai-whisper
    try:
        from faster_whisper import WhisperModel
        compute_type = "float16" if device == "cuda" else "int8"
        logger.info("Loading faster-whisper model '%s' on %s", model_name, device)
        _whisper_model = WhisperModel(model_name, device=device, compute_type=compute_type)
        _whisper_model_name = model_name
        return _whisper_model
    except ImportError:
        pass

    try:
        import whisper
        logger.info("Loading openai-whisper model '%s' on %s", model_name, device)
        _whisper_model = whisper.load_model(model_name, device=device)
        _whisper_model_name = model_name
        return _whisper_model
    except ImportError:
        raise RuntimeError(
            "Whisper is not installed. Run: pip install faster-whisper  "
            "(or: pip install openai-whisper)"
        )


def _transcribe_with_faster_whisper(audio_path: str, language: Optional[str] = None) -> list[dict]:
    """Transcribe using faster-whisper. Returns [{speaker, text, timestamp}]."""
    from faster_whisper import WhisperModel
    model = _load_model()
    if not isinstance(model, WhisperModel):
        raise RuntimeError("faster-whisper model not loaded")

    from app.config import settings
    segments, info = model.transcribe(
        audio_path,
        language=language or None,
        beam_size=max(1, settings.WHISPER_BEAM_SIZE),
        vad_filter=True,
    )
    logger.info(
        "Whisper detected language '%s' (probability %.2f)",
        info.language,
        info.language_probability,
    )

    transcript = []
    for seg in segments:
        transcript.append({
            "speaker": "Speaker",  # faster-whisper doesn't diarize by default
            "text": seg.text.strip(),
            "timestamp": round(seg.start, 2),
        })
    return transcript


def _transcribe_with_openai_whisper(audio_path: str, language: Optional[str] = None) -> list[dict]:
    """Transcribe using openai-whisper. Returns [{speaker, text, timestamp}]."""
    import whisper
    model = _load_model()

    options = {"language": language} if language else {}
    result = model.transcribe(audio_path, **options)

    transcript = []
    for seg in result.get("segments", []):
        transcript.append({
            "speaker": "Speaker",
            "text": seg["text"].strip(),
            "timestamp": round(seg["start"], 2),
        })
    return transcript


async def transcribe_with_whisper(
    audio_path: str,
    language: Optional[str] = None,
    known_participants: Optional[list[str]] = None,
) -> list[dict]:
    """Transcribe audio using local Whisper model.

    Returns a list of {speaker, text, timestamp} dicts.
    Falls back to empty list on error (caller handles fallback to Gemini).
    """
    if not os.path.exists(audio_path) or os.path.getsize(audio_path) == 0:
        logger.warning("Whisper: audio file missing or empty — %s", audio_path)
        return []

    if not is_whisper_available():
        logger.error("Whisper requested but not available — check WHISPER_ENABLED and package install")
        return []

    try:
        # Backend detected once at module load (faster-whisper preferred).
        if _WHISPER_BACKEND == "faster":
            transcript = await asyncio.to_thread(
                _transcribe_with_faster_whisper, audio_path, language
            )
        else:
            transcript = await asyncio.to_thread(
                _transcribe_with_openai_whisper, audio_path, language
            )

        logger.info(
            "Whisper transcription complete: %d segments from %s",
            len(transcript),
            audio_path,
        )

        # If we have known participant names, try to assign them to speakers
        # (simple heuristic: rotate through names if only one speaker was detected)
        if known_participants and transcript:
            speakers_detected = {e["speaker"] for e in transcript}
            if len(speakers_detected) <= 1 and len(known_participants) > 1:
                logger.info(
                    "Whisper: single speaker detected, known participants: %s",
                    known_participants,
                )
                # Can't diarize without a separate model — keep as-is

        return transcript

    except Exception as exc:
        logger.error("Whisper transcription error for %s: %s", audio_path, exc)
        return []
