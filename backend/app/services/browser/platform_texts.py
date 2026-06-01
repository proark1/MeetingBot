"""Per-platform body-text signals for admission / end / alone detection.

Pure data, extracted from ``browser_bot.py``. These are the documented
extension point when adding a new platform (see CLAUDE.md): add an entry per
dict here plus the ``_join_<platform>`` logic in ``browser_bot.py``.

Each maps a platform key to a list of lowercase substrings matched against the
page's visible body text:
- IN_CALL_TEXTS  — bot is admitted / in the call
- WAITING_TEXTS  — bot is in a waiting room / lobby
- END_TEXTS      — the meeting/call has ended or the bot was removed
- ALONE_TEXTS    — the bot is the only participant
"""

IN_CALL_TEXTS = {
    "google_meet": ["leave call", "you're in the call", "turn on camera", "everyone in this call"],
    "zoom": ["stop video", "audio connected", "end meeting"],
    "microsoft_teams": ["you're in the meeting", "leave", "raise your hand"],
    "onepizza": [],  # rely on DOM checks only (no reliable in-call text)
}

WAITING_TEXTS = {
    "google_meet": ["waiting to be admitted", "waiting room", "someone will let you in"],
    "zoom": ["waiting for the host", "waiting room"],
    "microsoft_teams": ["waiting for others", "someone in the meeting should let you in", "lobby"],
    "onepizza": [],  # waiting room detected via #waitingRoomOverlay DOM check
}

END_TEXTS = {
    "google_meet": ["you left the meeting", "call has ended", "meeting ended", "you've been removed"],
    "zoom": ["meeting has been ended", "meeting is ended", "this meeting has ended"],
    "microsoft_teams": ["the meeting has ended", "call ended", "you left"],
    "onepizza": ["meeting ended", "meeting has ended", "you left", "the meeting has ended"],
}

# Text signals that the bot is the only one in the meeting.
ALONE_TEXTS = {
    "google_meet": [
        "no one else is here",
        "you're the only one",
        "you are the only one",
        "no one else has joined",
        "add others to this call",
    ],
    "zoom": [
        "you are the only participant",
        "waiting for others to join",
    ],
    "microsoft_teams": [
        "you're the only one here",
        "you are the only one here",
        "no one else is here",
    ],
    "onepizza": [],  # detected via tile count
}
