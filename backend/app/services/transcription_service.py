"""Gemini-powered audio transcription.

Uploads the meeting WAV file to the Gemini Files API and asks Gemini to
transcribe it with speaker labels and timestamps. Returns the same format
used throughout the app:
    [{"speaker": "Alice", "text": "...", "timestamp": 12.5}, ...]
"""

import asyncio
import json
import logging
import os
import re
import time
from typing import Any

logger = logging.getLogger(__name__)

_TRANSCRIPTION_PROMPT = """
Transcribe the audio recording of this meeting.

Return ONLY a valid JSON array — no markdown fences, no prose outside the array.

Each element of the array must be an object with exactly these keys:
  "speaker"   — the speaker's name, or "Participant 1" / "Participant 2" etc. if
                 names cannot be determined from context
  "text"      — what that speaker said (clean, no filler trimming needed)
  "timestamp" — number of seconds from the start of the recording (float)

Rules:
- Identify distinct voices and give each a consistent label throughout.
- If a real name is said in the meeting (e.g. "Thanks, Sarah"), use it.
- Split long monologues into natural sentence-level entries.
- Omit silences, background noise, and unintelligible segments.
- Do not add commentary, summaries, or any text outside the JSON array.
""".strip()


async def transcribe_audio(audio_path: str, known_participants: list[str] | None = None) -> list[dict[str, Any]]:
    """
    Transcribe an audio file using the Gemini API.

    Args:
        audio_path: Path to a WAV (or MP3/M4A) file.

    Returns:
        List of transcript entries, or [] if transcription fails.
    """
    from app.config import settings  # imported here to avoid circular import

    if not settings.GEMINI_API_KEY:
        logger.error("GEMINI_API_KEY is not set — cannot transcribe")
        return []

    if not os.path.exists(audio_path):
        logger.error("Audio file not found: %s", audio_path)
        return []

    size = os.path.getsize(audio_path)
    logger.info("Audio file size: %d bytes (%s)", size, audio_path)
    if size < 8192:
        logger.warning("Audio file is too small (%d bytes) — likely no audio captured", size)
        return []

    try:
        import google.generativeai as genai
    except ImportError:
        logger.error("google-generativeai is not installed — run: pip install google-generativeai")
        return []

    genai.configure(api_key=settings.GEMINI_API_KEY)

    uploaded = None
    try:
        # Upload audio to the Files API (handles files of any size)
        logger.info("Uploading audio to Gemini Files API (%d bytes)…", size)
        uploaded = await asyncio.to_thread(
            genai.upload_file, audio_path, mime_type="audio/wav"
        )

        # Wait until processing is complete
        for _ in range(30):
            if uploaded.state.name != "PROCESSING":
                break
            await asyncio.sleep(2)
            uploaded = await asyncio.to_thread(genai.get_file, uploaded.name)

        if uploaded.state.name != "ACTIVE":
            logger.error("Gemini file upload failed — state: %s", uploaded.state.name)
            return []

        logger.info("Audio uploaded (%s) — transcribing…", uploaded.name)
        prompt = _TRANSCRIPTION_PROMPT
        if known_participants:
            names_list = ", ".join(known_participants)
            prompt += f"\n\nKnown participants in this meeting: {names_list}. Use these exact names for speaker labels where you can match the voice."
        model = genai.GenerativeModel("gemini-2.5-flash")
        response = await model.generate_content_async(
            [prompt, uploaded],
            generation_config={"temperature": 0, "max_output_tokens": 8192},
        )

        raw = response.text.strip()

        # Extract the JSON array from wherever Gemini put it.
        # Gemini sometimes wraps the array in prose or markdown fences;
        # we find the first '[' … matching ']' to be robust about it.
        json_match = re.search(r"\[[\s\S]*\]", raw)
        if json_match:
            raw = json_match.group(0)
        else:
            logger.error(
                "Gemini response contains no JSON array. First 500 chars: %s",
                raw[:500],
            )
            return []

        transcript = json.loads(raw)
        logger.info("Transcription complete: %d entries", len(transcript))
        return transcript

    except json.JSONDecodeError as exc:
        logger.error(
            "Gemini returned invalid JSON for transcript (%s). Raw (first 500): %s",
            exc, raw[:500] if "raw" in dir() else "<unavailable>",
        )
        return []
    except Exception as exc:
        logger.error("Transcription error: %s", exc)
        return []
    finally:
        # Clean up uploaded file from Gemini storage
        if uploaded:
            try:
                await asyncio.to_thread(genai.delete_file, uploaded.name)
            except Exception:
                pass
