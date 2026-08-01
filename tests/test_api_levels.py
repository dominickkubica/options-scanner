"""The levels endpoint: one request, one chart's worth of context.

Offline like the rest of the API suite. The fake provider serves the real captured SPY
bars, so the levels in the response are computed from genuine price action rather than
from a synthetic series that would agree with whatever the detectors do.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from optscan.api.app import create_app
from optscan.api.deps import clear_caches, provider_factory_dep
from optscan.config import Settings
from optscan.models import ChainSnapshot, PriceBar
from optscan.storage import write_snapshot
from tests.conftest import FakeProvider

BARS_FIXTURE = Path(__file__).parent / "fixtures" / "spy_daily_bars.json"


@pytest.fixture(autouse=True)
def _clear() -> Iterator[None]:
    clear_caches()
    yield
    clear_caches()


@pytest.fixture(scope="session")
def real_bars() -> list[PriceBar]:
    payload = json.loads(BARS_FIXTURE.read_text(encoding="utf-8"))
    return [
        PriceBar(
            symbol=payload["symbol"],
            ts=datetime.fromisoformat(row["ts"]),
            open=row["open"],
            high=row["high"],
            low=row["low"],
            close=row["close"],
            volume=row["volume"],
            fetched_at=datetime.fromisoformat(payload["captured_at"]),
            source=payload["source"],
        )
        for row in payload["bars"]
    ]


class BarProvider(FakeProvider):
    """The frozen chain plus the real captured daily bars."""

    def __init__(self, snapshot: ChainSnapshot, bars: list[PriceBar]) -> None:
        super().__init__(snapshot)
        self._bars = bars
        self.history_error: Exception | None = None

    def get_history(self, symbol: str, days: int) -> list[PriceBar]:
        self.calls.append(("get_history", symbol))
        if self.history_error is not None:
            raise self.history_error
        return list(self._bars)


@pytest.fixture
def settings(tmp_settings: Settings, frozen_snapshot: ChainSnapshot) -> Settings:
    tmp_settings.ensure_dirs()
    write_snapshot(frozen_snapshot, tmp_settings.snapshot_path)
    return tmp_settings


@pytest.fixture
def provider(frozen_snapshot: ChainSnapshot, real_bars: list[PriceBar]) -> BarProvider:
    return BarProvider(frozen_snapshot, real_bars)


def client(settings: Settings, provider: BarProvider) -> TestClient:
    app = create_app(settings)
    app.dependency_overrides[provider_factory_dep] = lambda: lambda: provider
    return TestClient(app)


def fetch(settings: Settings, provider: BarProvider, query: str = "") -> dict:
    response = client(settings, provider).get(f"/api/symbols/SPY/levels{query}")
    assert response.status_code == 200, response.text
    return response.json()


class TestPayload:
    def test_it_answers_with_the_whole_chart(
        self, settings: Settings, provider: BarProvider
    ) -> None:
        """One request, because assembling this picture from four would let the browser
        draw four different moments on one set of axes."""
        body = fetch(settings, provider)

        assert body["symbol"] == "SPY"
        assert body["bars"]
        assert body["levels"]
        assert body["cone"]
        assert body["moving_averages"]

    def test_both_ages_are_reported_separately(
        self, settings: Settings, provider: BarProvider
    ) -> None:
        """The bars are fetched now and the volatilities come from the last capture.
        Those are different ages, and this is the one panel that draws both."""
        body = fetch(settings, provider)

        assert body["bars_provenance"] is not None
        assert body["chain_provenance"] is not None
        assert body["bars_provenance"]["source"] == "yfinance"

    def test_levels_carry_the_evidence_behind_them(
        self, settings: Settings, provider: BarProvider
    ) -> None:
        """A bare touch count reads as far more evidence than it is, so the expected
        count travels with it."""
        body = fetch(settings, provider)
        swing = [item for item in body["levels"] if item["kind"].startswith("swing")]

        for level in swing:
            assert level["p_value"] is not None
            assert level["expected_touches"] is not None
            assert level["touches"] > level["expected_touches"]

    def test_a_round_number_admits_it_measured_nothing(
        self, settings: Settings, provider: BarProvider
    ) -> None:
        body = fetch(settings, provider)
        rounds = [item for item in body["levels"] if item["kind"] == "round_number"]

        assert rounds
        assert all(item["p_value"] is None for item in rounds)

    def test_it_says_how_many_candidates_were_rejected(
        self, settings: Settings, provider: BarProvider
    ) -> None:
        """A short level list has to read as a finding rather than a broken job."""
        body = fetch(settings, provider)
        swing = [item for item in body["levels"] if item["kind"].startswith("swing")]

        assert body["swing_candidates"] > len(swing)
        assert any("swing candidates" in note for note in body["notes"])

    def test_the_chart_is_not_a_hairball(self, settings: Settings, provider: BarProvider) -> None:
        """The constraint every filter in levels.py is aimed at."""
        assert len(fetch(settings, provider)["levels"]) <= 20


class TestCone:
    def test_each_point_carries_its_own_volatility(
        self, settings: Settings, provider: BarProvider
    ) -> None:
        body = fetch(settings, provider)

        assert body["cone"]
        for point in body["cone"]:
            assert point["sigma"] > 0
            assert point["bands"]
            for band in point["bands"]:
                assert band["low"] < body["spot"] < band["high"]

    def test_bands_widen_with_deviations(self, settings: Settings, provider: BarProvider) -> None:
        point = fetch(settings, provider)["cone"][0]
        bands = sorted(point["bands"], key=lambda item: item["deviations"])

        assert len(bands) >= 2
        assert bands[1]["low"] < bands[0]["low"]
        assert bands[1]["high"] > bands[0]["high"]


class TestVarianceRiskPremium:
    def test_the_comparison_is_refused_across_mismatched_tenors(
        self, settings: Settings, provider: BarProvider
    ) -> None:
        """The frozen chain is 4 and 8 DTE against a 30 day realized vol. Comparing
        those is the term structure talking, not the variance risk premium, so the
        endpoint declines to publish a number rather than publishing a wrong one."""
        body = fetch(settings, provider)

        assert body["realized_vol"] is not None
        assert body["implied_vol"] is None
        assert body["variance_risk_premium"] is None


class TestDegradation:
    def test_a_failed_candle_fetch_costs_the_levels_and_nothing_else(
        self, settings: Settings, provider: BarProvider
    ) -> None:
        """The cone and the strikes come from the stored capture, so they still render.
        An honest partial beats an error page."""
        from optscan.providers.errors import ProviderUnavailable

        provider.history_error = ProviderUnavailable("vendor is down")
        body = fetch(settings, provider)

        assert body["bars"] == []
        assert body["levels"] == []
        assert body["cone"], "the cone comes from the stored chain and must survive"
        assert any("unavailable" in note.lower() for note in body["notes"])

    def test_an_unknown_symbol_says_how_to_fix_it(
        self, settings: Settings, provider: BarProvider
    ) -> None:
        response = client(settings, provider).get("/api/symbols/NVDA/levels")

        assert response.status_code == 404
        assert "optscan snapshot" in response.json()["detail"]

    def test_an_expiry_that_was_never_captured_is_refused(
        self, settings: Settings, provider: BarProvider
    ) -> None:
        """Quietly projecting a neighbour's volatility under the requested date is
        wrong in a way nobody would catch by looking at the chart."""
        response = client(settings, provider).get("/api/symbols/SPY/levels?expiry=2099-01-15")

        assert response.status_code == 404
        assert "no captured chain" in response.json()["detail"]


class TestDistribution:
    def test_the_terminal_distribution_sums_to_one(
        self, settings: Settings, provider: BarProvider
    ) -> None:
        body = fetch(settings, provider)
        distribution = body["distribution"]

        assert distribution is not None
        total = sum(item["probability"] for item in distribution["bins"])
        assert total == pytest.approx(1.0)

    def test_it_is_drawn_for_the_chosen_expiry(
        self, settings: Settings, provider: BarProvider
    ) -> None:
        listed = fetch(settings, provider)
        chosen = listed["distribution"]["expiry"]
        explicit = fetch(settings, provider, f"?expiry={chosen}")

        assert explicit["distribution"]["expiry"] == chosen

    def test_the_median_follows_the_risk_neutral_drift(
        self, settings: Settings, provider: BarProvider
    ) -> None:
        """Median terminal price is spot * exp((r - sigma^2/2) * t), and on a short
        dated index that lands slightly *above* spot rather than below.

        Worth pinning, because the sign is not obvious and is easy to assume wrong.
        The lognormal median sits below the mean, which tempts you to expect it below
        spot too; that only holds when the carry is zero. Here the endpoint projects
        under the configured 4.3 percent rate, and for SPY at roughly 14 percent vol
        the carry term beats half the variance, so the median drifts up.
        """
        import math

        body = fetch(settings, provider)
        distribution = body["distribution"]

        time = distribution["dte"] / 365.0
        sigma = distribution["sigma"]
        drift = settings.risk_free_rate - 0.5 * sigma * sigma
        expected = body["spot"] * math.exp(drift * time)

        assert distribution["median"] == pytest.approx(expected, rel=0.01)
        assert drift > 0, "this fixture is the case where carry beats half the variance"
        assert distribution["median"] > body["spot"]


class TestWindow:
    def test_a_shorter_window_uses_fewer_sessions(
        self, settings: Settings, provider: BarProvider
    ) -> None:
        """The provider ignores `days` and returns everything, so this checks the
        request is accepted and the session count is reported, not that the vendor
        honoured it."""
        body = fetch(settings, provider, "?days=90")
        assert body["sessions"] > 0

    def test_an_absurd_window_is_rejected_by_the_schema(
        self, settings: Settings, provider: BarProvider
    ) -> None:
        response = client(settings, provider).get("/api/symbols/SPY/levels?days=99999")
        assert response.status_code == 422
