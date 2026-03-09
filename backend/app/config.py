from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # AI
    GEMINI_API_KEY: str = ""

    # App
    SECRET_KEY: str = "meetingbot-dev-secret-change-in-production"
    DATABASE_URL: str = "sqlite+aiosqlite:///./meetingbot.db"
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

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()
