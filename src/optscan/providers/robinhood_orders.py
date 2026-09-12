"""Robinhood's order history, which carries the fill times the CSV export leaves out.

Reached through `robin_stocks`, an unofficial client. Two rules hold here:

**The trader logs in, in their own terminal.** `robin_stocks` prompts for the username
with `input()` and the password with `getpass`, then runs Robinhood's own device
approval or code challenge. Nothing here takes, stores, prints or logs a credential, and
`store_session=False` means no token is written to disk either: the session ends when
the command does, and `logout` runs even if the fetch fails.

**Only order history is read.** No order is placed, changed or cancelled, and nothing
about the account is written back. The one thing taken away is when each fill happened.

Unofficial means it can break when Robinhood changes something, and the response shapes
are parsed defensively for that reason: a leg missing its contract details is looked up
from its instrument URL, and an order with no executions is skipped rather than failing
the whole import.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator
from datetime import date, datetime

from optscan.imports.fill_times import MARKET_TZ, ExecutedFill
from optscan.models.broker import Effect
from optscan.models.enums import Right
from optscan.models.opportunity import Action


class RobinhoodUnavailable(RuntimeError):
    """robin_stocks is not installed, or the login did not complete."""


def _moment(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _grouped(executions: Iterable[dict]) -> Iterator[tuple[float, float, datetime]]:
    """(quantity, average price, first execution) per Eastern trading day."""
    days: dict[date, list[tuple[datetime, float, float]]] = defaultdict(list)
    for execution in executions:
        try:
            moment = _moment(execution["timestamp"])
            quantity = float(execution["quantity"])
            price = float(execution["price"])
        except (KeyError, TypeError, ValueError):
            continue
        if quantity > 0:
            days[moment.astimezone(MARKET_TZ).date()].append((moment, quantity, price))
    for items in days.values():
        quantity = sum(q for _, q, _ in items)
        price = sum(q * p for _, q, p in items) / quantity
        yield quantity, price, min(m for m, _, _ in items)


def parse_option_orders(
    orders: Iterable[dict], instrument: Callable[[str], dict]
) -> list[ExecutedFill]:
    """Executed option legs. `instrument` fetches a leg's contract from its URL."""
    fills = []
    for order in orders:
        symbol = order.get("chain_symbol")
        for leg in order.get("legs") or []:
            executions = leg.get("executions") or []
            if not executions:
                continue
            expiry = leg.get("expiration_date")
            strike = leg.get("strike_price")
            kind = leg.get("option_type")
            leg_symbol = symbol
            if not (expiry and strike and kind and leg_symbol) and leg.get("option"):
                details = instrument(leg["option"]) or {}
                expiry = expiry or details.get("expiration_date")
                strike = strike or details.get("strike_price")
                kind = kind or details.get("type")
                leg_symbol = leg_symbol or details.get("chain_symbol")
            if not (expiry and strike and kind and leg_symbol):
                continue
            for quantity, price, first in _grouped(executions):
                fills.append(
                    ExecutedFill(
                        symbol=str(leg_symbol).upper(),
                        expiry=date.fromisoformat(str(expiry)[:10]),
                        right=Right.CALL if str(kind).lower().startswith("c") else Right.PUT,
                        strike=float(strike),
                        action=Action.BUY if leg.get("side") == "buy" else Action.SELL,
                        effect=Effect.OPEN
                        if leg.get("position_effect") == "open"
                        else Effect.CLOSE,
                        quantity=quantity,
                        price=price,
                        executed_at=first,
                        order_id=str(order.get("id", "")),
                    )
                )
    return fills


def parse_stock_orders(orders: Iterable[dict], symbol: Callable[[str], str]) -> list[ExecutedFill]:
    """Executed share orders. `symbol` resolves an instrument URL to its ticker."""
    fills = []
    for order in orders:
        executions = order.get("executions") or []
        if not executions or not order.get("instrument"):
            continue
        ticker = symbol(order["instrument"])
        if not ticker:
            continue
        for quantity, price, first in _grouped(executions):
            fills.append(
                ExecutedFill(
                    symbol=ticker.upper(),
                    expiry=None,
                    right=None,
                    strike=None,
                    action=Action.BUY if order.get("side") == "buy" else Action.SELL,
                    effect=None,
                    quantity=quantity,
                    price=price,
                    executed_at=first,
                    order_id=str(order.get("id", "")),
                )
            )
    return fills


def fetch_fills() -> list[ExecutedFill]:
    """Log in interactively, read every executed order, log out.

    Interactive by design: it prompts on the terminal it is run from, so it cannot run
    inside the API or a scheduled job, and it should not.
    """
    try:
        from robin_stocks.robinhood import authentication, helper, orders, stocks  # noqa: PLC0415
    except ImportError as error:
        raise RobinhoodUnavailable(
            "robin_stocks is not installed. Install it with: pip install robin_stocks"
        ) from error

    authentication.login(store_session=False)
    try:
        instruments: dict[str, dict] = {}
        symbols: dict[str, str] = {}

        def instrument(url: str) -> dict:
            if url not in instruments:
                instruments[url] = helper.request_get(url) or {}
            return instruments[url]

        def ticker(url: str) -> str:
            if url not in symbols:
                symbols[url] = stocks.get_symbol_by_url(url) or ""
            return symbols[url]

        option_orders = orders.get_all_option_orders() or []
        stock_orders = orders.get_all_stock_orders() or []
        if not isinstance(option_orders, list) or not isinstance(stock_orders, list):
            raise RobinhoodUnavailable(
                "Robinhood did not return an order history. Was the login completed?"
            )
        return parse_option_orders(option_orders, instrument) + parse_stock_orders(
            stock_orders, ticker
        )
    finally:
        authentication.logout()
