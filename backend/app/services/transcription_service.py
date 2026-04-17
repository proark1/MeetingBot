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
from typing import Any, Optional

logger = logging.getLogger(__name__)

_TRANSCRIPTION_PROMPT = """
Transcribe the audio recording of this meeting with accurate speaker diarization.

Return ONLY a valid JSON array — no markdown fences, no prose outside the array.

Each element of the array must be an object with exactly these keys:
  "speaker"   — the speaker's name, or "Participant 1" / "Participant 2" etc. if
                 names cannot be determined from context
  "text"      — what that speaker said (clean, no filler trimming needed)
  "timestamp" — number of seconds from the start of the recording (float)

Speaker diarization rules:
- Carefully distinguish distinct voices by their acoustic characteristics (pitch,
  pace, accent, speaking style) — this is the most critical part.
- Give each distinct voice a consistent label throughout the ENTIRE recording.
- If a real name is spoken (e.g. "Thanks, Sarah", "I agree with John"), use that
  name as the speaker label for that person going forward.
- When two voices sound similar, pay extra attention to context clues (topic,
  direction of address) to separate them correctly.
- Number unnamed speakers as "Participant 1", "Participant 2", etc. — do NOT
  reuse numbers across different people.
- Do NOT merge different speakers into the same label unless you are certain
  they are the same voice.
- Split long monologues into sentence-level entries for readability.
- Omit silences, background noise, and completely unintelligible segments.
- Do not add commentary, summaries, or any text outside the JSON array.
- IMPORTANT: If the audio is COMPLETELY silent (no speech whatsoever), return an
  empty array: []. If there is ANY recognisable speech, even noisy or imperfect,
  transcribe it — do not return [] just because audio quality is low.
""".strip()


_CHUNK_THRESHOLD_S = 2100   # 35 min — below this, transcribe as a single file
_CHUNK_SIZE_S      = 1800   # 30 min per chunk

# Silence detection — peak amplitude below this threshold (out of 32 768 for
# 16-bit PCM) means the WAV is effectively silent.  Sending a silent recording
# to an LLM risks hallucinated transcripts; we return [] instead.
_SILENCE_PEAK_THRESHOLD = 200   # ~0.6 % of full scale; quiet speech is typically 1 000+
_SILENCE_SAMPLE_FRAMES  = 16_000  # ~1 s at 16 kHz — checked at several offsets


def _estimate_duration_s(file_path: str) -> float:
    """Rough duration estimate from file size (16 kHz mono PCM = 32 000 bytes/s)."""
    return os.path.getsize(file_path) / 32_000


def _check_audio_has_speech(audio_path: str) -> tuple[bool, float]:
    """
    Return (has_speech, peak_amplitude) for a WAV file.

    Samples frames at multiple offsets across the file (10%, 25%, 50%, 75%, 90%)
    and returns the highest peak found.  Uses only stdlib — no extra deps.

    Returns ``(True, peak)`` if speech is likely present, or ``(False, peak)``
    if the recording is below the silence threshold.  On any error returns
    ``(True, -1.0)`` so that uncertain files are still sent for transcription.
    """
    import wave
    import array as _array

    try:
        with wave.open(audio_path, "rb") as wf:
            if wf.getsampwidth() != 2:          # only handle 16-bit PCM
                return True, -1.0
            total_frames = wf.getnframes()
            if total_frames == 0:
                return False, 0.0

            peak = 0
            for frac in (0.10, 0.25, 0.50, 0.75, 0.90):
                seek = min(int(total_frames * frac), max(0, total_frames - _SILENCE_SAMPLE_FRAMES))
                wf.setpos(seek)
                raw = wf.readframes(_SILENCE_SAMPLE_FRAMES)
                if raw:
                    samples = _array.array("h", raw)
                    local_peak = max(abs(s) for s in samples) if samples else 0
                    peak = max(peak, local_peak)
                    if peak >= _SILENCE_PEAK_THRESHOLD:
                        break   # already found audible content

            return peak >= _SILENCE_PEAK_THRESHOLD, float(peak)

    except Exception as exc:
        logger.debug("Audio silence check error (will proceed with transcription): %s", exc)
        return True, -1.0   # on error, assume speech is present (safer)


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
    language: str | None = None,
) -> list[dict[str, Any]]:
    """Split the audio into 30-min chunks and transcribe them in parallel."""
    logger.info(
        "Long recording (~%.0f min) — splitting into chunks of %d s",
        estimated_s / 60, _CHUNK_SIZE_S,
    )
    chunks = await _split_audio(audio_path, _CHUNK_SIZE_S)
    if not chunks:
        logger.error("Audio split produced no chunks — cannot transcribe")
        return []

    async def _process_chunk(idx: int, chunk_path: str) -> list[dict[str, Any]]:
        offset_s = idx * _CHUNK_SIZE_S
        logger.info("Transcribing chunk %d/%d (offset %d s)…", idx + 1, len(chunks), offset_s)
        entries = await transcribe_audio(chunk_path, known_participants, language=language)
        return [dict(e, timestamp=float(e.get("timestamp", 0)) + offset_s) for e in entries]

    all_entries: list[dict[str, Any]] = []
    try:
        results = await asyncio.gather(
            *(_process_chunk(i, p) for i, p in enumerate(chunks)),
            return_exceptions=True,
        )
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.error("Chunk %d/%d transcription failed: %s", i + 1, len(chunks), result)
            else:
                all_entries.extend(result)
        # Re-sort by timestamp since parallel results may arrive out of order
        all_entries.sort(key=lambda e: e.get("timestamp", 0))
    finally:
        for path in chunks:
            try:
                os.unlink(path)
            except OSError:
                pass

    logger.info("Chunked transcription complete: %d total entries from %d chunks", len(all_entries), len(chunks))
    return all_entries


