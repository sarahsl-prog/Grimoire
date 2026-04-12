"""FastAPI application factory for the Grimoire REST API."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from grimoire.api.routes import categories, documents, generate, ingest, query, watch


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Manage application startup and shutdown."""
    from grimoire.config.settings import get_settings
    from grimoire.db.session import close_db, initialize_db

    settings = get_settings()
    await initialize_db(settings.database.url)
    try:
        yield
    finally:
        await close_db()


def create_app(use_lifespan: bool = True) -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title="Grimoire",
        description="Knowledge management and content generation API.",
        version="0.1.0",
        lifespan=lifespan if use_lifespan else None,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(ingest.router, prefix="/api/v1")
    app.include_router(query.router, prefix="/api/v1")
    app.include_router(documents.router, prefix="/api/v1")
    app.include_router(categories.router, prefix="/api/v1")
    app.include_router(generate.router, prefix="/api/v1")
    app.include_router(watch.router, prefix="/api/v1")

    @app.get("/health")
    async def health_check() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
