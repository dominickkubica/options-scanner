"""The FastAPI application.

Everything the browser talks to is under /api. The built frontend, when there is one,
is mounted at the root and its index is served for unknown paths so client side routes
survive a refresh. In development there is no build: Vite serves the app on 5173 and
talks to this over CORS, which is the only reason CORS is configured at all.

Phase 5 added one streaming endpoint, /api/live/stream, and it is server sent events
rather than a websocket. The reasoning is in optscan.live.hub: the vendor this project
runs against has no push to forward on its free tier, the traffic is one directional,
and EventSource reconnects on its own. Every other endpoint still answers from stored
snapshots, and the UI labels which is which per panel rather than in aggregate.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from optscan import __version__
from optscan.api.deps import clear_caches, frontend_dist, live_hub, reset_live_hub, settings_dep
from optscan.api.routers import (
    backtest,
    catalogue,
    health,
    ideas,
    journal,
    levels,
    live,
    payoff,
    positions,
    quotes,
    refresh,
    scan,
    signals,
    symbols,
    watchlist,
)
from optscan.config import Settings, get_settings
from optscan.logging import configure_logging, get_logger

log = get_logger("optscan.api")

API_PREFIX = "/api"

#: Where the Vite dev server runs. Both spellings, because a browser sent to
#: 127.0.0.1 does not consider itself to be at localhost and the CORS check is exact.
DEV_ORIGINS = (
    "http://localhost:5173",
    "http://127.0.0.1:5173",
)

DESCRIPTION = """
Ranks and displays option selling candidates. It never places orders.

Every payload carrying market data also carries when that data was fetched and whether
that makes it stale. A number that cannot be computed honestly is serialized as null
with a stated reason, never as zero.

Market data is read from stored snapshots, not live quotes. The corporate calendar and
the daily candles are the two exceptions, and both say so when they are unavailable.
""".strip()


#: Methods that change something. A browser sends an Origin header on every one of these
#: when the page making the request is on another origin.
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: `Sec-Fetch-Site` values a write may carry when there is no Origin to check.
SAFE_FETCH_SITES = frozenset({"same-origin", "none"})


def _same_origin(origin: str, request: Request) -> bool:
    parsed = urlsplit(origin)
    host = request.headers.get("host", "")
    return (
        parsed.scheme in ("http", "https") and bool(host) and parsed.netloc.lower() == host.lower()
    )


async def refuse_cross_site_writes(request: Request, call_next):
    """Refuse a state-changing request that another site's page sent.

    CORS decides whether a browser hands a cross-origin *response* back to the page that
    asked. It does not stop the request running. Measured 2026-09-12: a POST to
    /api/watchlist/ZZZZ carrying `Origin: https://evil.example` returned 200 and pinned
    the symbol, and a `text/plain` POST to /api/journal/import would have written forged
    rows into the broker ledger, which has no delete path. So a write is accepted only
    from this app's own origin, from the Vite dev server, or from a client that is not a
    web page at all: the CLI, curl and the tests send no Origin.

    This is not a DNS rebinding defence and must not be read as one. Under rebinding the
    browser believes the attacker's page is same origin, so the Origin matches the Host;
    only a check on the Host name itself stops that, and this does not make one.
    """
    if request.method in UNSAFE_METHODS:
        origin = request.headers.get("origin")
        site = request.headers.get("sec-fetch-site")
        if origin is not None:
            refused = origin not in DEV_ORIGINS and not _same_origin(origin, request)
        else:
            refused = site is not None and site not in SAFE_FETCH_SITES
        if refused:
            log.warning("refused a cross-site write", path=request.url.path, origin=origin)
            return JSONResponse(
                status_code=403,
                content={"detail": "Refused: this change came from another site's page."},
            )
    return await call_next(request)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = get_settings()
    configure_logging(settings)
    clear_caches()
    dist = frontend_dist()
    log.info(
        "api starting",
        version=__version__,
        provider=settings.provider,
        data_path=str(settings.data_path),
        frontend="built" if dist else "dev (serve it with vite)",
        live=settings.live_enabled,
    )
    # A no-op unless live_enabled, and the hub itself enforces that rather than this
    # caller, so there is one place that decides whether a poller may exist.
    live_hub(settings).start()
    yield
    reset_live_hub()
    clear_caches()


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the app.

    A factory rather than a module level singleton so a test can construct one against
    a temporary data directory. Passing settings registers a dependency override, so
    the whole request path sees them rather than only whatever reads this variable.
    """
    app = FastAPI(
        title="optscan",
        version=__version__,
        description=DESCRIPTION,
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(DEV_ORIGINS),
        allow_credentials=False,
        # PUT and DELETE too: the frontend uses both, and without them a direct call
        # from the dev origin was refused on its preflight.
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["*"],
    )
    app.middleware("http")(refuse_cross_site_writes)

    for router in (
        health.router,
        backtest.router,
        ideas.router,
        watchlist.router,
        quotes.router,
        refresh.router,
        catalogue.router,
        symbols.router,
        scan.router,
        payoff.router,
        live.router,
        levels.router,
        positions.router,
        journal.router,
        signals.router,
    ):
        app.include_router(router, prefix=API_PREFIX)

    if settings is not None:
        app.dependency_overrides[settings_dep] = lambda: settings

    _mount_frontend(app)
    return app


def _mount_frontend(app: FastAPI) -> None:
    """Serve the built single page app, if it has been built.

    Absent in development and that is the normal state, so it is logged at info and
    not warned about. A warning on every dev server start is a warning nobody reads.
    """
    dist = frontend_dist()
    if dist is None:
        return

    assets = dist / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    index = dist / "index.html"

    # The page shell must be revalidated on every load. Without a Cache-Control header
    # browsers cache it heuristically, and on 2026-09-12 a rebuilt frontend stayed
    # invisible behind yesterday's index.html, which pointed at yesterday's bundle. The
    # bundles themselves are named by content hash, so they can be cached freely.
    shell_headers = {"Cache-Control": "no-cache"}

    @app.get("/", include_in_schema=False)
    def _index() -> FileResponse:
        return FileResponse(index, headers=shell_headers)

    @app.get("/{path:path}", include_in_schema=False)
    def _spa(path: str) -> FileResponse:
        """Client side routes fall back to the index so a refresh does not 404.

        A real file under dist still wins, which is what serves favicon.ico and any
        other root level asset Vite emits.
        """
        candidate = (dist / path).resolve()
        if candidate.is_file() and _inside(candidate, dist):
            return FileResponse(candidate)
        return FileResponse(index, headers=shell_headers)


def _inside(candidate: Path, root: Path) -> bool:
    """Whether a resolved path is under root. Stops ../ from escaping dist."""
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return False
    return True


app = create_app()
