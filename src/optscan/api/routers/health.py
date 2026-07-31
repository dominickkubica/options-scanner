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

router = APIRouter(tags=["health"])

#: Providers that quote in real time. yfinance does not, and pretending otherwise in
#: a dashboard header is exactly the kind of small lie this project refuses.
REALTIME_PROVIDERS: frozenset[str] = frozenset()


@router.get("/health", response_model=HealthOut)
def health(settings: SettingsDep) -> HealthOut:
    return HealthOut(
        status="ok",
        version=__version__,
        provider=settings.provider,
        realtime=settings.provider in REALTIME_PROVIDERS,
    )
