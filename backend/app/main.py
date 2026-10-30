"""FastAPI application factory."""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api.routes import companies, documents, health
from app.core.config import Settings, get_settings
from app.core.db import create_engine, create_sessionmaker
from app.core.errors import error_body, register_exception_handlers
from app.core.logging import configure_logging
from app.services.embeddings import create_embedding_provider
from app.services.ingestion import ProcessingTracker
from app.services.storage import PdfStorage

__version__ = "0.1.0"

# Allowance for multipart framing and form fields on top of the file itself.
_MULTIPART_OVERHEAD_BYTES = 1024 * 1024


class MaxBodySizeMiddleware:
    """Reject requests whose declared Content-Length is over the limit before reading them.

    The upload service still enforces the exact limit while streaming, which also
    covers chunked requests without a Content-Length.
    """

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            for name, value in scope["headers"]:
                if name == b"content-length":
                    if value.isdigit() and int(value) > self.max_bytes:
                        await _send_json_error(
                            send, 413, "payload_too_large", "Request body too large."
                        )
                        return
                    break
        await self.app(scope, receive, send)


async def _send_json_error(send: Send, status_code: int, code: str, message: str) -> None:
    body = json.dumps(error_body(code, message)).encode()
    start: Message = {
        "type": "http.response.start",
        "status": status_code,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ],
    }
    await send(start)
    await send({"type": "http.response.body", "body": body})


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    engine = create_engine(settings.database_url, echo=settings.db_echo)
    settings.upload_dir.mkdir(parents=True, exist_ok=True)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await engine.dispose()

    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        summary="Track what management promised vs what they delivered.",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.sessionmaker = create_sessionmaker(engine)
    app.state.storage = PdfStorage(settings.upload_dir, settings.max_upload_bytes)
    app.state.embedder = create_embedding_provider(settings)
    app.state.processing = ProcessingTracker()

    register_exception_handlers(app)
    # Middleware added last runs first: CORS wraps everything so error responses carry it too.
    app.add_middleware(
        MaxBodySizeMiddleware, max_bytes=settings.max_upload_bytes + _MULTIPART_OVERHEAD_BYTES
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health.router)
    app.include_router(companies.router)
    app.include_router(documents.router)
    return app


app = create_app()
