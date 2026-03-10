"""Gemini-powered meeting intelligence — summaries, action items, etc."""

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

_ANALYSIS_PROMPT = """You are an expert meeting analyst. Given a meeting transcript produce a
structured JSON analysis. Be concise but thorough. Return ONLY valid JSON — no markdown fences,
no prose outside the JSON object.

Required JSON shape:
{
  "summary":      "<2–4 sentence overview>",
  "key_points":   ["<point 1>", ...],
  "action_items": [{"task": "...", "assignee": "...", "due_date": "..."}],
  "decisions":    ["<decision 1>", ...],
  "next_steps":   ["<step 1>", ...],
  "sentiment":    "positive|neutral|negative",
  "topics":       ["<topic 1>", ...]
}"""

_CHAPTERS_PROMPT = """You are a meeting analyst. Segment the following transcript into 3–8 named chapters.
Return ONLY a JSON array — no markdown, no prose outside the array.
Each entry: {"title": "Short Chapter Title", "start_time": <seconds_float>, "summary": "1–2 sentence summary."}.
Order by start_time ascending."""

_DEMO_TRANSCRIPT_PROMPT = """You generate realistic meeting transcripts.
Return ONLY a JSON array of transcript entries — no markdown, no prose outside the array.
Each entry: {"speaker": "Name", "text": "...", "timestamp": <seconds_float>}.
Generate 15–25 entries spanning 8–15 minutes.
Make it a realistic tech-team meeting with concrete discussion."""


def _get_model():
    from app.config import settings
    try:
        import google.generativeai as genai
    except ImportError:
        raise RuntimeError(
            "google-generativeai is not installed — run: pip install google-generativeai"
        )
    genai.configure(api_key=settings.GEMINI_API_KEY)
    return genai.GenerativeModel("gemini-2.5-flash")


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        parts = text.split("```")
        content = parts[1] if len(parts) > 1 else text
        if content.startswith("json"):
            content = content[4:]
        return content.strip()
    return text


async def analyze_transcript(
    transcript: list[dict[str, Any]],
    prompt_override: str | None = None,
    vocabulary: list[str] | None = None,
) -> dict[str, Any]:
    """Send transcript to Gemini and return structured meeting analysis.

    Args:
        prompt_override: If set (e.g. from a meeting template), replaces the default
            analysis prompt. Should instruct Gemini to return valid JSON.
        vocabulary: Domain-specific terms to prepend as transcription hints.
    """
    from app.config import settings

    if not settings.GEMINI_API_KEY:
        logger.warning("GEMINI_API_KEY not set — returning stub analysis")
        return _stub_analysis(transcript)

    if not transcript:
        return _empty_analysis()

    vocab_hint = ""
    if vocabulary:
        vocab_hint = f"Known terms and names (prefer these spellings): {', '.join(vocabulary)}\n\n"

    lines = vocab_hint + "\n".join(
        f"[{e.get('timestamp', 0):.1f}s] {e['speaker']}: {e['text']}"
        for e in transcript
    )

    prompt = prompt_override or _ANALYSIS_PROMPT

    try:
        model = _get_model()
        response = await model.generate_content_async(
            f"{prompt}\n\nAnalyze this meeting transcript:\n\n{lines}",
            generation_config={"temperature": 0.2, "max_output_tokens": 4096},
        )
        return json.loads(_strip_fences(response.text))

    except json.JSONDecodeError as exc:
        logger.error("Gemini returned invalid JSON for analysis: %s", exc)
        return _stub_analysis(transcript)
    except Exception as exc:
        logger.error("Gemini analysis error: %s", exc)
        return _stub_analysis(transcript)


