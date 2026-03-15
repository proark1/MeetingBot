from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # AI — set either key to enable AI features; Anthropic takes precedence over Gemini
    ANTHROPIC_API_KEY: str = ""
    GEMINI_API_KEY: str = ""

    # API key for authenticating client requests (Bearer token)
    # Leave empty to disable authentication (useful for internal deployments)
    API_KEY: str = ""

    # CORS — comma-separated list of allowed origins, or "*" for all
    CORS_ORIGINS: str = "*"

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

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()
