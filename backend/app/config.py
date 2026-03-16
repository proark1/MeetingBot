from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # AI — set either key to enable AI features; Anthropic takes precedence over Gemini
    ANTHROPIC_API_KEY: str = ""
    GEMINI_API_KEY: str = ""

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
    BOT_NAME_DEFAULT: str = "MeetingBot"
    BOT_SIMULATION_DURATION: int = 15   # seconds for unsupported-platform demo mode
    BOT_ADMISSION_TIMEOUT: int = 300    # seconds to wait for host to admit the bot
    BOT_MAX_DURATION: int = 7200        # max meeting length in seconds (2 hours)
    BOT_ALONE_TIMEOUT: int = 300        # seconds alone before the bot leaves (5 min)

    # Concurrency — max simultaneous browser bots
    MAX_CONCURRENT_BOTS: int = 3

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

    # USDC/ERC-20 — leave CRYPTO_RPC_URL empty to disable crypto payments
    CRYPTO_HD_SEED: str = ""          # hex seed for HD wallet (generate once, keep secret)
    CRYPTO_RPC_URL: str = ""          # Infura/Alchemy endpoint, e.g. https://mainnet.infura.io/v3/...
    USDC_CONTRACT: str = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"

    # Billing
    CREDIT_MARKUP: float = 3.0        # multiply raw AI cost by this factor when deducting credits (unused when flat fee is enabled)
    BOT_FLAT_FEE_USD: float = 0.10    # flat fee charged per bot usage (0 = use markup-based pricing)
    MIN_CREDITS_USD: float = 0.10     # minimum balance required to create a bot

    # JWT for web UI sessions
    JWT_SECRET: str = "change-me-in-production"
    JWT_EXPIRE_HOURS: int = 24

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

    # ── Subscription plans ────────────────────────────────────────────────────
    # Plan bot limits: -1 = unlimited.  Enforced at bot creation.
    PLAN_FREE_BOTS_PER_MONTH: int = 5
    PLAN_STARTER_BOTS_PER_MONTH: int = 50
    PLAN_PRO_BOTS_PER_MONTH: int = 500
    PLAN_BUSINESS_BOTS_PER_MONTH: int = -1

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

    # ── Idempotency ───────────────────────────────────────────────────────────
    IDEMPOTENCY_TTL_HOURS: int = 24       # how long to cache key → bot_id mappings

    # ── Webhook retry ─────────────────────────────────────────────────────────
    WEBHOOK_MAX_ATTEMPTS: int = 5
    # Comma-separated backoff delays in seconds (1 min, 5 min, 25 min, 2 h, 10 h)
    WEBHOOK_RETRY_DELAYS: str = "60,300,1500,7200,36000"
    WEBHOOK_DELIVERY_RETENTION_DAYS: int = 30  # prune delivery logs older than N days

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()
