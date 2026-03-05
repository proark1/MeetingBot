import os
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    ANTHROPIC_API_KEY: str = ""
    SECRET_KEY: str = "recall-ai-clone-dev-secret-change-in-production"
    DATABASE_URL: str = "sqlite+aiosqlite:///./meetingbot.db"
    BOT_NAME_DEFAULT: str = "MeetingBot"
    WEBHOOK_TIMEOUT_SECONDS: int = 10
    BOT_SIMULATION_DURATION: int = 15  # seconds a simulated meeting lasts (set higher for realistic demos)

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()