def _normalise_speakers(
    entries: list[dict],
    known_participants: list[str] | None,
) -> list[dict]:
    """Normalise speaker labels for consistency.

    1. Map generic numeric labels (e.g. "speaker 1", "participant 2", "person 1")
       to a canonical "Participant N" form.
    2. If known_participants are provided, fuzzy-match model labels against real
       names and replace generic labels where a confident match exists.
    3. Ensure a speaker labelled with only a first name is matched to a full name
       in known_participants where unambiguous.
    """
    if not entries:
        return entries

    # Regex to recognise generic numeric speaker labels from the model
    _generic_re = re.compile(
        r"^(?:speaker|participant|person|voice|unknown|spk|s)\s*[-_]?\s*(\d+)$",
        re.IGNORECASE,
    )

    # First pass: build a mapping of raw model labels → canonical "Participant N"
    label_map: dict[str, str] = {}
    participant_counter: dict[str, int] = {}  # raw_label → assigned number
    _next_num = [1]

    def _canonical(raw: str) -> str:
        m = _generic_re.match(raw.strip())
        if m:
            # Normalise so "Speaker 1" and "speaker 1" map to same key
            key = f"__generic_{m.group(1)}__"
            if key not in participant_counter:
                participant_counter[key] = _next_num[0]
                _next_num[0] += 1
            return f"Participant {participant_counter[key]}"
        return raw.strip()

    for entry in entries:
        raw = entry.get("speaker", "")
        if raw not in label_map:
            label_map[raw] = _canonical(raw)

    # Second pass: if known_participants provided, try to match canonical or raw
    # labels to real names using partial / first-name matching.
    if known_participants:
        known_lower = {n.strip().lower(): n.strip() for n in known_participants if n.strip()}
        known_firstnames: dict[str, str] = {}  # first name → full name (only when unambiguous)
        for full_name in known_lower.values():
            parts = full_name.split()
            if parts:
                fn = parts[0].lower()
                if fn not in known_firstnames:
                    known_firstnames[fn] = full_name
                else:
                    known_firstnames[fn] = ""  # ambiguous — don't use

        for raw, canonical in list(label_map.items()):
            cl = canonical.strip().lower()
            # Exact match against known names
            if cl in known_lower:
                label_map[raw] = known_lower[cl]
                continue
            # First-name match (unambiguous)
            first = cl.split()[0] if cl.split() else ""
            if first and first in known_firstnames and known_firstnames[first]:
                label_map[raw] = known_firstnames[first]

    # Apply mapping
    result = []
    for entry in entries:
        new_entry = dict(entry)
        raw = new_entry.get("speaker", "")
        new_entry["speaker"] = label_map.get(raw, raw)
        result.append(new_entry)

    return result


async def _set_diag(bot_id: Optional[str], **fields) -> None:
    """Best-effort update of diagnostic fields on a BotSession."""
    if not bot_id:
        return
    try:
        from app.store import store as _store
        await _store.update_bot(bot_id, **fields)
    except Exception:
        pass


