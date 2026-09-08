"""Coercing vendor JSON into numbers, once, for every adapter.

Three adapters had their own copy of this and the copies disagreed, which is worse
than the duplication. Tradier and yfinance preserved a zero, Alpaca collapsed it to
None, and nothing said so: the difference only showed up as a chain with fewer bids
in it.

## The rule these exist to enforce

**None means unknown, 0.0 means the vendor said zero.** It is the first convention in
this project's CLAUDE.md and it is easiest to break exactly here, at the boundary where
a vendor's `null`, its `0`, and its `NaN` all arrive looking similar.

The two functions are the two honest answers, and which one a field takes is a real
decision rather than a style choice:

- `non_negative` for a **quote**. A 0.00 bid is a real, common market state: measured
  on one SPY expiry, 57 of 642 contracts had a 0.00 bid against a real ask, and none
  had both sides zero. Collapsing those to None throws away the bid on every far
  out of the money wing and makes the chain look thinner than it is.
- `positive` for a **derived or implied** value. A zero implied volatility is not a
  measurement, it is a solver that gave up, and Tradier was measured publishing exactly
  that: 0.0 on deep in the money calls and 10.0 on deep in the money puts, both clamps
  dressed as numbers.

Getting this backwards is quiet in both directions. Use `positive` on a bid and the
chain silently loses its wings; use `non_negative` on an implied vol and a clamp
becomes a data point.
"""

from __future__ import annotations

import math
from typing import Any


def _as_float(value: Any) -> float | None:
    """A float, or None for anything that is not one. NaN is not one."""
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(number) else number


def non_negative(value: Any) -> float | None:
    """A quote. Zero is kept because a zero bid is a real state; negatives are not."""
    number = _as_float(value)
    if number is None or number < 0:
        return None
    return number


def positive(value: Any) -> float | None:
    """A derived value. Zero means the vendor had nothing, not that the answer is zero."""
    number = _as_float(value)
    if number is None or number <= 0:
        return None
    return number


def whole(value: Any) -> int | None:
    """A count: size, volume, open interest. Zero is kept; it is a real count."""
    number = _as_float(value)
    if number is None or number < 0:
        return None
    return round(number)
