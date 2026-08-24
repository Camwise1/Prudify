"""FastAPI application factory and process lifecycle."""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .api import routes_books, routes_jobs, routes_settings, routes_system
from .api.deps import require_api_key
from .config import Config, load_config
from .db import init_db, session_scope
from .logging_setup import configure_logging, start_db_logging, stop_db_logging, trim_logs
from .services import library as library_service
from .services.events import bus
from .services.queue import init_queue
from .services.watcher import init_watcher

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    config: Config = app.state.config

    configure_logging(config.log_level, config.log_path())
    init_db(config.database_path())
    start_db_logging()
    trim_logs()

    bus.bind_loop(asyncio.get_running_loop())

    queue = init_queue(config)
    queue.start()
    app.state.queue = queue

    use_polling = os.environ.get("PRUDIFY_POLLING_WATCHER", "").lower() in {"1", "true", "yes"}
    watcher = init_watcher(config, use_polling=use_polling)
    watcher.start()
    app.state.watcher = watcher

    log.info("Prudify %s listening on %s:%s", __version__, config.server.host, config.server.port)
    if config.server.require_api_key:
        log.info("API key: %s", config.server.api_key)
    if not config.libraries:
        log.info("No libraries configured yet -- add one in Settings to get started.")
    else:
        # A scan at startup catches anything added while the service was down.
        async def initial_scan() -> None:
            await asyncio.sleep(1.0)
            await asyncio.to_thread(_startup_scan, config)

        asyncio.create_task(initial_scan())

    try:
        yield
    finally:
        log.info("Shutting down")
        watcher.stop()
        queue.stop()
        stop_db_logging()


def _startup_scan(config: Config) -> None:
    try:
        with session_scope() as session:
            library_service.scan_all(session, config)
    except Exception:
        log.exception("Startup scan failed")


def create_app(config: Config | None = None) -> FastAPI:
    config = config or load_config()

    app = FastAPI(
        title="Prudify",
        version=__version__,
        description="Self-hosted profanity filtering for audiobook libraries.",
        docs_url=f"{config.server.url_base}/api/docs",
        openapi_url=f"{config.server.url_base}/api/openapi.json",
        lifespan=lifespan,
    )
    app.state.config = config

    if config.server.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=config.server.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    api = APIRouter(prefix=f"{config.server.url_base}/api/v1", dependencies=[Depends(require_api_key)])
    api.include_router(routes_books.router)
    api.include_router(routes_jobs.router)
    api.include_router(routes_settings.router)
    api.include_router(routes_system.router)
    app.include_router(api)

    # Unauthenticated liveness probe for Docker / k8s.
    @app.get(f"{config.server.url_base}/ping", include_in_schema=False)
    def ping() -> JSONResponse:
        return JSONResponse({"status": "ok", "version": __version__})

    _mount_frontend(app, config)
    return app


def _mount_frontend(app: FastAPI, config: Config) -> None:
    """Serve the built SPA, falling back to a setup notice if it is absent."""
    base = config.server.url_base
    index = STATIC_DIR / "index.html"

    if not index.exists():
        @app.get(base or "/", include_in_schema=False)
        def missing_ui() -> JSONResponse:
            return JSONResponse(
                {
                    "message": (
                        "The web UI has not been built. Run `npm install && npm run build` "
                        "in the frontend/ directory, or use the Docker image."
                    ),
                    "api_docs": f"{base}/api/docs",
                },
                status_code=503,
            )
        return

    assets = STATIC_DIR / "assets"
    if assets.is_dir():
        app.mount(f"{base}/assets", StaticFiles(directory=assets), name="assets")

    @app.get(base or "/", include_in_schema=False)
    def spa_root() -> FileResponse:
        return FileResponse(index)

    @app.get(base + "/{full_path:path}", include_in_schema=False)
    def spa_catch_all(full_path: str):
        # An unmatched API path is a 404, not the app shell -- returning HTML
        # to an API client makes typos maddening to debug.
        if full_path.startswith("api/"):
            return JSONResponse({"detail": "Not found"}, status_code=404)
        # Serve real files when they exist (favicon, manifest); otherwise let
        # the client-side router handle the path.
        candidate = STATIC_DIR / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(index)


app = None  # populated by run()


def run() -> None:
    """Entry point used by `python -m prudify` and the console script."""
    import uvicorn

    config = load_config()
    configure_logging(config.log_level, config.log_path())
    uvicorn.run(
        create_app(config),
        host=config.server.host,
        port=config.server.port,
        log_config=None,
        access_log=False,
    )


if __name__ == "__main__":
    run()
