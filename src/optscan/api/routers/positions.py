"""Held positions, marked and evaluated.

Read only. Entry stays on the CLI, deliberately: a fill price typed into a browser form
is the one number in this project that cannot be checked against anything, and a
mistyped one silently corrupts every number on this page until somebody notices the
profit looks wrong. The terminal at least makes it obvious that a person typed it.

Alerts are not delivered from here either. A GET that sent notifications would fire
them on every page load and on every refresh, which is exactly how an alerting tool
gets muted. `optscan manage` sends; this endpoint only reports what it would have.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter

from optscan.api.deps import ProviderFactoryDep, ScreenConfigDep, SettingsDep
from optscan.api.schemas import PortfolioOut
from optscan.api.views import portfolio_view
from optscan.jobs.manage import run_manage
from optscan.logging import get_logger
from optscan.providers import ProviderError

router = APIRouter(tags=["positions"])
log = get_logger("optscan.api.positions")


@router.get("/positions", response_model=PortfolioOut)
def positions(
    settings: SettingsDep,
    config: ScreenConfigDep,
    provider_factory: ProviderFactoryDep,
) -> PortfolioOut:
    """Open positions with their profit, greeks, and any conditions that have fired.

    `send_alerts` is false. See the module docstring: a browser refresh must not consume
    a once-only notification.
    """
    provider = None
    try:
        provider = provider_factory()
    except (ProviderError, NotImplementedError, OSError) as error:
        # Degrades rather than fails. Without a provider the positions still mark from
        # stored snapshots and the run says which checks it could not make.
        log.warning("provider unavailable for positions", error=str(error))

    result = run_manage(
        settings,
        config,
        provider=provider,
        send_alerts=False,
        asof=datetime.now(UTC).date(),
    )
    return portfolio_view(result)
