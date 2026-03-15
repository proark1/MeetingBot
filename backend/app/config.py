from urllib.parse import quote_plus

from pydantic import model_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # AI — set either key to enable AI features; Anthropic takes precedence over Gemini
    ANTHROPIC_API_KEY: str = ""
    GEMINI_API_KEY: str = ""

    # App
    SECRET_KEY: str = "meetingbot-dev-secret-change-in-production"

    # ── Database ──────────────────────────────────────────────────────────────
    # Option A — set DATABASE_URL directly (any SQLAlchemy async URL).
    # Option B — set SUPABASE_URL + SUPABASE_DB_PASSWORD (easiest for Railway).
    #            The host is derived from SUPABASE_URL automatically.
    # "postgres://" and "postgresql://" are both accepted — rewritten to asyncpg.
    DATABASE_URL: str = "sqlite+aiosqlite:///./meetingbot.db"

    # Supabase connection via API project URL + database password (Option B).
    #
    # SUPABASE_URL  — your project URL from Supabase dashboard → Settings → API
    #                 e.g. https://abcdefghijklm.supabase.co
    # SUPABASE_DB_PASSWORD — the Postgres password set when you created the project
    #                        (Settings → Database → Database password)
    #
    # The remaining fields default to Supabase's standard values and rarely need changing.
    # SUPABASE_DB_HOST overrides the derived host (db.[ref].supabase.co).  Use this to
    # point at the session pooler instead of the direct connection, e.g.:
    #   SUPABASE_DB_HOST=aws-0-us-east-1.pooler.supabase.com
    #   SUPABASE_DB_USER=postgres.[ref]   (pooler requires the project-ref suffix)
    #   SUPABASE_DB_PORT=5432             (or 6543 for transaction pooler)
    SUPABASE_URL: str = ""           # https://[ref].supabase.co
    SUPABASE_DB_PASSWORD: str = ""   # database password (NOT the anon/service key)
    SUPABASE_DB_HOST: str = ""       # optional host override (e.g. pooler hostname)
    SUPABASE_DB_USER: str = "postgres"
    SUPABASE_DB_NAME: str = "postgres"
    SUPABASE_DB_PORT: int = 5432     # 5432 = direct/session pooler, 6543 = transaction pooler

    @model_validator(mode="after")
    def build_database_url(self) -> "Settings":
        """Build DATABASE_URL from SUPABASE_URL + SUPABASE_DB_PASSWORD if provided.

        Priority order:
        1. DATABASE_URL explicitly set to a Postgres URL → used as-is (no override).
        2. SUPABASE_URL + SUPABASE_DB_PASSWORD both set → DATABASE_URL is derived.
           Use SUPABASE_DB_HOST to override the database host (e.g. session pooler).
        3. Fallback → SQLite default (development only).

        On Railway + Supabase free tier the direct connection (db.[ref].supabase.co:5432)
        is IPv6-only and unreachable from IPv4 networks.  Fix: set DATABASE_URL in Railway
        to the "Session pooler" connection string from Supabase → Settings → Database.
        """
        # If the user explicitly provided a non-SQLite DATABASE_URL, respect it and
        # do NOT override it — even when SUPABASE_* variables are also set.
        db_explicitly_set = (
            "DATABASE_URL" in self.model_fields_set
            and not self.DATABASE_URL.startswith("sqlite")
        )
        if db_explicitly_set:
            return self

        if self.SUPABASE_URL and self.SUPABASE_DB_PASSWORD:
            # Extract project ref from https://[ref].supabase.co
            ref = self.SUPABASE_URL.rstrip("/").removeprefix("https://").split(".")[0]
            host = self.SUPABASE_DB_HOST or f"db.{ref}.supabase.co"
            password = quote_plus(self.SUPABASE_DB_PASSWORD)
            self.DATABASE_URL = (
                f"postgresql://{self.SUPABASE_DB_USER}:{password}"
                f"@{host}:{self.SUPABASE_DB_PORT}/{self.SUPABASE_DB_NAME}"
            )
        return self
    BOT_NAME_DEFAULT: str = "MeetingBot"
    WEBHOOK_TIMEOUT_SECONDS: int = 10
    BOT_SIMULATION_DURATION: int = 15  # seconds for unsupported-platform demo mode

    # Real browser bot
    BOT_ADMISSION_TIMEOUT: int = 300   # seconds to wait for host to admit the bot
    BOT_MAX_DURATION: int = 7200       # max meeting length in seconds (2 hours)
    BOT_ALONE_TIMEOUT: int = 300       # seconds alone before bot leaves (5 minutes)

    # Security
    # If set, all /api/v1/* endpoints require:  Authorization: Bearer <API_KEY>
    # Leave empty to disable auth (backward-compatible default).
    API_KEY: str = ""

    # CORS — comma-separated list of allowed origins, or "*" for all.
    # When set to specific origins, credentials are allowed.
    # When "*", credentials are disabled (browsers reject credentialed wildcard CORS).
    CORS_ORIGINS: str = "*"

    # Concurrency — maximum number of browser bots that can run simultaneously.
    # Each bot spawns a Chromium process + ffmpeg; too many will crash the container.
    MAX_CONCURRENT_BOTS: int = 3

    # Join retry — extra attempts after the first failure before giving up.
    BOT_JOIN_MAX_RETRIES: int = 2      # 0 = no retry
    BOT_JOIN_RETRY_DELAY_S: int = 30   # seconds between attempts

    # Email summary (optional) — set SMTP_HOST to enable
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASS: str = ""
    SMTP_FROM: str = "meetingbot@example.com"
    BASE_URL: str = ""  # e.g. https://meetingbot-production-4d6a.up.railway.app

    # Slack integration (optional) — set SLACK_WEBHOOK_URL to enable
    SLACK_WEBHOOK_URL: str = ""

    # Notion integration (optional)
    NOTION_API_KEY: str = ""
    NOTION_DATABASE_ID: str = ""

    # Linear integration (optional)
    LINEAR_API_KEY: str = ""
    LINEAR_TEAM_ID: str = ""

    # Jira integration (optional) — creates tasks for action items
    JIRA_BASE_URL: str = ""         # e.g. https://yourcompany.atlassian.net
    JIRA_EMAIL: str = ""            # Atlassian account email
    JIRA_API_TOKEN: str = ""        # Atlassian API token
    JIRA_PROJECT_KEY: str = ""      # e.g. ENG or PROJ

    # HubSpot CRM integration (optional) — logs meeting notes as engagements
    HUBSPOT_API_KEY: str = ""       # Private App access token

    # Calendar auto-join (optional) — iCal URL polled every 5 minutes
    # Use Google Calendar's "Secret address in iCal format" or any iCal feed
    CALENDAR_ICAL_URL: str = ""

    # Transcription language (optional) — BCP-47 code, e.g. "en", "es", "fr", "de"
    # Leave empty to use Gemini's auto-detection (default)
    TRANSCRIPTION_LANGUAGE: str = ""

    # Weekly digest email (optional) — comma-separated recipients
    DIGEST_EMAIL: str = ""

    # Recording retention — delete WAV files older than this many days (0 = keep forever)
    RECORDING_RETENTION_DAYS: int = 30

    # ── Billing / Stripe ──────────────────────────────────────────────────────
    STRIPE_SECRET_KEY: str = ""          # sk_live_... or sk_test_...
    STRIPE_WEBHOOK_SECRET: str = ""      # whsec_... for verifying Stripe webhook signatures
    STRIPE_PRICE_PER_MEETING: int = 0    # cents — flat fee per meeting (0 = free)
    STRIPE_PRICE_PER_1K_TOKENS: int = 0  # cents — usage-based per 1K AI tokens (0 = free)
    # Markup multiplier applied on top of raw AI cost for billing purposes.
    # E.g. 2.0 means you charge 2× what the AI providers charge you.
    BILLING_COST_MARKUP: float = 2.0

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()
