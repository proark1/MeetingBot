"""Claude-powered meeting intelligence — summaries, action items, etc."""

import json
import logging
from typing import Any

import anthropic

from app.config import settings

logger = logging.getLogger(__name__)

_client: anthropic.AsyncAnthropic | None = None


def _get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
    return _client


SYSTEM_PROMPT = """You are an expert meeting analyst. Given a meeting transcript you produce a
structured JSON analysis. Be concise but thorough. Return ONLY valid JSON — no markdown fences,
no prose outside the JSON object.

Required JSON shape:
{
  "summary": "<2–4 sentence overview>",
  "key_points": ["<point 1>", ...],
  "action_items": [{"task": "...", "assignee": "...", "due_date": "..."}],
  "decisions": ["<decision 1>", ...],
  "next_steps": ["<step 1>", ...],
  "sentiment": "positive|neutral|negative",
  "topics": ["<topic 1>", ...]
}"""


async def analyze_transcript(transcript: list[dict[str, Any]]) -> dict[str, Any]:
    """Send transcript to Claude and return structured meeting analysis."""
    if not settings.ANTHROPIC_API_KEY:
        logger.warning("ANTHROPIC_API_KEY not set — returning stub analysis")
        return _stub_analysis(transcript)

    if not transcript:
        return _empty_analysis()

    lines = "\n".join(
        f"[{entry.get('timestamp', 0):.1f}s] {entry['speaker']}: {entry['text']}"
        for entry in transcript
    )

    client = _get_client()
    try:
        async with client.messages.stream(
            model="claude-opus-4-6",
            max_tokens=4096,
            thinking={"type": "adaptive"},
            system=SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": f"Analyze this meeting transcript:\n\n{lines}",
                }
            ],
        ) as stream:
            response = await stream.get_final_message()

        # Extract text block (skip thinking blocks)
        raw = next(
            (b.text for b in response.content if b.type == "text"), "{}"
        )
        return json.loads(raw)

    except json.JSONDecodeError as exc:
        logger.error("Claude returned invalid JSON: %s", exc)
        return _stub_analysis(transcript)
    except anthropic.APIError as exc:
        logger.error("Claude API error: %s", exc)
        return _stub_analysis(transcript)


async def generate_demo_transcript(meeting_url: str) -> list[dict[str, Any]]:
    """Use Claude to generate a realistic demo transcript for a given meeting URL."""
    if not settings.ANTHROPIC_API_KEY:
        return _hardcoded_demo_transcript()

    client = _get_client()
    try:
        async with client.messages.stream(
            model="claude-opus-4-6",
            max_tokens=4096,
            system=(
                "You generate realistic meeting transcripts. "
                "Return ONLY a JSON array of transcript entries. "
                "Each entry: {\"speaker\": \"Name\", \"text\": \"...\", \"timestamp\": <seconds_float>}. "
                "Generate 15–25 entries spanning 8–15 minutes. "
                "Make it a realistic tech team meeting with concrete discussion."
            ),
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Generate a realistic meeting transcript for a video call at: {meeting_url}\n"
                        "Topics should feel natural for this kind of meeting. "
                        "Include 3–4 distinct speakers."
                    ),
                }
            ],
        ) as stream:
            response = await stream.get_final_message()

        raw = next((b.text for b in response.content if b.type == "text"), "[]")
        return json.loads(raw)

    except (json.JSONDecodeError, anthropic.APIError) as exc:
        logger.error("Failed to generate demo transcript: %s", exc)
        return _hardcoded_demo_transcript()


# ── Fallbacks ──────────────────────────────────────────────────────────────

def _empty_analysis() -> dict[str, Any]:
    return {
        "summary": "No transcript available.",
        "key_points": [],
        "action_items": [],
        "decisions": [],
        "next_steps": [],
        "sentiment": "neutral",
        "topics": [],
    }


def _stub_analysis(transcript: list[dict]) -> dict[str, Any]:
    speakers = list({e["speaker"] for e in transcript})
    return {
        "summary": (
            f"Meeting with {len(speakers)} participant(s): {', '.join(speakers)}. "
            f"{len(transcript)} transcript entries recorded."
        ),
        "key_points": ["Meeting recorded successfully"],
        "action_items": [],
        "decisions": [],
        "next_steps": ["Review the full transcript above"],
        "sentiment": "neutral",
        "topics": ["general discussion"],
    }


def _hardcoded_demo_transcript() -> list[dict[str, Any]]:
    return [
        {"speaker": "Alice (PM)", "text": "Good morning everyone! Let's get started with the sprint review.", "timestamp": 2.0},
        {"speaker": "Bob (Eng)", "text": "Morning Alice. I finished the authentication module — all tests passing.", "timestamp": 8.5},
        {"speaker": "Carol (Design)", "text": "I've updated the design system components. The new color tokens are ready.", "timestamp": 15.2},
        {"speaker": "Alice (PM)", "text": "Excellent. Bob, can you walk us through what you built?", "timestamp": 22.1},
        {"speaker": "Bob (Eng)", "text": "Sure. We now support OAuth 2.0 with Google and GitHub. JWT tokens, 24-hour expiry, refresh token rotation.", "timestamp": 28.0},
        {"speaker": "Dave (QA)", "text": "I ran the security suite — no critical findings. Two minor issues I'll file tickets for.", "timestamp": 45.3},
        {"speaker": "Alice (PM)", "text": "Great work everyone. Next sprint we need to tackle the dashboard performance issues.", "timestamp": 58.0},
        {"speaker": "Bob (Eng)", "text": "I have some ideas there — we could implement virtual scrolling and lazy-load the chart data.", "timestamp": 65.5},
        {"speaker": "Carol (Design)", "text": "I can create mockups for a skeleton loading state to improve perceived performance.", "timestamp": 74.2},
        {"speaker": "Alice (PM)", "text": "Perfect. Bob owns the virtual scrolling, Carol the skeletons. Dave, please set up performance baselines.", "timestamp": 82.0},
        {"speaker": "Dave (QA)", "text": "Will do. I'll use Lighthouse and set up a CI performance budget.", "timestamp": 90.1},
        {"speaker": "Alice (PM)", "text": "Any blockers before we wrap up?", "timestamp": 98.0},
        {"speaker": "Bob (Eng)", "text": "I need a staging environment access to test the OAuth flows end-to-end.", "timestamp": 103.4},
        {"speaker": "Alice (PM)", "text": "I'll get that sorted today. Anything else?", "timestamp": 110.0},
        {"speaker": "Carol (Design)", "text": "No blockers on my end.", "timestamp": 114.0},
        {"speaker": "Alice (PM)", "text": "Great. Next sync is Thursday 10am. Thanks everyone!", "timestamp": 118.5},
    ]
