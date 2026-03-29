"""Consent announcement and opt-out tracking service.

When `consent_enabled=True` on a bot:
1. The bot sends a consent announcement message in the meeting chat as soon as it joins.
2. Transcript entries are monitored for the opt-out phrase.
3. Participants who opt out are recorded in `bot.opted_out_participants`.
4. (Optional) Post-processing: redact transcript entries from opted-out participants.

Configuration (see config.py):
  CONSENT_ANNOUNCEMENT_ENABLED  — set True to enable consent by default for all bots
  CONSENT_MESSAGE                — the announcement text (overridable per-bot)
  CONSENT_OPT_OUT_PHRASE         — phrase that triggers opt-out (default: "opt out")
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)


def _get_settings():
    from app.config import settings
    return settings


def build_announcement_message(custom_message: Optional[str] = None) -> str:
    """Return the consent announcement text to send in meeting chat."""
    s = _get_settings()
    return (
        custom_message
        or s.CONSENT_MESSAGE
        or "This meeting is being recorded. Say 'opt out' if you do not wish to be recorded."
    )


def check_transcript_for_optout(
    transcript_entry: dict,
    opt_out_phrase: Optional[str] = None,
) -> bool:
    """Return True if this transcript entry contains an opt-out request."""
    s = _get_settings()
    phrase = (opt_out_phrase or s.CONSENT_OPT_OUT_PHRASE).lower().strip()
    text = (transcript_entry.get("text") or "").lower()
    return phrase in text


def filter_opted_out_participants(
    transcript: list[dict],
    opted_out: list[str],
) -> list[dict]:
    """Remove transcript entries from opted-out participants.

    Called during post-processing to redact content from participants who
    exercised their opt-out right.
    """
    if not opted_out:
        return transcript
    opted_out_lower = {name.lower() for name in opted_out}
    filtered = []
    for entry in transcript:
        speaker = (entry.get("speaker") or "").lower()
        if speaker in opted_out_lower:
            # Replace content with opt-out notice rather than deleting entirely
            # to preserve transcript structure and timestamps.
            filtered.append({
                **entry,
                "text": "[Participant opted out of recording]",
                "opted_out": True,
            })
        else:
            filtered.append(entry)
    return filtered


def scan_transcript_for_optouts(
    transcript: list[dict],
    opt_out_phrase: Optional[str] = None,
) -> list[str]:
    """Scan a completed transcript and return a list of participant names who opted out."""
    opted_out: list[str] = []
    for entry in transcript:
        if check_transcript_for_optout(entry, opt_out_phrase):
            speaker = entry.get("speaker")
            if speaker and speaker not in opted_out:
                opted_out.append(speaker)
                logger.info("Participant '%s' opted out of recording", speaker)
    return opted_out


async def process_consent(bot_id: str, transcript: list[dict]) -> list[dict]:
    """Scan transcript for opt-outs, update bot, and return filtered transcript.

    This is called after transcription completes when consent_enabled=True.
    """
    if not transcript:
        return transcript or []

    from app.store import store

    bot = await store.get_bot(bot_id)
    if bot is None or not bot.consent_enabled:
        return transcript

    s = _get_settings()
    opt_out_phrase = s.CONSENT_OPT_OUT_PHRASE

    # Find new opt-outs in this transcript
    new_optouts = scan_transcript_for_optouts(transcript, opt_out_phrase)

    # Merge with any previously recorded opt-outs
    all_optouts = list(bot.opted_out_participants)
    for name in new_optouts:
        if name not in all_optouts:
            all_optouts.append(name)

    if all_optouts:
        await store.update_bot(bot_id, opted_out_participants=all_optouts)
        transcript = filter_opted_out_participants(transcript, all_optouts)
        logger.info(
            "Bot %s: %d participant(s) opted out — transcript filtered",
            bot_id,
            len(all_optouts),
        )

    return transcript
