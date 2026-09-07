"""Robinhood account activity export.

The file is the one a Robinhood account produces under Statements, a CSV with these
columns:

    Activity Date, Process Date, Settle Date, Instrument, Description,
    Trans Code, Quantity, Price, Amount

Everything below was verified against a real 434 row export covering 2026-07-13 to
2026-09-04. Nothing here is guessed from documentation, because Robinhood publishes
none for this format.

## What the format actually does, and where it bites

**Amounts use accounting parentheses.** `$7.94` is cash in, `($34.04)` is cash out.
A naive float parse silently turns every debit into a credit and doubles the account's
apparent profit.

**Option descriptions come in two shapes.** A trade is `TSLA 9/4/2026 Call $357.50`.
An expiration is `Option Expiration for IBIT 8/14/2026 Call $37.00`, which the same
regex will not match, so an importer written against trades alone loses every
expiration and leaves the position open forever.

**An expiring short leg is marked with a trailing S on the quantity**, as `1S`. It is
the only place in the entire file that states the direction of an expiring contract,
because an expiration has no transaction code to carry it and no cash amount at all.
Read it wrong and a closed spread reconstructs as two open contracts.

**Fees are recoverable and are not stated.** Price times multiplier times quantity does
not equal Amount, and the gap is the regulatory fee. Measured over 380 legs it is
$0.04 per contract on buys and $0.06 on sells. That number is worth having exactly
rather than as a config guess, because it is the cost input every expectancy question
needs.

**The file ends with a disclaimer paragraph** in a row with no transaction code. It is
not data and is skipped.

## The rule this module will not bend

**An unrecognised transaction code raises.** Robinhood has codes this export happened
not to contain, assignment and exercise among them, and both move real contracts. An
importer that skipped what it did not recognise would produce a tidy profit figure with
legs missing from it, which is worse than a crash because nothing would ever say so.
"""

from __future__ import annotations

import csv
import hashlib
import re
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from optscan.models.broker import OPTION_MULTIPLIER, BrokerTxn, Effect, TxnKind
from optscan.models.enums import Right
from optscan.models.opportunity import Action

SOURCE = "robinhood"

REQUIRED_COLUMNS = (
    "Activity Date",
    "Description",
    "Trans Code",
    "Quantity",
    "Price",
    "Amount",
)

#: `TSLA 9/4/2026 Call $357.50`
OPTION_TRADE = re.compile(
    r"^(?P<symbol>\S+)\s+(?P<expiry>\d{1,2}/\d{1,2}/\d{4})\s+"
    r"(?P<right>Call|Put)\s+\$(?P<strike>[\d,]+(?:\.\d+)?)$"
)
#: `Option Expiration for IBIT 8/14/2026 Call $37.00`
OPTION_EVENT = re.compile(
    r"^Option (?P<event>Expiration|Assignment|Exercise) for\s+(?P<symbol>\S+)\s+"
    r"(?P<expiry>\d{1,2}/\d{1,2}/\d{4})\s+(?P<right>Call|Put)\s+"
    r"\$(?P<strike>[\d,]+(?:\.\d+)?)$",
    re.IGNORECASE,
)

#: Option trade codes, and what each one does to a holding.
OPTION_TRADES: dict[str, tuple[Action, Effect]] = {
    "BTO": (Action.BUY, Effect.OPEN),
    "STO": (Action.SELL, Effect.OPEN),
    "BTC": (Action.BUY, Effect.CLOSE),
    "STC": (Action.SELL, Effect.CLOSE),
}

EQUITY_TRADES: dict[str, Action] = {"Buy": Action.BUY, "Sell": Action.SELL}

#: Codes that end an option position without being a trade.
OPTION_EVENTS: dict[str, TxnKind] = {
    "OEXP": TxnKind.OPTION_EXPIRATION,
    "OASGN": TxnKind.OPTION_ASSIGNMENT,
    "OEXCS": TxnKind.OPTION_EXERCISE,
}

#: Cash movements. None of these touch a holding, and all of them touch the balance,
#: which is why an equity curve built from trades alone never reconciles.
CASH_CODES = frozenset(
    {
        "ACH",  # bank transfer
        "ACATI",  # account transfer in
        "ACATO",  # account transfer out
        "AFEE",  # advisory fee
        "CDIV",  # cash dividend
        "DFEE",  # foreign tax or fee on a dividend
        "DTAX",  # dividend tax withheld
        "GOLD",  # subscription fee
        "GDBP",  # gold buying power adjustment
        "INT",  # interest paid on cash
        "MINT",  # margin interest
        "MRGC",  # margin credit
        "MISC",  # anything Robinhood declines to name
        "RTP",  # instant transfer
        "REC",  # receipt, seen with a zero amount
        "SPL",  # stock split
        "SPR",  # reverse split
        "CONV",  # conversion
    }
)


