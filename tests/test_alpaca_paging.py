"""A paged bar response keeps every page (BUG-0002).

The bars endpoint answers `{"bars": {"QQQ": [...]}}` on every page, each with a different
slice of rows under the same symbol. The old merge was a plain dict merge, so each page
replaced the one before it and only the last survived: 1000 + 258 rows came back as 258,
with no error, and every indicator drawn over the result was drawn over a hole.

The adapter's other tests all set `next_page_token` to None, which is why nothing caught
it. These drive the paging loop past one page.
"""

from __future__ import annotations

from datetime import date, timedelta

import httpx

from optscan.providers.alpaca import AlpacaProvider
from tests.test_provider_alpaca import build

LAST = date(2026, 9, 4)
TOTAL = 1258


def rows(first: int, count: int) -> list[dict]:
    """Consecutive daily bars ending on LAST, so every one is inside the window asked for."""
    out = []
    for index in range(first, first + count):
        day = LAST - timedelta(days=TOTAL - 1 - index)
        out.append(
            {
                "t": f"{day.isoformat()}T04:00:00Z",
                "o": 100.0,
                "h": 101.0,
                "l": 99.0,
                "c": 100.5,
                "v": 1000,
            }
        )
    return out


def test_a_two_page_history_keeps_both_pages() -> None:
    pages = {
        None: {"bars": {"QQQ": rows(0, 1000)}, "next_page_token": "page-2"},
        "page-2": {"bars": {"QQQ": rows(1000, TOTAL - 1000)}, "next_page_token": None},
    }
    asked: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        token = request.url.params.get("page_token")
        asked.append(token)
        return httpx.Response(200, json=pages[token])

    bars = build(handler).get_history("QQQ", 1400)

    assert asked == [None, "page-2"]
    assert len(bars) == TOTAL, "a page was dropped"


def test_merging_extends_repeated_lists_and_leaves_unique_keys_alone() -> None:
    """Bars repeat their key across pages; chain snapshots are keyed by unique contract."""
    merged = AlpacaProvider._merge_page(
        {"QQQ": [1, 2], "QQQ260918P00700000": {"bid": 1.0}},
        {"QQQ": [3], "SPY": [4], "QQQ260918P00700000": {"bid": 1.1}},
    )
    assert merged == {"QQQ": [1, 2, 3], "SPY": [4], "QQQ260918P00700000": {"bid": 1.1}}
