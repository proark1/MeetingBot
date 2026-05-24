"""Unit tests for Settings parsing helpers."""

from app.config import Settings


def test_stripe_top_up_amounts_parses_and_skips_junk():
    s = Settings(STRIPE_TOP_UP_AMOUNTS="10, 25 ,x, 50,")
    assert s.stripe_top_up_amounts == [10, 25, 50]


def test_stripe_top_up_amounts_default():
    assert Settings().stripe_top_up_amounts == [10, 25, 50, 100]


def test_stripe_top_up_amounts_empty():
    assert Settings(STRIPE_TOP_UP_AMOUNTS="").stripe_top_up_amounts == []


def test_ai_transcript_max_chars_default():
    assert Settings().AI_TRANSCRIPT_MAX_CHARS == 200_000


def test_async_database_url_translates_postgres_schemes():
    assert Settings(DATABASE_URL="postgresql://u:p@h/db").async_database_url == \
        "postgresql+asyncpg://u:p@h/db"
    assert Settings(DATABASE_URL="postgres://u:p@h/db").async_database_url == \
        "postgresql+asyncpg://u:p@h/db"
    # sqlite is passed through unchanged
    assert Settings(DATABASE_URL="sqlite+aiosqlite:///./x.db").async_database_url == \
        "sqlite+aiosqlite:///./x.db"


def test_plan_limits_mapping():
    limits = Settings().plan_limits
    assert set(limits) == {"free", "starter", "pro", "business"}
    assert limits["business"] == -1
