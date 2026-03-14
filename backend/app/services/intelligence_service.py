"""Meeting intelligence — summaries, action items, etc.

Supports two AI providers, selected by environment variable:
  • Anthropic Claude  (ANTHROPIC_API_KEY)  — takes precedence
  • Google Gemini     (GEMINI_API_KEY)      — used when no Anthropic key is set

If neither key is configured the service returns stub/empty results.
"""

import json
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

# ── Cost-per-token pricing (USD) ─────────────────────────────────────────────
# Updated as of March 2026.  Keys match model IDs returned by the providers.
_PRICING: dict[str, dict[str, float]] = {
    "claude-opus-4-6":     {"input": 15.0 / 1_000_000, "output": 75.0 / 1_000_000},
    "claude-sonnet-4-6":   {"input": 3.0 / 1_000_000,  "output": 15.0 / 1_000_000},
    "claude-haiku-4-5":    {"input": 0.80 / 1_000_000, "output": 4.0 / 1_000_000},
    "gemini-2.5-flash":    {"input": 0.15 / 1_000_000, "output": 0.60 / 1_000_000},
    "gemini-2.0-flash":    {"input": 0.10 / 1_000_000, "output": 0.40 / 1_000_000},
}

def _estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Return estimated USD cost for a model call."""
    pricing = _PRICING.get(model, {"input": 0.0, "output": 0.0})
    return input_tokens * pricing["input"] + output_tokens * pricing["output"]


# Accumulated usage records for the current bot session.  The bot_service
# calls collect_usage() after each AI phase and resets via reset_usage().
_usage_records: list[dict[str, Any]] = []

def record_usage(entry: dict[str, Any]) -> None:
    """Append a usage record (called internally after each AI call)."""
    _usage_records.append(entry)

def collect_usage() -> list[dict[str, Any]]:
    """Return and clear all accumulated usage records."""
    records = list(_usage_records)
    _usage_records.clear()
    return records

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

_FOLLOWUP_EMAIL_PROMPT = """You are a professional meeting assistant. Write a concise follow-up email
summarising the meeting. Return ONLY valid JSON — no markdown fences, no prose outside the JSON.

Required JSON shape:
{
  "subject": "<concise subject line>",
  "body": "<full email body as plain text, 150-300 words>"
}

The email should:
- Thank participants and briefly summarise 2-3 key discussion points
- List action items with owners when present
- State agreed next steps
- Be warm, professional, and scannable"""

_BRIEF_PROMPT = """You are a meeting preparation assistant. Given context about an upcoming meeting,
generate a concise preparation brief. Return ONLY valid JSON — no markdown fences.

Required JSON shape:
{
  "brief": "<full preparation doc as plain text, 150-250 words>",
  "talking_points": ["<point 1>", ...],
  "questions_to_raise": ["<question 1>", ...],
  "context_summary": "<1-2 sentence summary of relevant background>"
}"""

_RECURRING_BRIEF_PROMPT = """You are a meeting intelligence assistant. Analyse these summaries from
previous instances of a recurring meeting and identify patterns, trends, and unresolved items.
Return ONLY valid JSON — no markdown fences.

