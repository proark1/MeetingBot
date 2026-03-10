"""Text-to-speech using Microsoft Edge TTS (edge-tts).

Fast (~300 ms), high quality, no API key required.
Output is an MP3 file ready to be played through PulseAudio.
"""

import asyncio
import logging
import os
import tempfile

logger = logging.getLogger(__name__)

# Default voice — natural-sounding English US female
DEFAULT_VOICE = "en-US-AriaNeural"


async def synthesize(text: str, voice: str = DEFAULT_VOICE) -> str | None:
    """Convert *text* to an MP3 file via edge-tts.

    Returns the path to a temporary MP3 file on success, or None on failure.
    The caller is responsible for deleting the file after playback.
    """
    try:
        import edge_tts  # type: ignore

        # Write to a temp file (edge-tts streams directly to disk)
        fd, path = tempfile.mkstemp(suffix=".mp3", prefix="bot_tts_")
        os.close(fd)

        communicate = edge_tts.Communicate(text, voice)
        await communicate.save(path)

        if os.path.getsize(path) < 100:
            logger.warning("TTS output is suspiciously small (%d bytes)", os.path.getsize(path))
            os.unlink(path)
            return None

        logger.debug("TTS synthesized %d chars → %s (%d bytes)", len(text), path, os.path.getsize(path))
        return path

    except ImportError:
        logger.error("edge-tts not installed — run: pip install edge-tts")
        return None
    except Exception as exc:
        logger.error("TTS synthesis failed: %s", exc)
        return None


async def play_audio(path: str, sink: str) -> None:
    """Play an audio file through a PulseAudio sink (non-blocking, fire-and-forget).

    Uses ffmpeg which is already present in the environment for recording.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", path,
            "-f", "pulse", sink,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        logger.debug("TTS playback complete: %s", path)
    except Exception as exc:
        logger.warning("TTS playback failed: %s", exc)
    finally:
        try:
            os.unlink(path)
        except Exception:
            pass