class RobinhoodParseError(ValueError):
    """The file is not the shape this parser understands."""


class UnknownTransactionCode(RobinhoodParseError):
    """A code this parser has never seen.

    Deliberately fatal. See the module docstring: silently skipping a code that moves
    contracts produces a profit figure with holes and no way to notice them.
    """

    def __init__(self, code: str, description: str, row: int) -> None:
        super().__init__(
            f"row {row}: unknown Robinhood transaction code {code!r} "
            f"({description!r}). Add it to optscan.imports.robinhood so it is handled "
            "on purpose rather than dropped."
        )
        self.code = code


def _money(value: str | None) -> float | None:
    """A Robinhood currency cell to a signed float.

    Parentheses mean cash left the account. Returns None for a blank cell, which is a
    real state: an expiration has no amount at all, and that is not zero dollars, it is
    a row that does not state one.
    """
    text = (value or "").strip()
    if not text:
        return None
    negative = text.startswith("(") and text.endswith(")")
    digits = re.sub(r"[^\d.]", "", text)
    if not digits:
        return None
    try:
        amount = float(digits)
    except ValueError:  # pragma: no cover, guarded by the regex above
        return None
    return -amount if negative else amount


def _quantity(value: str | None) -> tuple[float | None, bool]:
    """Quantity, and whether Robinhood marked it short with a trailing S."""
    text = (value or "").strip()
    if not text:
        return None, False
    short = text.upper().endswith("S")
    digits = re.sub(r"[^\d.]", "", text)
    if not digits:
        return None, short
    return float(digits), short


def _day(value: str | None) -> date | None:
    text = (value or "").strip()
    if not text:
        return None
    return datetime.strptime(text, "%m/%d/%Y").date()


def _fee(
    price: float | None,
    quantity: float | None,
    amount: float | None,
    kind: TxnKind,
) -> float | None:
    """What the broker kept, as the gap between the stated price and the cash moved.

    **Options only, and that restriction is load bearing.** For a contract the export
    states an exact price in cents against a whole number of contracts, so the residual
    against the amount is genuinely the regulatory fee: measured over 380 legs it came
    out at 4 cents a contract on buys and 6 on sells, never negative.

    For shares the same arithmetic is nonsense, because Robinhood rounds the displayed
    price to two decimals while the amount is exact. `HTZ Buy 100 @ $2.58` shows a
    gross of $258.00 against an amount of $257.50: the fill was really $2.575, and the
    50 cent "fee" is the rounding. Fractional share quantities make it worse. Fourteen
    of the thirty four share rows in the reference export produced a negative fee this
    way, which is how a rounding artefact announces itself as a measurement.

    So the answer for an equity row is None: not zero, because the fee is unknown
    rather than absent, and an unknown averaged in as zero understates every cost that
    is later built on it.
    """
    if kind is not TxnKind.OPTION_TRADE:
        return None
    if price is None or quantity is None or amount is None:
        return None
    gross = price * OPTION_MULTIPLIER * quantity
    if gross <= 0:
        return None
    # A sale brings in less than gross, a purchase costs more. Either way the fee is
    # the size of the shortfall, so compare magnitudes and let the sign fall out.
    return round(gross - abs(amount), 4) if amount > 0 else round(abs(amount) - gross, 4)


def _digest(source: str, row: dict[str, Any]) -> str:
    """Stable identity for a row's content.

    Not unique on its own: two identical fills on one day are two trades. The ledger
    pairs this with an occurrence index. See `storage/ledger.py`.
    """
    payload = "|".join(
        str(row.get(column, "") or "").strip()
        for column in (
            "Activity Date",
            "Process Date",
            "Settle Date",
            "Instrument",
            "Description",
            "Trans Code",
            "Quantity",
            "Price",
            "Amount",
        )
    )
    return hashlib.sha256(f"{source}|{payload}".encode()).hexdigest()[:32]