async def transcribe_audio(
    audio_path: str,
    known_participants: list[str] | None = None,
    language: str | None = None,
    bot_id: Optional[str] = None,
) -> list[dict[str, Any]]:
    """
    Transcribe an audio file using the Gemini API.

    Args:
        audio_path: Path to a WAV (or MP3/M4A) file.
        bot_id: Optional bot id; when provided, per-call diagnostics (peak
            amplitude, Gemini finish_reason, safety blocks) are recorded on
            the BotSession so /api/bots/{bot_id}/debug can surface them.

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
    # 32 000 bytes/s for 16 kHz mono PCM — reject anything shorter than ~1 second
    if size < 8_000:
        logger.warning("Audio file is too small (%d bytes) — skipping transcription", size)
        return []

    # ── Silence detection ────────────────────────────────────────────────────
    # Check whether the WAV contains audible content before paying AI API cost.
    # A completely silent recording (e.g. from a PulseAudio routing failure)
    # would cause Gemini to hallucinate a transcript; we return [] instead.
    has_speech, peak = _check_audio_has_speech(audio_path)
    await _set_diag(bot_id, audio_peak_amplitude=float(peak))
    if not has_speech:
        logger.warning(
            "Audio file appears to be silent (peak amplitude %.0f/32768 < threshold %d) — "
            "returning empty transcript instead of risking hallucination. "
            "If speech was expected, check PulseAudio / Chrome audio routing.",
            peak, _SILENCE_PEAK_THRESHOLD,
        )
        return []

    logger.debug("Audio silence check passed (peak amplitude: %.0f/32768)", peak)

    # For long recordings, split into chunks and transcribe sequentially
    estimated_s = _estimate_duration_s(audio_path)
    if estimated_s > _CHUNK_THRESHOLD_S:
        return await _transcribe_chunked(audio_path, known_participants, estimated_s, language=language)

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

        # Wait until processing is complete using exponential backoff
        # (1 s → 2 s → 4 s … capped at 10 s; give up after 90 s total).
        _poll_delay = 1.0
        _poll_elapsed = 0.0
        while _poll_elapsed < 90 and uploaded.state.name == "PROCESSING":
            await asyncio.sleep(_poll_delay)
            _poll_elapsed += _poll_delay
            _poll_delay = min(_poll_delay * 2, 10.0)
            uploaded = await asyncio.to_thread(genai.get_file, uploaded.name)

        if uploaded.state.name != "ACTIVE":
            logger.error("Gemini file upload failed — state: %s", uploaded.state.name)
            return []

        logger.info("Audio uploaded (%s) — transcribing…", uploaded.name)
        prompt = _TRANSCRIPTION_PROMPT
        if language:
            prompt += f"\n\nThe spoken language is: {language}. Transcribe in that language."
        if known_participants:
            names_list = ", ".join(known_participants)
            prompt += f"\n\nKnown participants in this meeting: {names_list}. Use these exact names for speaker labels where you can match the voice."
        model = genai.GenerativeModel("gemini-2.5-flash")
        _t0 = time.time()
        response = await asyncio.wait_for(
            model.generate_content_async(
                [prompt, uploaded],
                generation_config={"temperature": 0, "max_output_tokens": 65536},
            ),
            timeout=300.0,  # 5 minute safety net for large audio files
        )
        _duration = round(time.time() - _t0, 2)

        # Record transcription AI usage
        try:
            from app.services.intelligence_service import record_usage, _estimate_cost
            meta = getattr(response, "usage_metadata", None)
            _in_tok = getattr(meta, "prompt_token_count", 0) or 0
            _out_tok = getattr(meta, "candidates_token_count", 0) or 0
            _cost = _estimate_cost("gemini-2.5-flash", _in_tok, _out_tok)
            record_usage({
                "operation": "transcription",
                "provider": "google",
                "model": "gemini-2.5-flash",
                "input_tokens": _in_tok,
                "output_tokens": _out_tok,
                "total_tokens": _in_tok + _out_tok,
                "cost_usd": round(_cost, 6),
                "duration_s": _duration,
            })
        except Exception as _usage_exc:
            logger.debug("Failed to record transcription usage: %s", _usage_exc)

        # Warn if the model stopped due to token limit (truncated JSON), and
        # record finish_reason + safety ratings onto the BotSession so the
        # debug endpoint can surface them — a "no content" failure caused by
        # SAFETY blocking looks identical to one caused by a silent recording
        # unless we capture this.
        try:
            finish_reason = response.candidates[0].finish_reason.name
            if finish_reason not in ("STOP", "1"):
                logger.warning("Gemini stopped with finish_reason=%s — output may be truncated", finish_reason)
            await _set_diag(bot_id, last_gemini_finish_reason=finish_reason)
        except Exception:
            pass
        try:
            safety_entries: list[dict] = []
            pf = getattr(response, "prompt_feedback", None)
            for r in (getattr(pf, "safety_ratings", None) or []):
                safety_entries.append({
                    "category": getattr(r.category, "name", str(r.category)),
                    "probability": getattr(r.probability, "name", str(r.probability)),
                })
            for cand_r in (getattr(response.candidates[0], "safety_ratings", None) or []):
                safety_entries.append({
                    "source": "candidate",
                    "category": getattr(cand_r.category, "name", str(cand_r.category)),
                    "probability": getattr(cand_r.probability, "name", str(cand_r.probability)),
                })
            if safety_entries:
                await _set_diag(bot_id, last_gemini_safety_blocks=safety_entries)
        except Exception:
            pass

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
        # 4. Truncated response recovery — extract all complete {...} objects
        if transcript is None:
            objects = re.findall(r'\{[^{}]*"speaker"[^{}]*"text"[^{}]*"timestamp"[^{}]*\}', raw)
            if objects:
                recovered = []
                for obj in objects:
                    try:
                        recovered.append(json.loads(obj))
                    except json.JSONDecodeError:
                        pass
                if recovered:
                    logger.warning("Recovered %d entries from truncated Gemini response", len(recovered))
                    transcript = recovered
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

        # Post-process: normalise speaker names against known_participants and
        # clean up inconsistent labels from the model (e.g. "speaker 1" vs "Participant 1").
        if validated:
            validated = _normalise_speakers(validated, known_participants)

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
