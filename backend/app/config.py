import os
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    ANTHROPIC_API_KEY: str = ""
    SECRET_KEY: str = "recall-ai-clone-dev-secret-change-in-production"
    DATABASE_URL: str = "sqlite+aiosqlite:///./meetingbot.db"
    BOT_NAME_DEFAULT: str = "MeetingBot"
    WEBHOOK_TIMEOUT_SECONDS: int = 10
    BOT_SIMULATION_DURATION: int = 15  # seconds for unsupported-platform demo mode

    # Real browser bot settings
    BOT_ADMISSION_TIMEOUT: int = 300   # seconds to wait for host to admit the bot
    BOT_MAX_DURATION: int = 7200       # maximum meeting duration in seconds (2 hours)

    # Whisper transcription model: "tiny", "base", "small", "medium", "large"
    # "base" is a good default — fast on CPU, solid accuracy
    # Use "small" or "medium" for better accuracy at higher compute cost
    WHISPER_MODEL: str = "base"

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()
