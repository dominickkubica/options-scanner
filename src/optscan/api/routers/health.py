"""Liveness, and the one fact a user most needs to know about the data.

`realtime` is on this payload because it decides how everything else should be read.
The whole dashboard is built on delayed vendor snapshots, and a UI that does not say
so in its chrome is inviting every number on it to be misread as live.
"""

from __future__ import annotations

from fastapi import APIRouter

from optscan import __version__
from optscan.api.deps import SettingsDep
from optscan.api.schemas import HealthOut
from optscan.providers import provider_is_realtime

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthOut)
def health(settings: SettingsDep) -> HealthOut:
    """Whether the service is up, and how its numbers should be read.

    Phase 5 replaced a set of real time provider names with a function. For Tradier the
    answer is not a property of the vendor: sandbox is documented as fifteen minutes
    delayed and production depends on the account's market data entitlement, which no
    response announces. So the question is asked of the settings, which know both, and
    the delay is reported alongside rather than left to be inferred from a false.
    """
    return HealthOut(
        status="ok",
        version=__version__,
        provider=settings.provider,
        realtime=provider_is_realtime(settings),
        delay_minutes=settings.quote_delay_minutes,
        live_enabled=settings.live_enabled,
    )
