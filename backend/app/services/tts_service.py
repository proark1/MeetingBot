"""Text-to-speech service.

Supports two providers:

  edge   — Microsoft Edge TTS (edge-tts package).
           Fast (~300 ms), high quality, no API key, free.

  gemini — Google Gemini TTS (gemini-2.5-flash-preview-tts via REST API).
           More natural voice, uses the existing GEMINI_API_KEY.
           Called directly through the REST endpoint so no SDK upgrade is
           needed on top of google-generativeai 0.8.x.

The public API is:
    synthesize(text, provider="edge", api_key=None, voice=None) → path | None
    play_audio(path, sink)
"""

import asyncio
import base64
import io
import logging
import os
import tempfile
import wave

import httpx

logger = logging.getLogger(__name__)

# ── Provider defaults ─────────────────────────────────────────────────────────

EDGE_DEFAULT_VOICE   = "en-US-AriaNeural"
GEMINI_DEFAULT_VOICE = "Aoede"              # natural-sounding en-US female
GEMINI_TTS_MODEL     = "gemini-2.5-flash-preview-tts"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _pcm_to_wav(pcm_data: bytes, sample_rate: int = 24000, channels: int = 1) -> bytes:
    """Wrap raw 16-bit PCM bytes in a RIFF WAV container."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)          # 16-bit = 2 bytes per sample
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_data)
    return buf.getvalue()


# ── Edge TTS ──────────────────────────────────────────────────────────────────

async def _synthesize_edge(text: str, voice: str) -> str | None:
    """edge-tts → temp MP3 file.  Returns path or None."""
    try:
        import edge_tts  # type: ignore

        fd, path = tempfile.mkstemp(suffix=".mp3", prefix="bot_tts_edge_")
        os.close(fd)

        communicate = edge_tts.Communicate(text, voice)
        await communicate.save(path)

        if os.path.getsize(path) < 100:
            logger.warning("Edge TTS output too small — discarding")
            os.unlink(path)
            return None

        logger.debug("Edge TTS: %d chars → %d bytes", len(text), os.path.getsize(path))
        return path

    except ImportError:
        logger.error("edge-tts not installed — run: pip install 'edge-tts>=6.1.9'")
        return None
    except Exception as exc:
        logger.error("Edge TTS synthesis failed: %s", exc)
        return None


# ── Gemini TTS ────────────────────────────────────────────────────────────────

async def _synthesize_gemini(text: str, api_key: str, voice: str) -> str | None:
    """Gemini TTS REST API → temp WAV file.  Returns path or None.

    Uses the generateContent endpoint with responseModalities=["AUDIO"].
    The response contains raw PCM data which we wrap in a WAV header.
    """
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_TTS_MODEL}:generateContent?key={api_key}"
    )
    body = {
        # NOTE: systemInstruction is NOT supported by TTS models — omit it
        # or the API returns HTTP 500.
        "contents": [{"role": "user", "parts": [{"text": text}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {
                "voiceConfig": {
                    "prebuiltVoiceConfig": {"voiceName": voice}
                }
            },
        },
    }

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.post(url, json=body)
            r.raise_for_status()
            data = r.json()

        if not data.get("candidates"):
            logger.error("Gemini TTS: empty candidates in response")
            return None
        try:
            part      = data["candidates"][0]["content"]["parts"][0]
            mime_type = part["inlineData"]["mimeType"]      # e.g. "audio/pcm;rate=24000"
            raw_b64   = part["inlineData"]["data"]
        except (KeyError, IndexError) as exc:
            logger.error("Gemini TTS: unexpected response shape — %s", exc)
            return None
        pcm_bytes = base64.b64decode(raw_b64)

        # Parse sample rate from mime type
        sample_rate = 24000
        if "rate=" in mime_type:
            try:
                sample_rate = int(mime_type.split("rate=")[1].split(";")[0])
            except (ValueError, IndexError):
                pass

        wav_bytes = _pcm_to_wav(pcm_bytes, sample_rate)

        fd, path = tempfile.mkstemp(suffix=".wav", prefix="bot_tts_gemini_")
        os.close(fd)
        with open(path, "wb") as f:
            f.write(wav_bytes)

        logger.debug(
            "Gemini TTS: %d chars → %d bytes (rate=%d Hz)",
            len(text), len(wav_bytes), sample_rate,
        )
        return path

    except httpx.HTTPStatusError as exc:
        logger.error("Gemini TTS HTTP %d: %s", exc.response.status_code, exc.response.text[:300])
        return None
    except Exception as exc:
        logger.error("Gemini TTS synthesis failed: %s", exc)
        return None


# ── Public API ────────────────────────────────────────────────────────────────

async def synthesize(
    text: str,
    provider: str = "edge",
    api_key: str | None = None,
    voice: str | None = None,
) -> str | None:
    """Convert *text* to an audio file using the requested provider.

    Args:
        text:     The text to speak.
        provider: ``"edge"`` (default) or ``"gemini"``.
        api_key:  Gemini API key — required when provider is ``"gemini"``.
        voice:    Override the default voice for the selected provider.

    Returns:
        Absolute path to a temp audio file (MP3 for edge, WAV for Gemini),
        or None on failure.  The caller must delete the file after playback.
    """
    if provider == "gemini":
        if not api_key:
            logger.warning("Gemini TTS requested but no API key provided — falling back to edge-tts")
        else:
            result = await _synthesize_gemini(text, api_key, voice or GEMINI_DEFAULT_VOICE)
            if result:
                return result
            logger.warning("Gemini TTS failed — falling back to edge-tts")

    return await _synthesize_edge(text, voice or EDGE_DEFAULT_VOICE)


async def play_audio(path: str, sink: str) -> None:
    """Play an audio file (MP3 or WAV) into a PulseAudio sink.

    Blocks until playback is complete, then deletes the temp file.
    Uses ffmpeg which is already present in the recording pipeline.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", path,
            "-f", "pulse", sink,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(proc.wait(), timeout=15.0)
        except asyncio.TimeoutError:
            proc.kill()
            logger.warning("TTS playback timed out — killed ffmpeg")
        logger.debug("TTS playback complete: %s", path)
    except Exception as exc:
        logger.warning("TTS playback failed: %s", exc)
    finally:
        try:
            os.unlink(path)
        except Exception:
            pass