def _is_footer(row: dict[str, Any]) -> bool:
    """The disclaimer paragraph Robinhood appends, and any wholly blank row."""
    code = (row.get("Trans Code") or "").strip()
    if code:
        return False
    activity = (row.get("Activity Date") or "").strip()
    description = (row.get("Description") or "").strip()
    return not activity or not description


def parse_rows(
    rows: Iterable[dict[str, Any]],
    *,
    source_file: str | None = None,
) -> list[BrokerTxn]:
    """Parse already-read CSV rows. Pure: no file access, no database, no clock."""
    seen: Counter[str] = Counter()
    out: list[BrokerTxn] = []

    for index, raw in enumerate(rows, start=2):  # 2 because row 1 is the header
        if _is_footer(raw):
            continue

        code = (raw.get("Trans Code") or "").strip()
        description = (raw.get("Description") or "").strip()
        activity = _day(raw.get("Activity Date"))
        if activity is None:
            raise RobinhoodParseError(f"row {index}: no Activity Date")

        quantity, is_short = _quantity(raw.get("Quantity"))
        price = _money(raw.get("Price"))
        amount = _money(raw.get("Amount"))
        instrument = (raw.get("Instrument") or "").strip() or None

        parsed = _classify(code, description, index)
        kind = parsed.kind

        digest = _digest(SOURCE, raw)
        dup_index = seen[digest]
        seen[digest] += 1

        out.append(
            BrokerTxn(
                source=SOURCE,
                source_file=source_file,
                activity_date=activity,
                process_date=_day(raw.get("Process Date")),
                settle_date=_day(raw.get("Settle Date")),
                instrument=instrument,
                description=description,
                trans_code=code,
                quantity=quantity,
                price=abs(price) if price is not None else None,
                amount=amount,
                kind=kind,
                symbol=parsed.symbol or instrument,
                expiry=parsed.expiry,
                right=parsed.right,
                strike=parsed.strike,
                action=parsed.action,
                effect=parsed.effect,
                is_short=is_short,
                fee=_fee(price, quantity, amount, kind),
                digest=digest,
                dup_index=dup_index,
            )
        )
    return out


@dataclass(frozen=True, slots=True)
class Classified:
    """What a row turned out to be. Named rather than a seven wide tuple, because the
    call site unpacking that positionally is one edit away from swapping strike and
    strantime silently."""

    kind: TxnKind
    symbol: str | None = None
    expiry: date | None = None
    right: Right | None = None
    strike: float | None = None
    action: Action | None = None
    effect: Effect | None = None


def _classify(code: str, description: str, row: int) -> Classified:
    """Decide what a row is from its code, then confirm against its description."""
    if code in OPTION_TRADES:
        match = OPTION_TRADE.match(description)
        if match is None:
            raise RobinhoodParseError(
                f"row {row}: {code} is an option trade but its description does not "
                f"parse as a contract: {description!r}"
            )
        action, effect = OPTION_TRADES[code]
        return Classified(
            TxnKind.OPTION_TRADE,
            match["symbol"],
            _day(match["expiry"]),
            Right.parse(match["right"]),
            float(match["strike"].replace(",", "")),
            action,
            effect,
        )

    if code in OPTION_EVENTS:
        match = OPTION_EVENT.match(description)
        if match is None:
            raise RobinhoodParseError(
                f"row {row}: {code} is an option event but its description does not "
                f"parse as one: {description!r}"
            )
        return Classified(
            OPTION_EVENTS[code],
            match["symbol"],
            _day(match["expiry"]),
            Right.parse(match["right"]),
            float(match["strike"].replace(",", "")),
            None,
            Effect.CLOSE,
        )

    if code in EQUITY_TRADES:
        action = EQUITY_TRADES[code]
        return Classified(TxnKind.EQUITY_TRADE, action=action)

    if code in CASH_CODES:
        return Classified(TxnKind.CASH)

    raise UnknownTransactionCode(code, description, row)


def parse_file(path: str | Path) -> list[BrokerTxn]:
    """Read and parse a Robinhood activity export."""
    file = Path(path)
    with file.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        missing = [c for c in REQUIRED_COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise RobinhoodParseError(
                f"{file.name} is missing expected columns {missing}. Found: {reader.fieldnames}"
            )
        return parse_rows(_clean(reader), source_file=file.name)


def _clean(reader: Iterable[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    """Drop the None key csv.DictReader adds for the ragged disclaimer row."""
    for row in reader:
        yield {k: v for k, v in row.items() if k is not None}