Required JSON shape:
{
  "recurring_themes": ["<theme 1>", ...],
  "unresolved_items": ["<item 1>", ...],
  "trend_summary": "<2-3 sentence overview of how things are progressing>",
  "suggested_agenda": ["<agenda point 1>", ...]
}"""


# ── Provider helpers ───────────────────────────────────────────────────────────

def _use_claude() -> bool:
    from app.config import settings
    return bool(settings.ANTHROPIC_API_KEY)


def _use_gemini() -> bool:
    from app.config import settings
    return bool(settings.GEMINI_API_KEY)


def _get_gemini_model():
    from app.config import settings
    try:
        import google.generativeai as genai
    except ImportError:
        raise RuntimeError(
            "google-generativeai is not installed — run: pip install google-generativeai"
        )
    genai.configure(api_key=settings.GEMINI_API_KEY)
    return genai.GenerativeModel("gemini-2.5-flash")


def _get_anthropic_client():
    from app.config import settings
    try:
        import anthropic
    except ImportError:
        raise RuntimeError(
            "anthropic is not installed — run: pip install anthropic"
        )
    return anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        parts = text.split("```")
        content = parts[1] if len(parts) > 1 else text
        if content.startswith("json"):
            content = content[4:]
        return content.strip()
    return text


# ── Claude implementations ─────────────────────────────────────────────────────

async def _claude_complete(prompt: str, max_tokens: int = 4096, temperature: float = 1.0, operation: str = "unknown") -> str:
    """Call Claude and return the text response. Uses adaptive thinking for complex tasks."""
    client = _get_anthropic_client()
    model_id = "claude-opus-4-6"
    t0 = time.monotonic()
    stream = client.messages.stream(
        model=model_id,
        max_tokens=max_tokens,
        thinking={"type": "adaptive"},
        messages=[{"role": "user", "content": prompt}],
    )
    async with stream as s:
        message = await s.get_final_message()
    duration_s = round(time.monotonic() - t0, 2)

    input_tokens = getattr(message.usage, "input_tokens", 0)
    output_tokens = getattr(message.usage, "output_tokens", 0)
    cost = _estimate_cost(model_id, input_tokens, output_tokens)

    record_usage({
        "operation": operation,
        "provider": "anthropic",
        "model": model_id,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "cost_usd": round(cost, 6),
        "duration_s": duration_s,
    })

    for block in message.content:
        if block.type == "text":
            return block.text
    return ""


async def _claude_analyze_transcript(
    transcript: list[dict[str, Any]],
    prompt_override: str | None = None,
    vocabulary: list[str] | None = None,
) -> dict[str, Any]:
    vocab_hint = ""
    if vocabulary:
        vocab_hint = f"Known terms and names (prefer these spellings): {', '.join(vocabulary)}\n\n"

    lines = vocab_hint + "\n".join(
        f"[{e.get('timestamp', 0):.1f}s] {e.get('speaker', '?')}: {e.get('text', '')}"
        for e in transcript
    )

    prompt = prompt_override or _ANALYSIS_PROMPT
    text = await _claude_complete(
        f"{prompt}\n\nAnalyze this meeting transcript:\n\n{lines}",
        max_tokens=4096,
        operation="analysis",
    )
    return json.loads(_strip_fences(text))


async def _claude_generate_chapters(transcript: list[dict[str, Any]]) -> list[dict[str, Any]]:
    lines = "\n".join(
        f"[{e.get('timestamp', 0):.1f}s] {e.get('speaker', '?')}: {e.get('text', '')}"
        for e in transcript
    )
    text = await _claude_complete(
        f"{_CHAPTERS_PROMPT}\n\nTranscript:\n{lines}",
        max_tokens=2048,
        operation="chapters",
    )
    return json.loads(_strip_fences(text))


async def _claude_mention_response(
    caption_context: str,
    bot_name: str,
    for_voice: bool = False,
    source: str = "caption",
) -> str:
    if for_voice:
        length_rule = (
            "Keep the answer to 2–3 short sentences (aim for under 50 words total). "
            "Write in natural spoken language — no bullet points, no markdown, no lists."
        )
        max_tokens = 500
    else:
        length_rule = (
            "Give a helpful, complete answer in up to 5 sentences. "
            "No markdown formatting."
        )
        max_tokens = 1024

    if source == "chat":
        context_label = "Recent in-meeting chat messages (these are text messages, not speech):"
    else:
        context_label = "Recent live captions from the meeting (spoken words):"

    prompt = (
        f"You are '{bot_name}', an AI assistant attending a meeting as a participant.\n"
        f"Someone addressed you by name. Read the context below and respond appropriately.\n\n"
        "Instructions:\n"
        "1. If a SPECIFIC QUESTION was asked, answer it directly and completely.\n"
        "2. For questions about THIS meeting (what was discussed, who said what, decisions made, "
        "topics covered, etc.) — use the captions provided above as your source. The captions "
        "ARE the meeting history, so you have the information.\n"
        "3. For general knowledge questions (not about this meeting), answer from your training — "
        "do NOT say 'I don't know'.\n"
        "4. If no clear question was asked and you were just greeted or called by name, "
        "briefly acknowledge and offer to help.\n"
        f"5. {length_rule}\n"
        "6. Return ONLY the answer text — no name prefix, no quotes, no preamble.\n\n"
        f"{context_label}\n{caption_context}"
    )
    text = await _claude_complete(prompt, max_tokens=max_tokens, operation="mention_response")
    if text.startswith("{") or text.endswith("}"):
        text = text.strip("{}").strip()
    return text.strip('"').strip("'")


async def _claude_ask_about_transcript(
    transcript: list[dict[str, Any]], question: str
) -> str:
    lines = "\n".join(
        f"[{e.get('timestamp', 0):.1f}s] {e.get('speaker', '?')}: {e.get('text', '')}"
        for e in transcript
    )
    return await _claude_complete(
        f"You are a meeting assistant. Answer the following question based ONLY on the meeting transcript below. "
        f"Be concise and specific. If the answer is not in the transcript, say so.\n\n"
        f"Question: {question}\n\nTranscript:\n{lines}",
        max_tokens=1024,
        operation="ask_question",
    )


async def _claude_demo_transcript(meeting_url: str) -> list[dict[str, Any]]:
    text = await _claude_complete(
        f"{_DEMO_TRANSCRIPT_PROMPT}\n\n"
        f"Generate a realistic meeting transcript for a video call at: {meeting_url}\n"
        "Topics should feel natural for this kind of meeting. Include 3–4 distinct speakers.",
        max_tokens=8192,
        operation="demo_transcript",
    )
    return json.loads(_strip_fences(text))


# ── Gemini implementations (unchanged) ────────────────────────────────────────

def _record_gemini_usage(response, operation: str, model_id: str = "gemini-2.5-flash", duration_s: float = 0.0) -> None:
    """Extract usage from a Gemini response and record it."""
    meta = getattr(response, "usage_metadata", None)
    input_tokens = getattr(meta, "prompt_token_count", 0) or 0
    output_tokens = getattr(meta, "candidates_token_count", 0) or 0
    cost = _estimate_cost(model_id, input_tokens, output_tokens)
    record_usage({
        "operation": operation,
        "provider": "google",
        "model": model_id,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "cost_usd": round(cost, 6),
        "duration_s": duration_s,
    })


async def _gemini_analyze_transcript(
    transcript: list[dict[str, Any]],
    prompt_override: str | None = None,
    vocabulary: list[str] | None = None,
) -> dict[str, Any]:
    vocab_hint = ""
    if vocabulary:
        vocab_hint = f"Known terms and names (prefer these spellings): {', '.join(vocabulary)}\n\n"

    lines = vocab_hint + "\n".join(
        f"[{e.get('timestamp', 0):.1f}s] {e.get('speaker', '?')}: {e.get('text', '')}"
        for e in transcript
    )

    prompt = prompt_override or _ANALYSIS_PROMPT
    model = _get_gemini_model()
    t0 = time.monotonic()
    response = await model.generate_content_async(
        f"{prompt}\n\nAnalyze this meeting transcript:\n\n{lines}",
        generation_config={"temperature": 0.2, "max_output_tokens": 4096},
    )
    _record_gemini_usage(response, "analysis", duration_s=round(time.monotonic() - t0, 2))
    return json.loads(_strip_fences(response.text))


async def _gemini_generate_chapters(transcript: list[dict[str, Any]]) -> list[dict[str, Any]]:
    lines = "\n".join(
        f"[{e.get('timestamp', 0):.1f}s] {e.get('speaker', '?')}: {e.get('text', '')}"
        for e in transcript
    )
    model = _get_gemini_model()
    t0 = time.monotonic()
    response = await model.generate_content_async(
        f"{_CHAPTERS_PROMPT}\n\nTranscript:\n{lines}",
        generation_config={"temperature": 0.2, "max_output_tokens": 2048},
    )
    _record_gemini_usage(response, "chapters", duration_s=round(time.monotonic() - t0, 2))
    return json.loads(_strip_fences(response.text))


async def _gemini_mention_response(
    caption_context: str,
    bot_name: str,
    for_voice: bool = False,
    source: str = "caption",
) -> str:
    if for_voice:
        length_rule = (
            "Keep the answer to 2–3 short sentences (aim for under 50 words total). "
            "Write in natural spoken language — no bullet points, no markdown, no lists."
        )
        max_tokens = 500
    else:
        length_rule = (
            "Give a helpful, complete answer in up to 5 sentences. "
            "No markdown formatting."
        )
        max_tokens = 4096

    if source == "chat":
        context_label = "Recent in-meeting chat messages (these are text messages, not speech):"
    else:
        context_label = "Recent live captions from the meeting (spoken words):"

    prompt = (
        f"You are '{bot_name}', an AI assistant attending a meeting as a participant.\n"
        f"Someone addressed you by name. Read the context below and respond appropriately.\n\n"
        "Instructions:\n"
        "1. If a SPECIFIC QUESTION was asked, answer it directly and completely.\n"
        "2. For questions about THIS meeting (what was discussed, who said what, decisions made, "
        "topics covered, etc.) — use the captions provided above as your source. The captions "
        "ARE the meeting history, so you have the information.\n"
        "3. For general knowledge questions (not about this meeting), answer from your training — "
        "do NOT say 'I don't know'.\n"
        "4. If no clear question was asked and you were just greeted or called by name, "
        "briefly acknowledge and offer to help.\n"
        f"5. {length_rule}\n"
        "6. Return ONLY the answer text — no name prefix, no quotes, no preamble.\n\n"
        f"{context_label}\n{caption_context}"
    )
    model = _get_gemini_model()
    t0 = time.monotonic()
    response = await model.generate_content_async(
        prompt,
        generation_config={"temperature": 0.4, "max_output_tokens": max_tokens},
    )
    _record_gemini_usage(response, "mention_response", duration_s=round(time.monotonic() - t0, 2))
    text = response.text.strip()
    if text.startswith("{") or text.endswith("}"):
        text = text.strip("{}").strip()
    return text.strip('"').strip("'")


async def _gemini_ask_about_transcript(
    transcript: list[dict[str, Any]], question: str
) -> str:
    lines = "\n".join(
        f"[{e.get('timestamp', 0):.1f}s] {e.get('speaker', '?')}: {e.get('text', '')}"
        for e in transcript
    )
    model = _get_gemini_model()
    t0 = time.monotonic()
    response = await model.generate_content_async(
        f"You are a meeting assistant. Answer the following question based ONLY on the meeting transcript below. "
        f"Be concise and specific. If the answer is not in the transcript, say so.\n\n"
        f"Question: {question}\n\nTranscript:\n{lines}",
        generation_config={"temperature": 0.3, "max_output_tokens": 1024},
    )
    _record_gemini_usage(response, "ask_question", duration_s=round(time.monotonic() - t0, 2))
    return response.text.strip()


async def _gemini_demo_transcript(meeting_url: str) -> list[dict[str, Any]]:
    model = _get_gemini_model()
    t0 = time.monotonic()
    response = await model.generate_content_async(
        f"{_DEMO_TRANSCRIPT_PROMPT}\n\n"
        f"Generate a realistic meeting transcript for a video call at: {meeting_url}\n"
        "Topics should feel natural for this kind of meeting. Include 3–4 distinct speakers.",
        generation_config={"temperature": 0.8, "max_output_tokens": 8192},
    )
    _record_gemini_usage(response, "demo_transcript", duration_s=round(time.monotonic() - t0, 2))
    return json.loads(_strip_fences(response.text))


# ── Follow-up email + brief (Claude) ─────────────────────────────────────────

async def _claude_followup_email(
    transcript: list[dict[str, Any]],
    analysis: dict[str, Any],
    participants: list[str],
) -> dict[str, Any]:
    lines = "\n".join(
        f"[{e.get('timestamp', 0):.1f}s] {e.get('speaker', '?')}: {e.get('text', '')}"
        for e in transcript[:80]  # cap to avoid huge prompts
    )
    analysis_summary = (
        f"Summary: {analysis.get('summary', '')}\n"
        f"Action items: {analysis.get('action_items', [])}\n"
        f"Decisions: {analysis.get('decisions', [])}\n"
        f"Next steps: {analysis.get('next_steps', [])}"
    )
    text = await _claude_complete(
        f"{_FOLLOWUP_EMAIL_PROMPT}\n\n"
        f"Participants: {', '.join(participants)}\n\n"
        f"Meeting analysis:\n{analysis_summary}\n\n"
        f"Transcript excerpt:\n{lines}",
        max_tokens=1024,
        operation="followup_email",
    )
    return json.loads(_strip_fences(text))


async def _claude_meeting_brief(
    agenda: str,
    participants: list[str],
    previous_summaries: list[str],
) -> dict[str, Any]:
    context = ""
    if previous_summaries:
        context = "Previous meeting summaries (most recent first):\n" + "\n\n".join(
            f"- {s}" for s in previous_summaries[:3]
        )
    text = await _claude_complete(
        f"{_BRIEF_PROMPT}\n\n"
        f"Participants: {', '.join(participants)}\n"
        f"Agenda: {agenda or 'No agenda provided'}\n\n"
        f"{context}",
        max_tokens=1024,
        operation="meeting_brief",
    )
    return json.loads(_strip_fences(text))


async def _claude_recurring_intelligence(
    previous_summaries: list[str],
    participants: list[str],
) -> dict[str, Any]:
    summaries_text = "\n\n".join(
        f"Meeting {i + 1}: {s}" for i, s in enumerate(previous_summaries[:5])
    )
    text = await _claude_complete(
        f"{_RECURRING_BRIEF_PROMPT}\n\n"
        f"Participants (typical): {', '.join(participants)}\n\n"
        f"Previous summaries:\n{summaries_text}",
        max_tokens=1024,
        operation="recurring_intelligence",
    )
    return json.loads(_strip_fences(text))


# ── Follow-up email + brief (Gemini) ─────────────────────────────────────────

async def _gemini_followup_email(
    transcript: list[dict[str, Any]],
    analysis: dict[str, Any],
    participants: list[str],
) -> dict[str, Any]:
    lines = "\n".join(
        f"[{e.get('timestamp', 0):.1f}s] {e.get('speaker', '?')}: {e.get('text', '')}"
        for e in transcript[:80]
    )
    analysis_summary = (
        f"Summary: {analysis.get('summary', '')}\n"
        f"Action items: {analysis.get('action_items', [])}\n"
        f"Decisions: {analysis.get('decisions', [])}\n"
        f"Next steps: {analysis.get('next_steps', [])}"
    )
    model = _get_gemini_model()
    t0 = time.monotonic()
    response = await model.generate_content_async(
        f"{_FOLLOWUP_EMAIL_PROMPT}\n\n"
        f"Participants: {', '.join(participants)}\n\n"
        f"Meeting analysis:\n{analysis_summary}\n\n"
        f"Transcript excerpt:\n{lines}",
        generation_config={"temperature": 0.3, "max_output_tokens": 1024},
    )
    _record_gemini_usage(response, "followup_email", duration_s=round(time.monotonic() - t0, 2))
    return json.loads(_strip_fences(response.text))


async def _gemini_meeting_brief(
    agenda: str,
    participants: list[str],
    previous_summaries: list[str],
) -> dict[str, Any]:
    context = ""
    if previous_summaries:
        context = "Previous meeting summaries:\n" + "\n\n".join(
            f"- {s}" for s in previous_summaries[:3]
        )
    model = _get_gemini_model()
    t0 = time.monotonic()
    response = await model.generate_content_async(
        f"{_BRIEF_PROMPT}\n\n"
        f"Participants: {', '.join(participants)}\n"
        f"Agenda: {agenda or 'No agenda provided'}\n\n"
        f"{context}",
        generation_config={"temperature": 0.3, "max_output_tokens": 1024},
    )
    _record_gemini_usage(response, "meeting_brief", duration_s=round(time.monotonic() - t0, 2))
    return json.loads(_strip_fences(response.text))


async def _gemini_recurring_intelligence(
    previous_summaries: list[str],
    participants: list[str],
) -> dict[str, Any]:
    summaries_text = "\n\n".join(
        f"Meeting {i + 1}: {s}" for i, s in enumerate(previous_summaries[:5])
    )
    model = _get_gemini_model()
    t0 = time.monotonic()
    response = await model.generate_content_async(
        f"{_RECURRING_BRIEF_PROMPT}\n\n"
        f"Participants (typical): {', '.join(participants)}\n\n"
        f"Previous summaries:\n{summaries_text}",
        generation_config={"temperature": 0.3, "max_output_tokens": 1024},
    )
    _record_gemini_usage(response, "recurring_intelligence", duration_s=round(time.monotonic() - t0, 2))
    return json.loads(_strip_fences(response.text))


# ── Public API ─────────────────────────────────────────────────────────────────

async def analyze_transcript(
    transcript: list[dict[str, Any]],
    prompt_override: str | None = None,
    vocabulary: list[str] | None = None,
) -> dict[str, Any]:
    """Analyze the transcript and return structured meeting intelligence.

    Uses Claude (Anthropic) when ANTHROPIC_API_KEY is set, otherwise Gemini.

    Args:
        prompt_override: Replaces the default analysis prompt (e.g. from a meeting template).
        vocabulary: Domain-specific terms to prepend as spelling hints.
    """
    if not transcript:
        return _empty_analysis()

    if _use_claude():
        try:
            return await _claude_analyze_transcript(transcript, prompt_override, vocabulary)
        except json.JSONDecodeError as exc:
            logger.error("Claude returned invalid JSON for analysis: %s", exc)
        except Exception as exc:
            logger.error("Claude analysis error: %s", exc)
        return _stub_analysis(transcript)

    if _use_gemini():
        try:
            return await _gemini_analyze_transcript(transcript, prompt_override, vocabulary)
        except json.JSONDecodeError as exc:
            logger.error("Gemini returned invalid JSON for analysis: %s", exc)
        except ValueError as exc:
            logger.warning("Gemini analysis blocked by safety filter: %s", exc)
        except Exception as exc:
            logger.error("Gemini analysis error: %s", exc)
        return _stub_analysis(transcript)

    logger.warning("No AI API key configured — returning stub analysis")
    return _stub_analysis(transcript)


async def generate_chapters(transcript: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Segment the transcript into named chapters with timestamps."""
    if not transcript:
        return []

    if _use_claude():
        try:
            return await _claude_generate_chapters(transcript)
        except json.JSONDecodeError as exc:
            logger.error("Claude chapters: invalid JSON — %s", exc)
        except Exception as exc:
            logger.warning("Claude chapter generation failed: %s", exc)
        return []

    if _use_gemini():
        try:
            return await _gemini_generate_chapters(transcript)
        except json.JSONDecodeError as exc:
            logger.error("Gemini chapters: invalid JSON — %s", exc)
        except ValueError as exc:
            logger.warning("Gemini chapters blocked by safety filter: %s", exc)
        except Exception as exc:
            logger.warning("Chapter generation failed: %s", exc)
        return []

    return []


