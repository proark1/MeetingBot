"""Unit tests for pure helpers in intelligence_service.

These cover the efficiency-pass changes (transcript cap, batched-sentiment
coercion, cached prompt-prefix content blocks, client/model reuse) plus
long-standing helpers that had no coverage (fence stripping, cost estimation,
time-anchor normalisation).
"""

import pytest

from app.services import intelligence_service as I
from app.config import settings


def _mk(n, text="hello world", speaker="A"):
    return [{"timestamp": float(i), "speaker": speaker, "text": text} for i in range(n)]


def test_transcript_lines_under_budget_unchanged():
    out = I._transcript_lines(_mk(3))
    assert out == "[0.0s] A: hello world\n[1.0s] A: hello world\n[2.0s] A: hello world"


def test_transcript_lines_prepends_vocab_hint():
    out = I._transcript_lines(_mk(1), vocab_hint="VOCAB\n\n")
    assert out.startswith("VOCAB\n\n[0.0s] A:")


def test_transcript_lines_caps_and_elides_middle():
    old = settings.AI_TRANSCRIPT_MAX_CHARS
    settings.AI_TRANSCRIPT_MAX_CHARS = 300
    try:
        big = [{"timestamp": float(i), "speaker": f"S{i}", "text": "x" * 40} for i in range(50)]
        out = I._transcript_lines(big)
        assert "truncated for length" in out
        # keeps opening and closing, well under the full length
        assert out.startswith("[0.0s] S0:")
        assert "S49: " in out
        assert len(out) < 2000
    finally:
        settings.AI_TRANSCRIPT_MAX_CHARS = old


def test_transcript_lines_cap_disabled_with_zero():
    old = settings.AI_TRANSCRIPT_MAX_CHARS
    settings.AI_TRANSCRIPT_MAX_CHARS = 0
    try:
        big = [{"timestamp": float(i), "speaker": "S", "text": "x" * 100} for i in range(50)]
        out = I._transcript_lines(big)
        assert "truncated" not in out
    finally:
        settings.AI_TRANSCRIPT_MAX_CHARS = old


def test_coerce_sentiments_normalises_and_pads():
    assert I._coerce_sentiments(["positive", "bad", "negative"], 3) == ["positive", "neutral", "negative"]
    # too few -> padded with neutral; non-list -> all neutral
    assert I._coerce_sentiments(["positive"], 3) == ["positive", "neutral", "neutral"]
    assert I._coerce_sentiments("garbage", 2) == ["neutral", "neutral"]
    assert I._coerce_sentiments([], 0) == []


def test_user_content_cache_prefix_builds_blocks():
    blocks = I._user_content("the question", "BIG TRANSCRIPT")
    assert isinstance(blocks, list) and len(blocks) == 2
    assert blocks[0]["text"] == "BIG TRANSCRIPT"
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}
    assert blocks[1]["text"] == "the question"


def test_user_content_without_prefix_is_plain_string():
    assert I._user_content("just a prompt", None) == "just a prompt"


def test_strip_fences():
    assert I._strip_fences("```json\n{\"a\":1}\n```") == '{"a":1}'
    assert I._strip_fences("```\nplain\n```") == "plain"
    assert I._strip_fences("no fence") == "no fence"


def test_estimate_cost_known_and_date_suffixed_model():
    # claude-haiku-4-5: input 1/M, output 5/M
    c = I._estimate_cost("claude-haiku-4-5", 1_000_000, 1_000_000)
    assert round(c, 6) == round(1.0 + 5.0, 6)
    # date-suffixed id strips to the base price
    c2 = I._estimate_cost("claude-haiku-4-5-20251001", 1_000_000, 0)
    assert round(c2, 6) == 1.0
    # unknown model -> zero cost, never raises
    assert I._estimate_cost("made-up-model", 999, 999) == 0.0


def test_normalise_time_anchors_rescales_minutes_to_seconds():
    # transcript spans 1200s (20 min); model returned minutes (max=17)
    items = [{"start_time": 1, "end_time": 5}, {"start_time": 10, "end_time": 17}]
    out = I._normalise_time_anchors(items, transcript_max_ts=1200.0)
    # values multiplied by 60 then clamped into range
    assert out[0]["start_time"] == 60.0
    assert out[1]["end_time"] == 1020.0


def test_normalise_time_anchors_clamps_and_sorts():
    items = [{"start_time": 5000, "end_time": 6000}, {"start_time": 10, "end_time": 20}]
    out = I._normalise_time_anchors(items, transcript_max_ts=100.0)
    starts = [o["start_time"] for o in out]
    assert starts == sorted(starts)
    assert all(0 <= o["start_time"] <= 100.0 for o in out)


def test_anthropic_client_is_cached(monkeypatch):
    pytest.importorskip("anthropic")
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "unit-test-key")
    I._anthropic_clients.clear()
    a1 = I._get_anthropic_client()
    a2 = I._get_anthropic_client()
    assert a1 is a2


def test_gemini_model_is_cached(monkeypatch):
    pytest.importorskip("google.generativeai")
    monkeypatch.setattr(settings, "GEMINI_API_KEY", "unit-test-key")
    I._gemini_models.clear()
    m1 = I._get_gemini_model()
    m2 = I._get_gemini_model()
    assert m1 is m2
