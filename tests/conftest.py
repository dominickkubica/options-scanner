"""Shared fixtures.

Settings are read from the process environment, so tests that care about config must
isolate themselves from whatever is in the developer's .env.

Nothing in the suite touches the network. The frozen SPY snapshot in fixtures/ is a
real chain captured on 30 July 2026, which makes the storage and job tests exercise
real data shapes (wide strike ladders, zero bids, missing volume) while staying
deterministic and offline.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from optscan.config import Settings, get_settings
from optscan.models import ChainSnapshot, OptionChain, PriceBar, Quote
from optscan.providers.base import MarketDataProvider

FIXTURE_DIR = Path(__file__).parent / "fixtures"
FROZEN_CHAIN = FIXTURE_DIR / "spy_chain_snapshot.json"


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> Iterator[None]:
    """Keep the settings singleton from leaking between tests."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every OPTSCAN_ variable so defaults are what is under test."""
    for key in list(os.environ):
        if key.startswith("OPTSCAN_"):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture
def tmp_settings(tmp_path: Path, clean_env: None) -> Settings:
    """Settings pointed at a throwaway data directory, ignoring any local .env."""
    return Settings(_env_file=None, data_dir=tmp_path)


@pytest.fixture(scope="session")
def frozen_snapshot_json() -> dict:
    return json.loads(FROZEN_CHAIN.read_text(encoding="utf-8"))


@pytest.fixture
def frozen_snapshot(frozen_snapshot_json: dict) -> ChainSnapshot:
    """A real two expiry SPY capture, parsed back into models."""
    return ChainSnapshot.model_validate(frozen_snapshot_json)


@pytest.fixture
def future_snapshot(frozen_snapshot: ChainSnapshot) -> ChainSnapshot:
    """The frozen capture, moved forward so its expiries are still ahead of today.

    The fixture is a real chain from 30 July 2026 whose expiries are days out, so it
    rots by the calendar: any code path that filters expiries to the future stops
    finding anything in it within a fortnight of the capture. Tests that read stored
    snapshots never noticed, which is why only the live capture path broke, and it
    broke on a date rather than on a change.

    Use this fixture wherever the code under test asks "which expiries are still
    tradeable", and the plain `frozen_snapshot` everywhere else. Shifting the dates
    would change every DTE and therefore every score, which is exactly what the pinned
    ranking tests exist to catch, so the shift lives here rather than in the file.

    Whole weeks, so expiries keep their weekday. Contracts move with their chain
    because the model checks that the two agree.
    """
    today = date.today()
    earliest = min(frozen_snapshot.expiries)
    lead = timedelta(days=14)
    if earliest - today >= lead:
        return frozen_snapshot

    weeks = -(-((today + lead) - earliest).days // 7)
    shift = timedelta(weeks=weeks)

    chains = tuple(
        chain.model_copy(
            update={
                "expiry": chain.expiry + shift,
                "contracts": tuple(
                    contract.model_copy(update={"expiry": contract.expiry + shift})
                    for contract in chain.contracts
                ),
            }
        )
        for chain in frozen_snapshot.chains
    )
    return frozen_snapshot.model_copy(update={"chains": chains, "session_date": today})


class FakeProvider(MarketDataProvider):
    """In memory provider driven by whatever the test hands it."""

    name = "fake"
    realtime = False

    def __init__(
        self,
        snapshot: ChainSnapshot | None = None,
        *,
        fail_symbols: set[str] | None = None,
        fail_expiries: set[date] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.snapshot = snapshot
        self.fail_symbols = fail_symbols or set()
        self.fail_expiries = fail_expiries or set()
        self.error = error
        self.calls: list[tuple[str, str]] = []

    def _guard(self, symbol: str) -> None:
        if symbol in self.fail_symbols:
            from optscan.providers.errors import SymbolNotFound

            raise SymbolNotFound(f"fake provider was told to fail on {symbol}")

    def get_quote(self, symbol: str) -> Quote:
        self.calls.append(("get_quote", symbol))
        self._guard(symbol)
        assert self.snapshot is not None
        return self.snapshot.quote.model_copy(update={"symbol": symbol})

    def get_expirations(self, symbol: str) -> list[date]:
        self.calls.append(("get_expirations", symbol))
        self._guard(symbol)
        assert self.snapshot is not None
        return list(self.snapshot.expiries)

    def get_chain(self, symbol: str, expiry: date) -> OptionChain:
        self.calls.append(("get_chain", f"{symbol}:{expiry}"))
        self._guard(symbol)
        if expiry in self.fail_expiries:
            from optscan.providers.errors import ProviderUnavailable

            raise ProviderUnavailable(f"fake provider was told to fail on {expiry}")
        assert self.snapshot is not None
        for chain in self.snapshot.chains:
            if chain.expiry == expiry:
                return chain.model_copy(
                    update={
                        "symbol": symbol,
                        "contracts": tuple(
                            c.model_copy(update={"symbol": symbol}) for c in chain.contracts
                        ),
                    }
                )
        from optscan.providers.errors import NoDataAvailable

        raise NoDataAvailable(f"no fake chain for {expiry}")

    def get_history(self, symbol: str, days: int) -> list[PriceBar]:
        self.calls.append(("get_history", symbol))
        self._guard(symbol)
        now = datetime.now(UTC)
        return [
            PriceBar(
                symbol=symbol,
                ts=now,
                open=100.0,
                high=101.0,
                low=99.0,
                close=100.5,
                volume=1_000,
                fetched_at=now,
                source=self.name,
            )
        ]


@pytest.fixture
def fake_provider(frozen_snapshot: ChainSnapshot) -> FakeProvider:
    return FakeProvider(frozen_snapshot)