async def generate_mention_response(
    caption_context: str,
    bot_name: str,
    for_voice: bool = False,
    source: str = "caption",
) -> str:
    """Generate an in-meeting reply when the bot's name is called.

    Handles three cases:
    - Meeting-specific question  → answered using the caption context
    - General knowledge question → answered from the model's knowledge
    - Simple greeting / name call → brief acknowledgement + offer to help

    Args:
        caption_context: Recent live-caption or chat text (~1 500 chars).
        bot_name:        Bot's display name.
        for_voice:       When True the reply is constrained to 2–3 short spoken sentences.
        source:          "caption" or "chat" — affects how the context is labelled.

    Returns:
        Answer text string, or empty string if no AI provider is available.
    """
    if _use_claude():
        try:
            return await _claude_mention_response(caption_context, bot_name, for_voice, source)
        except Exception as exc:
            logger.warning("Claude mention response error: %s", exc)
        return ""

    if _use_gemini():
        try:
            return await _gemini_mention_response(caption_context, bot_name, for_voice, source)
        except ValueError as exc:
            logger.warning("Gemini mention response blocked by safety filter: %s", exc)
        except Exception as exc:
            logger.warning("generate_mention_response error: %s", exc)
        return ""

    return ""


async def ask_about_transcript(transcript: list[dict[str, Any]], question: str) -> str:
    """Answer a free-form question about the meeting transcript."""
    if not transcript:
        return "No transcript available to answer questions about."

    if _use_claude():
        try:
            return await _claude_ask_about_transcript(transcript, question)
        except Exception as exc:
            logger.error("Claude ask_about_transcript error: %s", exc)
            return f"Error generating answer: {exc}"

    if _use_gemini():
        try:
            return await _gemini_ask_about_transcript(transcript, question)
        except ValueError as exc:
            logger.warning("Gemini answer blocked by safety filter: %s", exc)
            return "The answer could not be generated — the content was flagged by the safety filter."
        except Exception as exc:
            logger.error("ask_about_transcript error: %s", exc)
            return f"Error generating answer: {exc}"

    return "No AI API key configured."


