import typer
import uvicorn

from .config import Settings
from .main import create_app

app = typer.Typer(help="ssspy — OTel trace ingestion and eval platform")


@app.callback()
def _root() -> None:
    """ssspy command-line interface."""


@app.command()
def serve() -> None:
    """Start the HTTP server (OTLP receiver + web UI)."""
    settings = Settings()
    host, port = settings.listen_addr.rsplit(":", 1)
    uvicorn.run(
        create_app(settings),
        host=host,
        port=int(port),
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    app()
