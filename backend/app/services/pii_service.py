"""PII detection and redaction service.

Detects and optionally redacts the following PII types from transcript text:
  - Email addresses
  - Phone numbers (US and international formats)
  - US Social Security Numbers (SSN)
  - Credit card numbers (with Luhn validation)

All functions are synchronous and pure — no external dependencies.
"""

import re
from dataclasses import dataclass
from typing import Optional

# ── Regex patterns ─────────────────────────────────────────────────────────────

_EMAIL_RE = re.compile(
    r'[\w.+\-]+@[\w\-]+\.[\w.\-]+',
    re.IGNORECASE,
)

_PHONE_RE = re.compile(
    r'(?<!\d)'
    r'(\+?1[\s.\-]?)?'
    r'(\(?\d{3}\)?[\s.\-]?)'
    r'\d{3}[\s.\-]?\d{4}'
    r'(?!\d)',
)

_SSN_RE = re.compile(
    r'(?<!\d)'
    r'\d{3}[- ]\d{2}[- ]\d{4}'
    r'(?!\d)',
)

# Matches sequences of 13-16 digits (with optional spaces/dashes between groups)
_CC_RE = re.compile(
    r'(?<!\d)'
    r'(\d{4}[\s\-]?){3}\d{4}'
    r'(?!\d)',
)


# ── Luhn algorithm ─────────────────────────────────────────────────────────────

def _luhn_valid(number: str) -> bool:
    """Return True if the digit string passes the Luhn checksum."""
    digits = [int(d) for d in number if d.isdigit()]
    if len(digits) < 13 or len(digits) > 19:
        return False
    total = 0
    for i, digit in enumerate(reversed(digits)):
        if i % 2 == 1:
            doubled = digit * 2
            total += doubled - 9 if doubled > 9 else doubled
        else:
            total += digit
    return total % 10 == 0


# ── Public API ─────────────────────────────────────────────────────────────────

@dataclass
class PIIMatch:
    type: str       # "email" | "phone" | "ssn" | "credit_card"
    value: str      # matched text
    start: int      # character offset in original string
    end: int        # character offset in original string


def detect_pii(text: str) -> list[PIIMatch]:
    """Return all PII matches found in *text*, sorted by start position."""
    matches: list[PIIMatch] = []

    for m in _EMAIL_RE.finditer(text):
        matches.append(PIIMatch("email", m.group(), m.start(), m.end()))

    for m in _PHONE_RE.finditer(text):
        matches.append(PIIMatch("phone", m.group(), m.start(), m.end()))

    for m in _SSN_RE.finditer(text):
        matches.append(PIIMatch("ssn", m.group(), m.start(), m.end()))

    for m in _CC_RE.finditer(text):
        raw = m.group()
        digits_only = re.sub(r'\D', '', raw)
        if _luhn_valid(digits_only):
            matches.append(PIIMatch("credit_card", raw, m.start(), m.end()))

    # Sort and deduplicate overlapping matches (keep longest)
    matches.sort(key=lambda x: (x.start, -(x.end - x.start)))
    deduped: list[PIIMatch] = []
    last_end = -1
    for m in matches:
        if m.start >= last_end:
            deduped.append(m)
            last_end = m.end

    return deduped


def redact_pii(text: str, replacement: str = "[REDACTED]") -> str:
    """Replace all PII spans in *text* with *replacement*."""
    pii = detect_pii(text)
    if not pii:
        return text

    result = []
    prev = 0
    for match in pii:
        result.append(text[prev:match.start])
        result.append(replacement)
        prev = match.end
    result.append(text[prev:])
    return "".join(result)


def redact_transcript(
    transcript: list[dict],
    replacement: str = "[REDACTED]",
) -> list[dict]:
    """Return a copy of *transcript* with PII redacted from each entry's text field."""
    redacted = []
    for entry in transcript:
        text = entry.get("text", "") or ""
        new_text = redact_pii(text, replacement) if text else text
        if new_text != text:
            entry = dict(entry)
            entry["text"] = new_text
        redacted.append(entry)
    return redacted


def transcript_has_pii(transcript: list[dict]) -> bool:
    """Return True if any transcript entry contains detectable PII."""
    for entry in transcript:
        text = entry.get("text", "") or ""
        if text and detect_pii(text):
            return True
    return False