async def generate_followup_email(
    transcript: list[dict[str, Any]],
    analysis: dict[str, Any],
    participants: list[str],
) -> dict[str, Any]:
    """Generate a draft follow-up email for the meeting.

    Returns {"subject": "...", "body": "..."}.
    """
    if not transcript and not analysis:
        return {"subject": "Meeting Follow-up", "body": ""}

    if _use_claude():
        try:
            return await _claude_followup_email(transcript, analysis, participants)
        except Exception as exc:
            logger.error("Claude follow-up email error: %s", exc)

    if _use_gemini():
        try:
            return await _gemini_followup_email(transcript, analysis, participants)
        except Exception as exc:
            logger.error("Gemini follow-up email error: %s", exc)

    return {"subject": "Meeting Follow-up", "body": "AI provider not configured."}


async def generate_meeting_brief(
    agenda: str,
    participants: list[str],
    previous_summaries: list[str],
) -> dict[str, Any]:
    """Generate a pre-meeting preparation brief.

    Returns {"brief": "...", "talking_points": [...], "questions_to_raise": [...], "context_summary": "..."}.
    """
    if _use_claude():
        try:
            return await _claude_meeting_brief(agenda, participants, previous_summaries)
        except Exception as exc:
            logger.error("Claude meeting brief error: %s", exc)

    if _use_gemini():
        try:
            return await _gemini_meeting_brief(agenda, participants, previous_summaries)
        except Exception as exc:
            logger.error("Gemini meeting brief error: %s", exc)

    return {
        "brief": "AI provider not configured.",
        "talking_points": [],
        "questions_to_raise": [],
        "context_summary": "",
    }


