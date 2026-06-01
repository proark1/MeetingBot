"""Unit tests for browser/text_matchers.py — pure text-signal detection.

These test the extracted state-machine helpers without any Playwright or
browser dependency.  Every platform × signal combination is covered so
regressions in platform_texts.py data are caught immediately.
"""

import pytest

from app.services.browser.text_matchers import (
    text_signals_in_call,
    text_signals_waiting,
    text_signals_ended,
    text_signals_alone,
)
from app.services.browser.platform_texts import (
    IN_CALL_TEXTS,
    WAITING_TEXTS,
    END_TEXTS,
    ALONE_TEXTS,
)

PLATFORMS = ["google_meet", "zoom", "microsoft_teams", "onepizza"]


# ── Structural sanity ─────────────────────────────────────────────────────────

def test_all_platforms_present_in_all_dicts():
    for platform in PLATFORMS:
        assert platform in IN_CALL_TEXTS, f"IN_CALL_TEXTS missing {platform}"
        assert platform in WAITING_TEXTS, f"WAITING_TEXTS missing {platform}"
        assert platform in END_TEXTS, f"END_TEXTS missing {platform}"
        assert platform in ALONE_TEXTS, f"ALONE_TEXTS missing {platform}"


def test_all_signal_values_are_lowercase():
    """Matchers lower-case page body before matching; signals must already be lower."""
    for d, name in [
        (IN_CALL_TEXTS, "IN_CALL_TEXTS"),
        (WAITING_TEXTS, "WAITING_TEXTS"),
        (END_TEXTS, "END_TEXTS"),
        (ALONE_TEXTS, "ALONE_TEXTS"),
    ]:
        for platform, signals in d.items():
            for sig in signals:
                assert sig == sig.lower(), f"{name}[{platform}]: {sig!r} not lowercase"


# ── In-call detection ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("platform,body", [
    ("google_meet", "leave call"),
    ("google_meet", "you're in the call with alice"),
    ("google_meet", "turn on camera to show video"),
    ("google_meet", "everyone in this call can see your screen"),
    ("zoom", "stop video recording now"),
    ("zoom", "audio connected successfully"),
    ("zoom", "end meeting for all"),
    ("microsoft_teams", "you're in the meeting now"),
    ("microsoft_teams", "leave the call"),
    ("microsoft_teams", "raise your hand to speak"),
])
def test_in_call_positive(platform, body):
    assert text_signals_in_call(body, platform), f"{platform}: {body!r} should match in-call"


@pytest.mark.parametrize("platform,body", [
    ("google_meet", "waiting to be admitted"),
    ("google_meet", "hello world"),
    ("zoom", "waiting for the host to start"),
    ("microsoft_teams", "lobby"),
    ("onepizza", "some random text"),  # no in-call text signals for onepizza
])
def test_in_call_negative(platform, body):
    assert not text_signals_in_call(body, platform), f"{platform}: {body!r} should NOT match in-call"


# ── Waiting-room detection ────────────────────────────────────────────────────

@pytest.mark.parametrize("platform,body", [
    ("google_meet", "waiting to be admitted to the meeting"),
    ("google_meet", "you are in the waiting room"),
    ("google_meet", "someone will let you in shortly"),
    ("zoom", "waiting for the host to start the meeting"),
    ("zoom", "you are in the waiting room"),
    ("microsoft_teams", "waiting for others to join"),
    ("microsoft_teams", "someone in the meeting should let you in"),
    ("microsoft_teams", "you are in the lobby"),
])
def test_waiting_positive(platform, body):
    assert text_signals_waiting(body, platform), f"{platform}: {body!r} should match waiting"


@pytest.mark.parametrize("platform,body", [
    ("google_meet", "leave call"),
    ("zoom", "audio connected"),
    ("onepizza", "you are in the lobby"),  # onepizza uses DOM check, no text
])
def test_waiting_negative(platform, body):
    assert not text_signals_waiting(body, platform), f"{platform}: {body!r} should NOT match waiting"


# ── End-of-call detection ─────────────────────────────────────────────────────

@pytest.mark.parametrize("platform,body", [
    ("google_meet", "you left the meeting"),
    ("google_meet", "the call has ended"),
    ("google_meet", "the meeting ended at 3pm"),
    ("google_meet", "you've been removed from the meeting"),
    ("zoom", "this meeting has been ended by the host"),
    ("zoom", "the meeting is ended"),
    ("zoom", "this meeting has ended"),
    ("microsoft_teams", "the meeting has ended"),
    ("microsoft_teams", "call ended"),
    ("microsoft_teams", "you left the meeting"),
    ("onepizza", "meeting ended"),
    ("onepizza", "the meeting has ended"),
    ("onepizza", "you left the room"),
])
def test_ended_positive(platform, body):
    assert text_signals_ended(body, platform), f"{platform}: {body!r} should match ended"


@pytest.mark.parametrize("platform,body", [
    ("google_meet", "leave call"),
    ("zoom", "stop video"),
    ("microsoft_teams", "you're in the meeting"),
    ("onepizza", "welcome to your meeting"),
])
def test_ended_negative(platform, body):
    assert not text_signals_ended(body, platform), f"{platform}: {body!r} should NOT match ended"


# ── Alone detection ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("platform,body", [
    ("google_meet", "no one else is here"),
    ("google_meet", "you're the only one in this call"),
    ("google_meet", "you are the only one here"),
    ("google_meet", "no one else has joined yet"),
    ("google_meet", "add others to this call"),
    ("zoom", "you are the only participant in this meeting"),
    ("zoom", "waiting for others to join the meeting"),
    ("microsoft_teams", "you're the only one here"),
    ("microsoft_teams", "you are the only one here"),
    ("microsoft_teams", "no one else is here"),
])
def test_alone_positive(platform, body):
    assert text_signals_alone(body, platform), f"{platform}: {body!r} should match alone"


@pytest.mark.parametrize("platform,body", [
    ("google_meet", "alice and bob are here"),
    ("zoom", "3 participants"),
    ("microsoft_teams", "everyone joined"),
    # onepizza has no text signals — always returns False regardless of body text
    ("onepizza", "no one else is here"),
    ("onepizza", "you are the only participant"),
])
def test_alone_negative(platform, body):
    assert not text_signals_alone(body, platform), f"{platform}: {body!r} should NOT match alone"


def test_alone_onepizza_always_false():
    """onepizza alone detection is DOM-only; text_signals_alone must return False."""
    assert not text_signals_alone("you are the only participant", "onepizza")
    assert not text_signals_alone("no one else is here", "onepizza")
    assert not text_signals_alone("", "onepizza")


# ── Unknown platform ──────────────────────────────────────────────────────────

def test_unknown_platform_never_matches():
    body = "leave call you left the meeting waiting to be admitted"
    assert not text_signals_in_call(body, "unknown_platform")
    assert not text_signals_waiting(body, "unknown_platform")
    assert not text_signals_ended(body, "unknown_platform")
    assert not text_signals_alone(body, "unknown_platform")


# ── Case sensitivity ──────────────────────────────────────────────────────────

def test_matchers_require_lowercase_input():
    """Matchers expect already-lowercased body; uppercase input should NOT match."""
    assert not text_signals_in_call("LEAVE CALL", "google_meet")
    assert not text_signals_ended("YOU LEFT THE MEETING", "google_meet")
