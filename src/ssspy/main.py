from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from .config import Settings
from .logging import configure_logging


def create_app(settings: Settings) -> FastAPI:
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        from .db import dispose_engine, get_engine, run_migrations

        engine = get_engine(settings.database_url)
        await run_migrations(settings.database_url)
        app.state.engine = engine
        try:
            yield
        finally:
            await dispose_engine()

    app = FastAPI(title="ssspy", lifespan=lifespan)
    app.state.settings = settings

    from pathlib import Path

    from fastapi.staticfiles import StaticFiles

    from .ingest.routes import router as ingest_router
    from .web.routes import router as web_router

    app.include_router(ingest_router)
    app.include_router(web_router)

    static_dir = Path(__file__).parent / "web" / "static"
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
    return app