async def generate_recurring_intelligence(
    previous_summaries: list[str],
    participants: list[str],
) -> dict[str, Any]:
    """Analyse a series of recurring meeting summaries to surface themes and trends.

    Returns {"recurring_themes": [...], "unresolved_items": [...], "trend_summary": "...", "suggested_agenda": [...]}.
    """
    if not previous_summaries:
        return {
            "recurring_themes": [],
            "unresolved_items": [],
            "trend_summary": "No previous meetings to analyse.",
            "suggested_agenda": [],
        }

    if _use_claude():
        try:
            return await _claude_recurring_intelligence(previous_summaries, participants)
        except Exception as exc:
            logger.error("Claude recurring intelligence error: %s", exc)

    if _use_gemini():
        try:
            return await _gemini_recurring_intelligence(previous_summaries, participants)
        except Exception as exc:
            logger.error("Gemini recurring intelligence error: %s", exc)

    return {
        "recurring_themes": [],
        "unresolved_items": [],
        "trend_summary": "AI provider not configured.",
        "suggested_agenda": [],
    }


async def generate_demo_transcript(meeting_url: str) -> list[dict[str, Any]]:
    """Generate a realistic demo transcript (fallback when real audio unavailable)."""
    if _use_claude():
        try:
            return await _claude_demo_transcript(meeting_url)
        except Exception as exc:
            logger.error("Claude demo transcript error: %s", exc)
        return _hardcoded_demo_transcript()

    if _use_gemini():
        try:
            return await _gemini_demo_transcript(meeting_url)
        except ValueError as exc:
            logger.warning("Gemini demo transcript blocked by safety filter: %s", exc)
        except Exception as exc:
            logger.error("Failed to generate demo transcript: %s", exc)
        return _hardcoded_demo_transcript()

    return _hardcoded_demo_transcript()


# ── Fallbacks ─────────────────────────────────────────────────────────────────

def _empty_analysis() -> dict[str, Any]:
    return {
        "summary": "No transcript available.",
        "key_points": [], "action_items": [], "decisions": [],
        "next_steps": [], "sentiment": "neutral", "topics": [],
    }


def _stub_analysis(transcript: list[dict]) -> dict[str, Any]:
    speakers = list({e.get("speaker", "Unknown") for e in transcript})
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
