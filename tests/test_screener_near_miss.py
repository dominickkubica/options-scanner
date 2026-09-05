"""Near misses: the candidates one gate away, and the countdown on the DTE ones.

Two things here are worth more than the rest. `evaluate_all` has to report every gate a
candidate failed rather than the first, because "blocked by one thing" is the entire
definition of a near miss and a short circuiting check cannot distinguish one blocker
from four. And the countdown has to be the right way round: it is displayed as "enters
the screen in Nd", so a sign error there would quietly advertise contracts as becoming
eligible in the past, on a panel whose whole purpose is telling the reader when to look
again.
"""

from __future__ import annotations

import pytest

from optscan.api.views import near_miss_view
from optscan.models import ChainSnapshot
from optscan.screener.config import ScreenConfig
from optscan.screener.context import analyze_snapshot
from optscan.screener.filters import Rejection, evaluate, evaluate_all
from optscan.screener.scan import NEAR_MISSES_PER_BLOCKER, scan_analysis
from optscan.screener.strategies import generators_for

RATE = 0.043


@pytest.fixture
def analysis(frozen_snapshot: ChainSnapshot):
    return analyze_snapshot(frozen_snapshot, rate=RATE)


def _config(**dte) -> ScreenConfig:
    """The frozen chain is 4 and 8 days out, so the DTE window is the lever here."""
    return ScreenConfig.model_validate(
        {
            "filters": {
                "dte": {"min_dte": dte.get("min_dte", 0), "max_dte": dte.get("max_dte", 60)},
                "premium": {"min_annualized_return": 0.0},
            }
        }
    )


def _candidates(analysis, config):
    for expiry in analysis.expiries:
        for generator in generators_for(config.strategies.enabled):
            for candidate in generator.generate(analysis, expiry, config):
                yield candidate, expiry


class TestEvaluateAll:
    def test_a_passing_candidate_has_no_blockers(self, analysis) -> None:
        config = _config()
        passing = [
            (candidate, expiry)
            for candidate, expiry in _candidates(analysis, config)
            if evaluate(candidate, analysis, expiry, config)
        ]
        assert passing, "fixture produced nothing that passes, test proves nothing"
        for candidate, expiry in passing:
            assert evaluate_all(candidate, analysis, expiry, config) == []

    def test_reports_every_failing_group_not_just_the_first(self, analysis) -> None:
        """The whole reason this function exists rather than reusing `evaluate`."""
        config = _config()
        multi = [
            reasons
            for candidate, expiry in _candidates(analysis, config)
            if len(reasons := evaluate_all(candidate, analysis, expiry, config)) > 1
        ]
        assert multi, "no candidate failed more than one gate, cannot tell the two apart"

    def test_first_blocker_agrees_with_evaluate(self, analysis) -> None:
        """Both run the same checks in the same order, so they must not disagree."""
        config = _config()
        for candidate, expiry in _candidates(analysis, config):
            verdict = evaluate(candidate, analysis, expiry, config)
            reasons = evaluate_all(candidate, analysis, expiry, config)
            if verdict:
                assert reasons == []
            else:
                assert reasons and reasons[0] is verdict.reason


class TestCollection:
    def test_off_by_default(self, analysis) -> None:
        """Pure cost to a caller that only wants the ranked list."""
        assert scan_analysis(analysis, _config()).near_misses == []

    def test_every_near_miss_is_blocked_by_exactly_one_gate(self, analysis) -> None:
        config = _config(max_dte=2)
        result = scan_analysis(analysis, config, collect_near_misses=True)
        assert result.near_misses
        for item in result.near_misses:
            assert isinstance(item.blocker, Rejection)

    def test_near_misses_never_leak_into_opportunities(self, analysis) -> None:
        """The list a caller sizes positions from must contain nothing the screen rejected."""
        config = _config(max_dte=2)
        result = scan_analysis(analysis, config, collect_near_misses=True)
        passing = {id(item) for item in result.opportunities}
        assert passing.isdisjoint({id(item.opportunity) for item in result.near_misses})

    def test_expiring_candidates_are_not_listed_as_upcoming(self, analysis) -> None:
        """A contract under the DTE floor is leaving the window, not approaching it.

        The frozen chain is 4 and 8 days out, so the default 21 day floor rejects all
        of it. None of that belongs on a panel about what qualifies next.
        """
        config = _config(min_dte=21, max_dte=60)
        result = scan_analysis(analysis, config, collect_near_misses=True)
        blockers = {item.blocker for item in result.near_misses}
        assert Rejection.DTE_TOO_SHORT not in blockers

    def test_no_single_gate_can_starve_the_others(self, analysis) -> None:
        config = _config(max_dte=2)
        result = scan_analysis(analysis, config, collect_near_misses=True)
        result.rank(50)
        counts: dict[Rejection, int] = {}
        for item in result.near_misses:
            counts[item.blocker] = counts.get(item.blocker, 0) + 1
        assert counts
        for blocker, seen in counts.items():
            assert seen <= NEAR_MISSES_PER_BLOCKER, blocker


class TestCountdown:
    """`enters_screen_in_days` is calendar arithmetic, and it has a direction."""

    def test_counts_forward_from_the_ceiling(self, analysis) -> None:
        max_dte = 2
        config = _config(max_dte=max_dte)
        result = scan_analysis(analysis, config, collect_near_misses=True)
        dte_blocked = [
            item for item in result.near_misses if item.blocker is Rejection.DTE_TOO_LONG
        ]
        assert dte_blocked, "fixture produced nothing over the ceiling"

        for item in dte_blocked:
            view = near_miss_view(item, max_dte)
            assert view.enters_screen_in_days == item.opportunity.dte - max_dte
            # The contract is over the ceiling, so eligibility is strictly ahead of it.
            assert view.enters_screen_in_days > 0

    def test_no_countdown_on_a_gate_the_calendar_cannot_clear(self, analysis) -> None:
        """Only the DTE ceiling clears by waiting. Dating the others would be invention.

        The DTE window is left wide on purpose. Narrowing it to force DTE blockers
        makes every candidate fail that gate too, which takes them over the one blocker
        limit and empties the very list this test needs.
        """
        config = _config()
        result = scan_analysis(analysis, config, collect_near_misses=True)
        others = [item for item in result.near_misses if item.blocker is not Rejection.DTE_TOO_LONG]
        assert others, "fixture produced no non-DTE blockers"
        for item in others:
            assert near_miss_view(item, config.filters.dte.max_dte).enters_screen_in_days is None
