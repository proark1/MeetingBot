"""Whisper-based audio transcription.

Uses faster-whisper (CTranslate2 backend) to transcribe meeting audio into
timestamped transcript entries. Speaker labels are inferred from silence gaps
between utterances (a simple but effective heuristic for meetings).
"""

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# Gap in seconds between segments that suggests a speaker change
_SPEAKER_CHANGE_GAP = 1.5
# How many distinct "speakers" to cycle through when using gap heuristic
_MAX_HEURISTIC_SPEAKERS = 6


async def transcribe_audio(
    audio_path: str,
    model_size: str = "base",
) -> list[dict[str, Any]]:
    """
    Transcribe a WAV/MP3/M4A file and return a list of transcript entries.

    Each entry:
        {"speaker": "Participant 1", "text": "...", "timestamp": 12.34}

    Args:
        audio_path:  Path to the audio file.
        model_size:  Whisper model size — "tiny", "base", "small", "medium", "large".
                     "base" is the default (good accuracy, fast on CPU).
                     Use "small" or "medium" for better accuracy at higher cost.
    """
    if not os.path.exists(audio_path):
        logger.error("Audio file not found: %s", audio_path)
        return []

    file_size = os.path.getsize(audio_path)
    if file_size < 4096:  # essentially empty
        logger.warning("Audio file too small (%d bytes) — likely no audio was captured", file_size)
        return []

    try:
        from faster_whisper import WhisperModel
    except ImportError:
        logger.error(
            "faster-whisper is not installed. "
            "Run: pip install faster-whisper"
        )
        return []

    logger.info("Loading Whisper model '%s'…", model_size)
    try:
        # int8 quantization: fastest on CPU, negligible quality loss for speech
        model = WhisperModel(model_size, device="cpu", compute_type="int8")
    except Exception as exc:
        logger.error("Failed to load Whisper model: %s", exc)
        return []

    logger.info("Transcribing %s…", audio_path)
    try:
        segments, info = model.transcribe(
            audio_path,
            beam_size=5,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 300},
            word_timestamps=False,
        )

        logger.info(
            "Detected language: %s (confidence %.0f%%)",
            info.language,
            info.language_probability * 100,
        )

        transcript: list[dict[str, Any]] = []
        speaker_idx = 0
        last_end = 0.0

        for seg in segments:
            text = seg.text.strip()
            if not text:
                continue

            # Heuristic: a gap longer than the threshold suggests a new speaker
            if transcript and (seg.start - last_end) > _SPEAKER_CHANGE_GAP:
                speaker_idx = (speaker_idx + 1) % _MAX_HEURISTIC_SPEAKERS

            transcript.append(
                {
                    "speaker": f"Participant {speaker_idx + 1}",
                    "text": text,
                    "timestamp": round(seg.start, 2),
                }
            )
            last_end = seg.end

        logger.info("Transcription done: %d segments", len(transcript))
        return transcript

    except Exception as exc:
        logger.error("Transcription failed: %s", exc)
        return []
