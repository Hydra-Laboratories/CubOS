"""FastAPI app factory."""

import logging
import hmac
import os
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from cubos_api.config import get_settings
from cubos_api.routers import (
    data,
    deck,
    fluid_states,
    gantry,
    instruments,
    protocol,
    raw,
    runs,
    settings,
    system,
)

FRONTEND_DIST = Path(
    os.environ.get(
        "CUBOS_WEB_DIST",
        Path(__file__).resolve().parents[4] / "apps" / "operator-web" / "dist",
    )
)
logger = logging.getLogger(__name__)


class FrontendStaticFiles(StaticFiles):
    """Serve the operator UI with revalidation forced for the entry page.

    Asset filenames are content-hashed, so browsers may cache them freely,
    but index.html must be revalidated on every load — otherwise a cached
    entry page keeps referencing asset hashes that an in-app update deleted.
    """

    def file_response(self, full_path, stat_result, scope, status_code=200):
        response = super().file_response(full_path, stat_result, scope, status_code)
        if Path(full_path).name == "index.html":
            response.headers["Cache-Control"] = "no-cache"
        return response

# Methods that change server state — these are the ones a malicious page can
# trigger cross-origin (CSRF) and that DNS-rebinding could redirect to CubOS.
_STATE_CHANGING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

# Hosts that are always treated as equivalent to each other, regardless of
# which one the server is configured to bind on.
_LOCALHOST_EQUIVALENTS = {"localhost", "127.0.0.1", "::1", "[::1]"}


def _host_equivalents(host: str) -> set[str]:
    host = host.lower()
    if host in _LOCALHOST_EQUIVALENTS or host in ("0.0.0.0", "[::]"):
        return set(_LOCALHOST_EQUIVALENTS)
    return {host}


def _allowed_netlocs() -> set[str]:
    """Host:port values (and bare hosts) considered same-origin to CubOS."""
    settings = get_settings()
    hosts = _host_equivalents(settings.host)
    netlocs: set[str] = set()
    for host in hosts:
        netlocs.add(host)
        netlocs.add(f"{host}:{settings.port}")
    netlocs.update(h.lower() for h in settings.trusted_hosts)
    return netlocs


async def _origin_host_middleware(request: Request, call_next):
    allowed = _allowed_netlocs()

    # DNS-rebinding guard: the Host header must match the configured
    # host:port (or a localhost equivalent), for every method — not just
    # state-changing ones — since a GET can also read sensitive data.
    host_header = request.headers.get("host", "")
    host_value = host_header.split(",")[0].strip().lower()
    if not host_value or host_value not in allowed:
        return JSONResponse({"detail": "Invalid Host header"}, status_code=400)

    # CSRF guard: state-changing requests must be same-origin. Requests with
    # no Origin/Referer header (curl, native test clients) are allowed
    # through — only browsers reliably send these headers, and they send them
    # precisely on the cross-origin requests we want to block.
    if request.method in _STATE_CHANGING_METHODS:
        source = request.headers.get("origin") or request.headers.get("referer")
        if source:
            netloc = urlsplit(source).netloc.lower()
            if netloc not in allowed:
                return JSONResponse(
                    {"detail": "Cross-origin request blocked"}, status_code=403
                )

        # Browser requests from the served operator UI are already constrained by
        # the same-origin check above. Native clients must authenticate when a
        # per-device token is configured, including callers of legacy motion
        # routes as well as the versioned run API.
        try:
            token = get_settings().resolved_api_token()
        except (OSError, ValueError):
            logger.exception("Unable to load the configured API token")
            return JSONResponse({"detail": "API token is unavailable"}, status_code=503)
        if token is not None and not source:
            authorization = request.headers.get("authorization", "")
            scheme, _, supplied = authorization.partition(" ")
            expected = token.get_secret_value()
            if scheme.lower() != "bearer" or not hmac.compare_digest(supplied, expected):
                return JSONResponse({"detail": "Invalid API token"}, status_code=401)

    return await call_next(request)


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    # Shutdown: disconnect the gantry so the serial port is released cleanly
    session = gantry.current_session()
    if session is not None and session.connected:
        logger.info("Shutting down — disconnecting gantry")
        try:
            session.disconnect()
        except Exception as e:
            logger.warning("Error disconnecting gantry on shutdown: %s", e)
        instruments.reset_manual_instruments()
        gantry.reset_session()


def create_app() -> FastAPI:
    app = FastAPI(title="CubOS API", lifespan=lifespan)
    app.middleware("http")(_origin_host_middleware)
    app.include_router(deck.router)
    app.include_router(data.router)
    app.include_router(gantry.router)
    app.include_router(instruments.router)
    app.include_router(protocol.router)
    app.include_router(raw.router)
    app.include_router(settings.router)
    app.include_router(system.router)
    app.include_router(runs.router)
    app.include_router(fluid_states.router)

    if FRONTEND_DIST.is_dir():
        app.mount(
            "/", FrontendStaticFiles(directory=FRONTEND_DIST, html=True), name="frontend"
        )

    return app
