"""Named symbol groups, loaded from universe.yaml.

A thin reader with one opinion in it: **these lists are curated and dated, never
derived.** Alpaca publishes 14,277 active US equities and carries no sector, industry
or index field on any of them, so there is nothing in any vendor this project reaches
that could produce "the S&P 500" or "tech". Somebody typed them.

That is worth a module docstring because the failure it invites is quiet. A group
called `sp500_large` reads like index membership, and anything measured across it will
be described that way in conversation within a week. It is not: it is the constituents
somebody remembered to type, which is exactly the set that did not get dropped for
performing badly. Every aggregate over these groups carries that survivorship, and
`meta.date_checked` in the file is the only thing that says how stale it is.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import yaml

from optscan.config import REPO_ROOT
from optscan.logging import get_logger

log = get_logger("optscan.universe")

DEFAULT_UNIVERSE_FILENAME = "universe.yaml"


class UniverseError(ValueError):
    """A universe file that cannot be read. Fatal rather than defaulted to empty."""


@dataclass(frozen=True, slots=True)
class Universe:
    """Named groups of symbols, and when a human last checked them."""

    groups: dict[str, tuple[str, ...]]
    date_checked: date | None = None
    note: str | None = None

    @property
    def names(self) -> list[str]:
        return sorted(self.groups)

    def symbols(self, *names: str) -> list[str]:
        """Every symbol in the named groups, deduplicated, sorted.

        `all` is accepted and means every group. Deduplication matters more than it
        looks: NVDA is in `tech` and would be in any large cap group, and fetching it
        twice would double its weight in anything counted over the result.
        """
        wanted = self.names if not names or "all" in names else list(names)
        unknown = [name for name in wanted if name not in self.groups]
        if unknown:
            raise UniverseError(
                f"unknown group(s): {', '.join(sorted(unknown))}. "
                f"Available: {', '.join(self.names)}"
            )
        out: set[str] = set()
        for name in wanted:
            out.update(self.groups[name])
        return sorted(out)

    def staleness_note(self, today: date | None = None) -> str | None:
        """A sentence about how old the curation is, or None when it is fresh.

        Said out loud because index membership drifts and this file does not notice.
        """
        if self.date_checked is None:
            return "This universe has no date_checked, so nothing says how current it is."
        age = ((today or date.today()) - self.date_checked).days
        if age <= STALE_AFTER_DAYS:
            return None
        return (
            f"The universe was last checked by hand on {self.date_checked}, {age} days "
            "ago. These are curated lists rather than index membership, so additions "
            "and delistings since then are missing."
        )


#: Past this many days the curation is worth a warning. A quarter: index changes
#: cluster around quarterly rebalances, so a list older than one has probably missed
#: at least one.
STALE_AFTER_DAYS = 90


def universe_path(path: Path | None = None) -> Path:
    return path or (REPO_ROOT / DEFAULT_UNIVERSE_FILENAME)


def load_universe(path: Path | None = None) -> Universe:
    """Read universe.yaml. A missing or malformed file raises rather than defaulting.

    Defaulting to an empty universe would make `optscan prices sync` succeed having
    done nothing, which is the failure mode this project refuses everywhere else.
    """
    target = universe_path(path)
    if not target.is_file():
        raise UniverseError(f"no universe file at {target}")

    try:
        raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as error:
        raise UniverseError(f"{target.name} is not valid YAML: {error}") from error
    if not isinstance(raw, dict):
        raise UniverseError(f"{target.name} must be a mapping at the top level")

    raw_groups = raw.get("groups")
    if not isinstance(raw_groups, dict) or not raw_groups:
        raise UniverseError(f"{target.name} has no groups")

    groups: dict[str, tuple[str, ...]] = {}
    for name, members in raw_groups.items():
        if not isinstance(members, list) or not members:
            raise UniverseError(f"group {name!r} is empty or not a list")
        # Deduplicated within the group but order preserved for readability of the file
        # itself; symbols() sorts when it merges.
        seen: dict[str, None] = {}
        for member in members:
            # Refused rather than coerced. YAML 1.1 reads a bare ON, OFF, YES, NO, Y or
            # N as a boolean, so `- ON` for ON Semiconductor arrives here as True and
            # `str(True).upper()` is the ticker "TRUE". That happened: ON was silently
            # absent from every sync and a phantom TRUE was requested in its place,
            # which the vendor answered with nothing and the report blamed on a
            # delisting. Quoting the symbol in the file fixes the data; refusing the
            # type is what stops the next one being invisible.
            if not isinstance(member, str):
                raise UniverseError(
                    f"group {name!r} contains {member!r} ({type(member).__name__}), "
                    "not a ticker. YAML reads a bare ON, OFF, YES or NO as a boolean, "
                    'so quote it: - "ON"'
                )
            ticker = member.strip().upper()
            if ticker:
                seen[ticker] = None
        groups[str(name)] = tuple(seen)

    meta = raw.get("meta") or {}
    checked = meta.get("date_checked")
    if isinstance(checked, str):
        try:
            checked = date.fromisoformat(checked)
        except ValueError:
            checked = None
    if not isinstance(checked, date):
        checked = None

    total = len({s for members in groups.values() for s in members})
    log.info("universe loaded", groups=len(groups), symbols=total, checked=str(checked))
    return Universe(groups=groups, date_checked=checked, note=meta.get("note"))
