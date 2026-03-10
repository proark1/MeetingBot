"""Gemini-powered audio transcription.

Uploads the meeting WAV file to the Gemini Files API and asks Gemini to
transcribe it with speaker labels and timestamps. Returns the same format
used throughout the app:
    [{"speaker": "Alice", "text": "...", "timestamp": 12.5}, ...]
"""

import asyncio
import glob as _glob
import json
import logging
import os
import re
import time
import uuid
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


_CHUNK_THRESHOLD_S = 2100   # 35 min — below this, transcribe as a single file
_CHUNK_SIZE_S      = 1800   # 30 min per chunk


def _estimate_duration_s(file_path: str) -> float:
    """Rough duration estimate from file size (16 kHz mono PCM = 32 000 bytes/s)."""
    return os.path.getsize(file_path) / 32_000


async def _split_audio(audio_path: str, chunk_s: int = _CHUNK_SIZE_S) -> list[str]:
    """
    Split *audio_path* into ≤chunk_s-second WAV segments using ffmpeg.
    Returns a sorted list of temp file paths (caller must delete them).
    """
    uid = uuid.uuid4().hex
    pattern = f"/tmp/chunk_{uid}_%03d.wav"
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-i", audio_path,
        "-f", "segment", "-segment_time", str(chunk_s),
        "-c", "copy", pattern,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
    chunks = sorted(_glob.glob(f"/tmp/chunk_{uid}_*.wav"))
    logger.info("Split %s into %d chunk(s)", audio_path, len(chunks))
    return chunks


async def _transcribe_chunked(
    audio_path: str,
    known_participants: list[str] | None,
    estimated_s: float,
) -> list[dict[str, Any]]:
    """Split the audio into 30-min chunks and transcribe them sequentially."""
    logger.info(
        "Long recording (~%.0f min) — splitting into chunks of %d s",
        estimated_s / 60, _CHUNK_SIZE_S,
    )
    chunks = await _split_audio(audio_path, _CHUNK_SIZE_S)
    if not chunks:
        logger.error("Audio split produced no chunks — cannot transcribe")
        return []

    all_entries: list[dict[str, Any]] = []
    chunk_files = list(chunks)
    try:
        for idx, chunk_path in enumerate(chunk_files):
            offset_s = idx * _CHUNK_SIZE_S
            logger.info("Transcribing chunk %d/%d (offset %d s)…", idx + 1, len(chunk_files), offset_s)
            entries = await transcribe_audio(chunk_path, known_participants)
            for entry in entries:
                entry = dict(entry)
                entry["timestamp"] = float(entry.get("timestamp", 0)) + offset_s
                all_entries.append(entry)
    finally:
        for path in chunk_files:
            try:
                os.unlink(path)
            except OSError:
                pass

    logger.info("Chunked transcription complete: %d total entries from %d chunks", len(all_entries), len(chunk_files))
    return all_entries


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

    # For long recordings, split into chunks and transcribe sequentially
    estimated_s = _estimate_duration_s(audio_path)
    if estimated_s > _CHUNK_THRESHOLD_S:
        return await _transcribe_chunked(audio_path, known_participants, estimated_s)

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

        # Robustly extract the JSON array — Gemini sometimes wraps it in prose
        # or markdown fences.  Try multiple strategies in order:
        transcript = None
        # 1. Direct parse (response is already clean JSON)
        try:
            transcript = json.loads(raw)
        except json.JSONDecodeError:
            pass
        # 2. Strip markdown fences then parse
        if transcript is None:
            cleaned = re.sub(r"```(?:json)?\s*", "", raw).replace("```", "").strip()
            try:
                transcript = json.loads(cleaned)
            except json.JSONDecodeError:
                pass
        # 3. Regex-extract the outermost [...] block
        if transcript is None:
            m = re.search(r"\[[\s\S]*\]", raw)
            if m:
                try:
                    transcript = json.loads(m.group(0))
                except json.JSONDecodeError:
                    pass
        if transcript is None:
            logger.error(
                "Gemini response could not be parsed as JSON array. First 500 chars: %s",
                raw[:500],
            )
            return []

        # Validate each entry has the required keys before returning
        _REQUIRED = {"speaker", "text", "timestamp"}
        validated = [e for e in transcript if isinstance(e, dict) and _REQUIRED.issubset(e)]
        skipped = len(transcript) - len(validated)
        if skipped:
            logger.warning("Skipped %d malformed transcript entry(ies) from Gemini", skipped)
        if not validated and transcript:
            logger.error("All %d transcript entries were malformed — returning empty", len(transcript))
        logger.info("Transcription complete: %d valid entries", len(validated))
        return validated

    except json.JSONDecodeError as exc:
        logger.error(
            "Gemini returned invalid JSON for transcript (%s). Raw (first 500): %s",
            exc, raw[:500] if "raw" in dir() else "<unavailable>",
        )
        return []
    except ValueError as exc:
        logger.warning("Gemini transcription blocked by safety filter: %s", exc)
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
