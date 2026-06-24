from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # AI — set either key to enable AI features; Anthropic takes precedence over Gemini
    ANTHROPIC_API_KEY: str = ""
    GEMINI_API_KEY: str = ""

    # Cap on the rendered transcript length (characters) sent to the LLM for
    # analysis/chapters/Q&A. Multi-hour meetings can otherwise send tens of
    # thousands of tokens; over this budget the middle is elided, keeping the
    # opening and closing. 0 disables the cap. ~200k chars ≈ 50k tokens.
    AI_TRANSCRIPT_MAX_CHARS: int = 200_000

    # Claude model + thinking mode for post-meeting analysis/chapters/email/brief.
    # Defaults to Sonnet without extended thinking: ~5-10× cheaper and faster than
    # Opus+adaptive-thinking while still producing high-quality schema-shaped JSON.
    # Set AI_ANALYSIS_MODEL=claude-opus-4-6 and AI_ANALYSIS_THINKING=true to opt
    # back into the previous behaviour for maximum depth.
    AI_ANALYSIS_MODEL: str = "claude-sonnet-4-6"
    AI_ANALYSIS_THINKING: bool = False

    # API key for authenticating client requests (Bearer token)
    # Leave empty to disable authentication (useful for internal deployments)
    API_KEY: str = ""

    # CORS — comma-separated allowed origins.  Set to your frontend domain(s) in
    # production (e.g. "https://app.example.com").  "*" allows all origins and
    # should only be used during local development.
    CORS_ORIGINS: str = "*"

    # Admin — comma-separated list of emails granted admin access.
    # Accounts with is_admin=True in the DB are also granted access.
    ADMIN_EMAILS: str = ""

    # Bot defaults
    BOT_NAME_DEFAULT: str = "JustHereToListen.io"
    BOT_SIMULATION_DURATION: int = 15   # seconds for unsupported-platform demo mode
    BOT_ADMISSION_TIMEOUT: int = 300    # seconds to wait for host to admit the bot
    BOT_MAX_DURATION: int = 7200        # max meeting length in seconds (2 hours)
    BOT_ALONE_TIMEOUT: int = 300        # seconds alone before the bot leaves (5 min)
    # Hard wall-clock ceiling on an actively-running bot (joining→transcribing).
    # A safety net above BOT_MAX_DURATION (+ transcription/analysis headroom): if
    # a bot's lifecycle hangs or exits without setting a terminal state, the
    # reaper force-terminates it so it can't occupy a concurrency slot forever.
    BOT_LIFECYCLE_MAX_SECONDS: int = 10800  # 3 hours

    # Concurrency — max simultaneous browser bots
    MAX_CONCURRENT_BOTS: int = 3

    # Max concurrent per-entry AI/IO fan-out tasks per live bot (translation,
    # action-item extraction, decision detection, coaching, agentic, etc.).
    # Bounds memory/connection use on fast or long meetings with features on.
    LIVE_ENTRY_MAX_CONCURRENCY: int = 8

    # Join retry
    BOT_JOIN_MAX_RETRIES: int = 2
    BOT_JOIN_RETRY_DELAY_S: int = 30

    # Transcription language — BCP-47 code (e.g. "en", "es").  Empty = auto-detect.
    TRANSCRIPTION_LANGUAGE: str = ""

    # Webhook delivery timeout
    WEBHOOK_TIMEOUT_SECONDS: int = 10

    # ── Multi-tenant billing ──────────────────────────────────────────────────
    # Railway injects DATABASE_URL as "postgresql://..." — translate to asyncpg driver.
    # SQLite (default for local dev) stays as-is.
    DATABASE_URL: str = "sqlite+aiosqlite:///./meetingbot.db"

    @property
    def async_database_url(self) -> str:
        url = self.DATABASE_URL
        if url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
        elif url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql+asyncpg://", 1)
        return url

    # Stripe — leave empty to disable card payments
    STRIPE_SECRET_KEY: str = ""
    STRIPE_WEBHOOK_SECRET: str = ""
    STRIPE_TOP_UP_AMOUNTS: str = "10,25,50,100"  # comma-separated USD amounts

    @property
    def stripe_top_up_amounts(self) -> list[int]:
        """Parsed top-up amounts (USD). Single source of truth for the routes."""
        out: list[int] = []
        for part in self.STRIPE_TOP_UP_AMOUNTS.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                out.append(int(part))
            except ValueError:
                continue
        return out

    # USDC/ERC-20 — leave CRYPTO_RPC_URL empty to disable crypto payments
    CRYPTO_HD_SEED: str = ""          # hex seed for HD wallet (generate once, keep secret)
    CRYPTO_RPC_URL: str = ""          # Infura/Alchemy endpoint, e.g. https://mainnet.infura.io/v3/...
    USDC_CONTRACT: str = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
    # Require a wallet-ownership signature when linking a USDC deposit address.
    # When True, PUT /auth/wallet must include a `signature` over the challenge
    # message (GET /auth/wallet/challenge) proving control of the private key —
    # this prevents an attacker from front-running registration of a victim's
    # publicly-known address and stealing their deposits. Recommended ON for any
    # deployment where USDC top-ups are enabled; a signed request is always
    # verified regardless of this flag.
    REQUIRE_WALLET_SIGNATURE: bool = True

    # Billing
    CREDIT_MARKUP: float = 3.0        # multiply raw AI cost by this factor when deducting credits (unused when flat fee is enabled)
    BOT_FLAT_FEE_USD: float = 0.10    # flat fee charged per bot usage (0 = use markup-based pricing)
    MIN_CREDITS_USD: float = 0.10     # minimum balance required to create a bot

    # JWT for web UI sessions
    JWT_SECRET: str = "change-me-in-production"
    JWT_EXPIRE_HOURS: int = 24

    # Dedicated key for encryption-at-rest of third-party integration tokens.
    # Kept separate from JWT_SECRET so a JWT-signing-key leak doesn't also
    # decrypt stored OAuth/Slack/Notion tokens (key separation). When empty,
    # falls back to deriving from JWT_SECRET for backward compatibility.
    ENCRYPTION_KEY: str = ""

    # Environment — set to "production" to enforce strict security defaults
    ENVIRONMENT: str = "development"

    # Root logging level (DEBUG/INFO/WARNING/ERROR). Production can raise this to
    # WARNING to cut log volume and shrink the PII surface in aggregated logs.
    LOG_LEVEL: str = "INFO"

    # ── Error tracking (Sentry) — optional, graceful no-op when unset ──────────
    # Set SENTRY_DSN to enable exception aggregation + performance tracing.
    SENTRY_DSN: str = ""
    SENTRY_TRACES_SAMPLE_RATE: float = 0.0   # 0.0 = errors only, no perf traces
    SENTRY_PROFILES_SAMPLE_RATE: float = 0.0

    # ── Synthetic canary ───────────────────────────────────────────────────────
    # When enabled, a background loop periodically drives the join→audio→
    # transcript→leave pipeline against configured test meetings (see
    # canary_service.CanaryConfig.from_env / CANARY_* vars) to catch browser_bot
    # selector drift before customers do. Off by default; needs test-meeting URLs.
    CANARY_ENABLED: bool = False
    CANARY_INTERVAL_S: int = 1800   # how often to run the canary sweep (seconds)

    # Public base URL (e.g. "https://api.justheretolisten.io"). Used as the
    # primary `servers[0].url` in the published OpenAPI schema so generated
    # SDK clients hit the right host out of the box. Leave unset in dev — the
    # schema will fall back to the local uvicorn URL.
    PUBLIC_BASE_URL: str = ""

    # When True, the rate-limit/IP resolver may consult X-Forwarded-For. Only
    # enable behind a trusted reverse proxy that overwrites or appends this
    # header. With this off (default), client-supplied X-Forwarded-For is
    # ignored and the TCP peer address is used instead.
    TRUST_PROXY_HEADERS: bool = False

    # Cap on how many in-memory BotSession rows analytics endpoints will scan
    # in a single request. Larger tenants should keep their analytics in the
    # ``bot_snapshots`` table (queried separately) rather than the 24-h RAM
    # window. Round-2 fix #15 lowers the default from 10000 to 2000 to bound
    # worst-case latency on the /analytics endpoints.
    ANALYTICS_BOT_SCAN_LIMIT: int = 2000

    # Admin platform-analytics: total terminal-bot snapshots aggregated, and how
    # many are fetched+parsed per batch. Batching bounds peak memory — instead of
    # holding all snapshot JSON blobs in RAM at once, only one batch is resident.
    ADMIN_ANALYTICS_SNAPSHOT_LIMIT: int = 50000
    ADMIN_ANALYTICS_BATCH: int = 2000

    # When False (default): if any Account row exists at startup and there are
    # no auth indicators (API_KEY unset), the auth dependency requires a Bearer
    # token even in dev mode. Set to True to keep the legacy unauthenticated
    # dev-mode behaviour for local prototyping (tests, demos).
    ALLOW_UNAUTHENTICATED_DEV_MODE: bool = False

    # ── Cloud storage ─────────────────────────────────────────────────────────
    STORAGE_BACKEND: str = "local"          # "local" | "s3"
    S3_BUCKET: str = ""
    S3_ENDPOINT_URL: str = ""               # custom endpoint for R2/MinIO
    S3_ACCESS_KEY_ID: str = ""
    S3_SECRET_ACCESS_KEY: str = ""
    S3_REGION: str = "us-east-1"
    S3_PUBLIC_URL: str = ""                 # optional CDN base URL

    # ── Email notifications ────────────────────────────────────────────────────
    EMAIL_BACKEND: str = "none"             # "none" | "smtp" | "sendgrid"
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USERNAME: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM_ADDRESS: str = ""
    SMTP_USE_TLS: str = "true"
    SENDGRID_API_KEY: str = ""

    # ── Calendar auto-join ─────────────────────────────────────────────────────
    CALENDAR_POLL_INTERVAL_S: int = 300     # how often to poll iCal feeds (5 min)

    # ── Action-item due-date reminders ─────────────────────────────────────────
    # Background loop fires action_item.due_soon / action_item.overdue webhook
    # events for open items with a parseable due_date. Disabled by default.
    ACTION_ITEM_REMINDERS_ENABLED: bool = False
    ACTION_ITEM_REMINDER_INTERVAL_S: int = 3600   # how often to scan (1h)
    ACTION_ITEM_DUE_SOON_HOURS: int = 24          # "due_soon" window before the due date

    # ── Subscription plans ────────────────────────────────────────────────────
    # Plan bot limits: -1 = unlimited.  Enforced at bot creation.
    PLAN_FREE_BOTS_PER_MONTH: int = 5
    PLAN_STARTER_BOTS_PER_MONTH: int = 50
    PLAN_PRO_BOTS_PER_MONTH: int = 500
    PLAN_BUSINESS_BOTS_PER_MONTH: int = -1

    # Stripe subscription price IDs (create in Stripe Dashboard → Products)
    STRIPE_STARTER_PRICE_ID: str = ""
    STRIPE_PRO_PRICE_ID: str = ""
    STRIPE_BUSINESS_PRICE_ID: str = ""

    @property
    def plan_limits(self) -> dict[str, int]:
        """Plan name → monthly bot limit (-1 = unlimited)."""
        return {
            "free": self.PLAN_FREE_BOTS_PER_MONTH,
            "starter": self.PLAN_STARTER_BOTS_PER_MONTH,
            "pro": self.PLAN_PRO_BOTS_PER_MONTH,
            "business": self.PLAN_BUSINESS_BOTS_PER_MONTH,
        }

    # ── Google / Microsoft SSO ────────────────────────────────────────────────
    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""
    MICROSOFT_CLIENT_ID: str = ""
    MICROSOFT_CLIENT_SECRET: str = ""
    # Base URL used to build OAuth callback URIs.
    # e.g. "https://app.acme.com" — server appends /api/v1/auth/oauth/{provider}/callback
    OAUTH_REDIRECT_BASE_URL: str = "http://localhost:8000"

    # ── Bot persona ───────────────────────────────────────────────────────────
    DEFAULT_BOT_AVATAR_URL: str = ""    # platform-default avatar (per-bot can override)

    # ── Video recording ───────────────────────────────────────────────────────
    VIDEO_RECORDING_ENABLED: bool = True  # set False to globally disable video
    VIDEO_CRF: int = 28                   # ffmpeg CRF (lower = better quality, larger file)
    VIDEO_FPS: int = 15                   # capture framerate
    VIDEO_SCALE: str = "1280:720"         # output resolution WxH

    # ── Bot store memory management ──────────────────────────────────────────
    BOT_TTL_HOURS: int = 24               # how long to keep completed bots in memory
    STORE_CLEANUP_INTERVAL_SECONDS: int = 1800   # how often to run cleanup (30 min)
    STORE_MAX_BOTS: int = 10000           # max bots in memory before LRU eviction

    # ── Bot-state backend (distributed-state groundwork) ──────────────────────
    # "memory" (default) = in-process Store singleton. "redis" = shared
    # RedisBotStateStore (requires REDIS_URL) so multiple workers can share live
    # bot state. See app.store.get_bot_state_store — selecting "redis" is inert
    # until call sites are migrated to that accessor and validated on staging.
    BOT_STATE_BACKEND: str = "memory"     # "memory" | "redis"
    REDIS_URL: str = ""                   # e.g. redis://localhost:6379/0

    # ── Idempotency ───────────────────────────────────────────────────────────
    IDEMPOTENCY_TTL_HOURS: int = 24       # how long to cache key → bot_id mappings

    # ── Webhook retry ─────────────────────────────────────────────────────────
    WEBHOOK_MAX_ATTEMPTS: int = 5
    # Comma-separated backoff delays in seconds (1 min, 5 min, 25 min, 2 h, 10 h)
    WEBHOOK_RETRY_DELAYS: str = "60,300,1500,7200,36000"
    WEBHOOK_DELIVERY_RETENTION_DAYS: int = 30  # prune delivery logs older than N days

    # ── Consent announcement ───────────────────────────────────────────────────
    # Default ON: recording participants without notice is unlawful in two-party/
    # all-party-consent US states (CA, FL, IL, PA, WA, …) and under EU GDPR/
    # ePrivacy. Per-bot `consent_enabled` can override. Set this False only if
    # your tenants obtain consent out-of-band and you accept the liability.
    CONSENT_ANNOUNCEMENT_ENABLED: bool = True
    CONSENT_MESSAGE: str = (
        "This meeting is being recorded and transcribed by an AI bot. "
        "To opt out, type 'opt out' in the chat or say 'opt out' clearly."
    )
    CONSENT_OPT_OUT_PHRASE: str = "opt out"  # case-insensitive phrase to trigger opt-out

    # ── Data retention ─────────────────────────────────────────────────────────
    # Global defaults — overridable per account via the /auth/retention endpoint.
    DEFAULT_BOT_RETENTION_DAYS: int = 90       # days to keep bot data in DB (-1 = forever)
    DEFAULT_RECORDING_RETENTION_DAYS: int = 30  # days to keep audio/video files
    RECORDING_RETENTION_DAYS: int = 30          # alias used by existing code / docs

    # ── Keyword alerts ─────────────────────────────────────────────────────────
    KEYWORD_ALERTS_ENABLED: bool = True  # set False to globally disable

    # ── HubSpot CRM integration ────────────────────────────────────────────────
    HUBSPOT_API_KEY: str = ""           # HubSpot private app access token

    # ── Salesforce CRM integration ─────────────────────────────────────────────
    SALESFORCE_CLIENT_ID: str = ""
    SALESFORCE_CLIENT_SECRET: str = ""
    SALESFORCE_USERNAME: str = ""
    SALESFORCE_PASSWORD: str = ""
    SALESFORCE_SECURITY_TOKEN: str = ""
    SALESFORCE_INSTANCE_URL: str = ""   # e.g. https://yourorg.salesforce.com

    # ── Local Whisper transcription ────────────────────────────────────────────
    WHISPER_ENABLED: bool = False       # set True to use local Whisper instead of Gemini
    WHISPER_MODEL: str = "base"         # Whisper model size: tiny, base, small, medium, large
    WHISPER_DEVICE: str = "cpu"         # "cpu" or "cuda" for GPU acceleration
    # Beam width. 1 (greedy) is ~5× faster on CPU for marginal accuracy loss and
    # avoids pegging a core for minutes on long meetings; raise for more accuracy.
    WHISPER_BEAM_SIZE: int = 1

    # ── Team workspaces ────────────────────────────────────────────────────────
    WORKSPACES_ENABLED: bool = True

    # ── SAML SSO ───────────────────────────────────────────────────────────────
    SAML_ENABLED: bool = False
    SAML_SP_BASE_URL: str = ""          # e.g. https://app.meetingbot.io (used in SP metadata)

    # ── MCP server ─────────────────────────────────────────────────────────────
    MCP_ENABLED: bool = False

    # ── PostgreSQL connection pool ─────────────────────────────────────────────
    # Tune these for your deployment's expected concurrency.
    # pool_size: number of persistent connections kept open.
    # max_overflow: extra connections allowed above pool_size under burst load.
    # pool_recycle: recycle connections older than N seconds (prevents stale-connection errors).
    # pool_timeout: seconds to wait for a free connection before raising OperationalError.
    DB_POOL_SIZE: int = 30
    DB_POOL_MAX_OVERFLOW: int = 20
    DB_POOL_RECYCLE_SECONDS: int = 1800   # 30 minutes
    DB_POOL_TIMEOUT: int = 30

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()
