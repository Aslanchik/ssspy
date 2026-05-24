from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SSSPY_",
        env_file=".env",
        extra="ignore",
    )

    database_url: str = Field(...)
    listen_addr: str = "0.0.0.0:4318"
    max_body_bytes: int = 4 * 1024 * 1024
    log_level: str = "INFO"