async def generate_chapters(transcript: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Segment the transcript into named chapters with timestamps."""
    from app.config import settings

    if not settings.GEMINI_API_KEY or not transcript:
        return []

    lines = "\n".join(
        f"[{e.get('timestamp', 0):.1f}s] {e['speaker']}: {e['text']}"
        for e in transcript
    )

    try:
        model = _get_model()
        response = await model.generate_content_async(
            f"{_CHAPTERS_PROMPT}\n\nTranscript:\n{lines}",
            generation_config={"temperature": 0.2, "max_output_tokens": 2048},
        )
        return json.loads(_strip_fences(response.text))
    except Exception as exc:
        logger.warning("Chapter generation failed: %s", exc)
        return []


async def generate_mention_response(
    caption_context: str,
    bot_name: str,
    for_voice: bool = False,
) -> str:
    """Generate an in-meeting reply when the bot's name is called.

    Handles three cases:
    - Meeting-specific question  → answered using the caption context
    - General knowledge question → answered from Gemini's own knowledge
    - Simple greeting / name call → brief acknowledgement + offer to help

    Args:
        caption_context: Recent live-caption text (~1 500 chars) from the meeting.
        bot_name:        Bot's display name — prepended to the final reply.
        for_voice:       When True the reply is constrained to 2–3 short spoken
                         sentences (no markdown, no bullet points).

    Returns:
        A reply string like "Judas: The Q3 revenue target was mentioned as $2.4M."
        Empty string if Gemini is unavailable or the reply is empty.
    """
    from app.config import settings

    if not settings.GEMINI_API_KEY:
        return ""

    if for_voice:
        length_rule = (
            "Keep the answer to 2–3 short sentences (aim for under 40 words total). "
            "Write in natural spoken language — no bullet points, no markdown, no lists."
        )
        max_tokens = 120
    else:
        length_rule = (
            "Give a helpful, complete answer in up to 4 sentences. "
            "No markdown formatting."
        )
        max_tokens = 256

    prompt = (
        f"You are '{bot_name}', an AI assistant attending this meeting as a participant.\n"
        "Your name was just spoken. Read the recent conversation below and determine what was asked or said.\n\n"
        "Instructions:\n"
        "1. If a MEETING-SPECIFIC question was asked (about something discussed in this call, "
        "e.g. decisions made, action items, who said what), answer it using the context below.\n"
        "2. If a GENERAL KNOWLEDGE question was asked (e.g. 'What is X?', 'How does Y work?', "
        "anything not specific to this meeting), answer it from your own knowledge — do not "
        "claim the answer is in the transcript.\n"
        "3. If no clear question was asked and you were just greeted or addressed by name, "
        "briefly acknowledge and offer to help.\n"
        f"4. {length_rule}\n"
        "5. Return ONLY the answer text — no name prefix, no quotes, no extra explanation.\n\n"
        f"Recent meeting captions:\n{caption_context}"
    )
    try:
        model = _get_model()
        response = await model.generate_content_async(
            prompt,
            generation_config={"temperature": 0.4, "max_output_tokens": max_tokens},
        )
        text = response.text.strip().strip('"').strip("'")
        return text
    except Exception as exc:
        logger.warning("generate_mention_response error: %s", exc)
        return ""


async def ask_about_transcript(transcript: list[dict[str, Any]], question: str) -> str:
    """Answer a free-form question about the meeting transcript."""
    from app.config import settings

    if not settings.GEMINI_API_KEY:
        return "Gemini API key not configured."
    if not transcript:
        return "No transcript available to answer questions about."

    lines = "\n".join(
        f"[{e.get('timestamp', 0):.1f}s] {e['speaker']}: {e['text']}"
        for e in transcript
    )

    try:
        model = _get_model()
        response = await model.generate_content_async(
            f"You are a meeting assistant. Answer the following question based ONLY on the meeting transcript below. "
            f"Be concise and specific. If the answer is not in the transcript, say so.\n\n"
            f"Question: {question}\n\nTranscript:\n{lines}",
            generation_config={"temperature": 0.3, "max_output_tokens": 1024},
        )
        return response.text.strip()
    except Exception as exc:
        logger.error("ask_about_transcript error: %s", exc)
        return f"Error generating answer: {exc}"


async def generate_demo_transcript(meeting_url: str) -> list[dict[str, Any]]:
    """Generate a realistic demo transcript via Gemini (fallback when real audio unavailable)."""
    from app.config import settings

    if not settings.GEMINI_API_KEY:
        return _hardcoded_demo_transcript()

    try:
        model = _get_model()
        response = await model.generate_content_async(
            f"{_DEMO_TRANSCRIPT_PROMPT}\n\n"
            f"Generate a realistic meeting transcript for a video call at: {meeting_url}\n"
            "Topics should feel natural for this kind of meeting. Include 3–4 distinct speakers.",
            generation_config={"temperature": 0.8, "max_output_tokens": 8192},
        )
        return json.loads(_strip_fences(response.text))

    except Exception as exc:
        logger.error("Failed to generate demo transcript: %s", exc)
        return _hardcoded_demo_transcript()


# ── Fallbacks ─────────────────────────────────────────────────────────────────

def _empty_analysis() -> dict[str, Any]:
    return {
        "summary": "No transcript available.",
        "key_points": [], "action_items": [], "decisions": [],
        "next_steps": [], "sentiment": "neutral", "topics": [],
    }


def _stub_analysis(transcript: list[dict]) -> dict[str, Any]:
    speakers = list({e["speaker"] for e in transcript})
    return {
        "summary": (
            f"Meeting with {len(speakers)} participant(s): {', '.join(speakers)}. "
            f"{len(transcript)} transcript entries recorded."
        ),
        "key_points": ["Meeting recorded successfully"],
        "action_items": [], "decisions": [],
        "next_steps": ["Review the full transcript above"],
        "sentiment": "neutral",
        "topics": ["general discussion"],
    }


def _hardcoded_demo_transcript() -> list[dict[str, Any]]:
    return [
        {"speaker": "Alice (PM)",     "text": "Good morning everyone! Let's get started with the sprint review.", "timestamp": 2.0},
        {"speaker": "Bob (Eng)",      "text": "Morning Alice. I finished the authentication module — all tests passing.", "timestamp": 8.5},
        {"speaker": "Carol (Design)", "text": "I've updated the design system components. The new color tokens are ready.", "timestamp": 15.2},
        {"speaker": "Alice (PM)",     "text": "Excellent. Bob, can you walk us through what you built?", "timestamp": 22.1},
        {"speaker": "Bob (Eng)",      "text": "Sure. We now support OAuth 2.0 with Google and GitHub. JWT tokens, 24-hour expiry, refresh token rotation.", "timestamp": 28.0},
        {"speaker": "Dave (QA)",      "text": "I ran the security suite — no critical findings. Two minor issues I'll file tickets for.", "timestamp": 45.3},
        {"speaker": "Alice (PM)",     "text": "Great work everyone. Next sprint we need to tackle the dashboard performance issues.", "timestamp": 58.0},
        {"speaker": "Bob (Eng)",      "text": "I have some ideas — we could implement virtual scrolling and lazy-load the chart data.", "timestamp": 65.5},
        {"speaker": "Carol (Design)", "text": "I can create mockups for a skeleton loading state to improve perceived performance.", "timestamp": 74.2},
        {"speaker": "Alice (PM)",     "text": "Perfect. Bob owns virtual scrolling, Carol the skeletons. Dave, set up performance baselines.", "timestamp": 82.0},
        {"speaker": "Dave (QA)",      "text": "Will do. I'll use Lighthouse and set up a CI performance budget.", "timestamp": 90.1},
        {"speaker": "Alice (PM)",     "text": "Any blockers before we wrap up?", "timestamp": 98.0},
        {"speaker": "Bob (Eng)",      "text": "I need staging environment access to test the OAuth flows end-to-end.", "timestamp": 103.4},
        {"speaker": "Alice (PM)",     "text": "I'll get that sorted today. Anything else?", "timestamp": 110.0},
        {"speaker": "Carol (Design)", "text": "No blockers on my end.", "timestamp": 114.0},
        {"speaker": "Alice (PM)",     "text": "Great. Next sync is Thursday 10am. Thanks everyone!", "timestamp": 118.5},
    ]
