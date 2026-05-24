import pytest

from ssspy.config import Settings


@pytest.fixture
def settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://gophish:gophish@localhost:5432/ssspy_test",
        listen_addr="127.0.0.1:0",
        max_body_bytes=4 * 1024 * 1024,
        log_level="WARNING",
    )
