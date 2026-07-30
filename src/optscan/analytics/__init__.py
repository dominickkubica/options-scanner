"""Pure functions: greeks, implied vol, probability, surface, returns. No I/O.

Nothing in this package reads config, touches the network, or holds state. Every
threshold is an argument with a documented default, so a caller can pass its own from
config and a test can pass whatever it needs to.

Module map:

- greeks       Black-Scholes-Merton pricing and the five greeks, in trader units.
- iv           Solving implied vol from a quote, and refusing to when the quote
               cannot support one.
- probability  Finish, touch, and probability of profit under a lognormal terminal.
- montecarlo   Path simulation, for P50 which has no closed form.
- surface      Term structure across expiries and skew across strikes.
- moves        Expected move, from the ATM straddle and from the model.
- ivrank       IV rank and percentile against our own snapshot history, with a
               confidence rule that refuses to publish on thin history.
- returns      Return on capital, with the margin model stated explicitly.
- liquidity    Whether a quote is worth acting on.
- events       Earnings and ex dividend proximity, and early assignment risk.

Every module's docstring states the assumptions it makes and the direction in which
they are wrong. Read those before trusting a number.
"""

from optscan.analytics.greeks import Greeks, bsm_greeks, bsm_price, time_to_expiry
from optscan.analytics.iv import VolReason, VolResult, contract_implied_vol, implied_vol
from optscan.analytics.ivrank import Confidence, IVRank, iv_rank, iv_rank_from_series
from optscan.analytics.liquidity import LiquidityScore, score_liquidity
from optscan.analytics.moves import ExpectedMove, expected_move
from optscan.analytics.probability import (
    prob_finish_above,
    prob_finish_below,
    prob_of_touch,
    probability_of_profit,
    touch_and_finish,
)
from optscan.analytics.returns import ReturnProfile
from optscan.analytics.surface import Skew, TermStructure, build_skew, build_term_structure

__all__ = [
    "Confidence",
    "ExpectedMove",
    "Greeks",
    "IVRank",
    "LiquidityScore",
    "ReturnProfile",
    "Skew",
    "TermStructure",
    "VolReason",
    "VolResult",
    "bsm_greeks",
    "bsm_price",
    "build_skew",
    "build_term_structure",
    "contract_implied_vol",
    "expected_move",
    "implied_vol",
    "iv_rank",
    "iv_rank_from_series",
    "prob_finish_above",
    "prob_finish_below",
    "prob_of_touch",
    "probability_of_profit",
    "score_liquidity",
    "time_to_expiry",
    "touch_and_finish",
]
