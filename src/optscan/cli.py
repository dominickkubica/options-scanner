"""Command line entry point.

Thin by design: every command resolves settings, boots logging, and calls into a
module that is testable without a terminal.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from optscan import __version__
from optscan.config import Settings, get_settings
from optscan.logging import configure_logging, get_logger
from optscan.screener.config import ScreenConfig


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="optscan",
        description="Options selling scanner. Ranks and displays, never places orders.",
    )
    parser.add_argument("--version", action="version", version=f"optscan {__version__}")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Print resolved configuration and exit without creating directories.",
    )

    sub = parser.add_subparsers(dest="command")

    snapshot = sub.add_parser(
        "snapshot",
        help="Capture option chains for the watchlist. Safe to run more than once a day.",
    )
    snapshot.add_argument(
        "--symbol",
        action="append",
        dest="symbols",
        metavar="TICKER",
        help="Capture this symbol instead of the watchlist. Repeatable.",
    )
    snapshot.add_argument(
        "--force",
        action="store_true",
        help="Capture even when the market is closed. The mark will be stale.",
    )
    snapshot.add_argument(
        "--recapture",
        action="store_true",
        help="Capture again even if this session was already captured.",
    )

    watchlist = sub.add_parser("watchlist", help="Inspect and edit the watchlist.")
    watchlist_sub = watchlist.add_subparsers(dest="watchlist_command", required=True)
    watchlist_sub.add_parser("list", help="Print the watchlist.")
    wl_add = watchlist_sub.add_parser("add", help="Add symbols.")
    wl_add.add_argument("symbols", nargs="+", metavar="TICKER")
    wl_remove = watchlist_sub.add_parser("remove", help="Remove symbols.")
    wl_remove.add_argument("symbols", nargs="+", metavar="TICKER")

    scan = sub.add_parser("scan", help="Rank premium selling candidates.")
    scan.add_argument(
        "--symbol",
        action="append",
        dest="symbols",
        metavar="TICKER",
        help="Scan this symbol instead of the watchlist. Repeatable.",
    )
    scan.add_argument(
        "--watchlist",
        default="default",
        help="Named watchlist to scan. Only 'default' exists so far.",
    )
    scan.add_argument(
        "--live",
        action="store_true",
        help="Fetch fresh chains instead of using the most recent stored snapshot.",
    )
    scan.add_argument("--limit", type=int, default=20, help="Rows to print.")
    scan.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Screen config YAML. Defaults to screen.yaml beside the repo root.",
    )
    scan.add_argument("--gaps", action="store_true", help="Also print flagged mispricings.")
    scan.add_argument(
        "--explain",
        action="store_true",
        help="Show the score breakdown and warnings for each row.",
    )
    scan.add_argument(
        "--no-events",
        action="store_true",
        help="Skip the corporate calendar fetch. Faster, and blind to earnings.",
    )

    config_cmd = sub.add_parser("config", help="Print the effective screen configuration.")
    config_cmd.add_argument("--config", type=Path, default=None, help="Config YAML to load.")

    status = sub.add_parser("status", help="Market state, watchlist size, recent captures.")
    status.add_argument("--runs", type=int, default=10, help="How many recent runs to show.")

    schedule = sub.add_parser(
        "schedule",
        help="Print or install the Windows Task Scheduler entry for the daily snapshot.",
    )
    schedule.add_argument(
        "--install",
        action="store_true",
        help="Actually register the scheduled task. Without this the command is only printed.",
    )

    return parser


def startup(settings: Settings, *, create_dirs: bool = True) -> None:
    """Boot sequence shared by every command and, later, the API server."""
    configure_logging(settings)
    log = get_logger("optscan.startup")

    if create_dirs:
        settings.ensure_dirs()

    log.info("optscan starting", version=__version__, **settings.safe_summary())


def _cmd_snapshot(settings: Settings, args: argparse.Namespace) -> int:
    from optscan.jobs.snapshot import run_snapshot

    report = run_snapshot(
        settings,
        symbols=args.symbols,
        force=args.force,
        skip_existing=not args.recapture,
    )

    if report.skipped_reason:
        print(f"Skipped: {report.skipped_reason}")
        return 0

    print(f"Session {report.session_date}")
    for result in report.results:
        if result.ok:
            flag = " (partial)" if result.partial else ""
            print(
                f"  ok    {result.symbol:<6} {result.expiries:>3} expiries "
                f"{result.contracts:>5} contracts{flag}"
            )
        else:
            print(f"  FAIL  {result.symbol:<6} {result.error}")

    if not report.results:
        print("  nothing to do: every symbol was already captured for this session")
    return 1 if report.failed else 0


def _cmd_watchlist(settings: Settings, args: argparse.Namespace) -> int:
    from optscan.storage import db

    with db.session(settings.sqlite_path) as conn:
        db.seed_watchlist(conn, settings.default_watchlist)

        if args.watchlist_command == "add":
            for symbol in args.symbols:
                added = db.add_symbol(conn, symbol)
                print(f"{'added' if added else 'already present'}: {symbol.upper()}")
        elif args.watchlist_command == "remove":
            for symbol in args.symbols:
                removed = db.remove_symbol(conn, symbol)
                print(f"{'removed' if removed else 'not present'}: {symbol.upper()}")

        symbols = db.list_watchlist(conn)

    print(f"watchlist ({len(symbols)}): {', '.join(symbols) if symbols else 'empty'}")
    return 0


def _screen_config(settings: Settings, path: Path | None) -> ScreenConfig:
    """Load the screen config from an explicit path, or the repo default if present."""
    from optscan.config import REPO_ROOT
    from optscan.screener.config import DEFAULT_CONFIG_FILENAME

    return ScreenConfig.load(path or (REPO_ROOT / DEFAULT_CONFIG_FILENAME))


def _cmd_scan(settings: Settings, args: argparse.Namespace) -> int:
    from optscan.jobs.scan import STALE_AFTER_HOURS, run_scan

    if args.watchlist != "default":
        print(f"Unknown watchlist {args.watchlist!r}. Only 'default' exists so far.")
        return 1

    config = _screen_config(settings, args.config)
    result, inputs = run_scan(
        settings,
        config,
        symbols=args.symbols,
        live=args.live,
        with_events=not args.no_events,
    )

    if inputs.missing:
        print(
            f"No data for: {', '.join(inputs.missing)}. "
            "Run `optscan snapshot` first, or pass --live."
        )
    for symbol, message in result.symbols_failed.items():
        print(f"Failed: {symbol}: {message}")

    for symbol, age_seconds in sorted(result.stale_symbols.items()):
        hours = age_seconds / 3600.0
        if hours > STALE_AFTER_HOURS:
            print(f"Stale: {symbol} quotes are {hours:.1f} hours old.")

    rows = result.top(args.limit)
    if not rows:
        print("No candidates passed the screen.")
        print(f"  {result.tally.summary()}")
        return 0

    print()
    print(
        f"{'symbol':<7}{'expiry':<12}{'strategy':<20}{'legs':<18}"
        f"{'credit':>7}{'profit':>8}{'capital':>9}{'ann':>7}{'POP':>6}"
        f"{'delta':>7}{'liq':>5}{'score':>7}"
    )
    print("-" * 113)
    for opportunity in rows:
        legs = "/".join(f"{leg.strike:g}{leg.right}" for leg in opportunity.legs)
        annualized = (
            f"{opportunity.annualized_return:.0%}"
            if opportunity.annualized_return is not None
            else "n/a"
        )
        pop = (
            f"{opportunity.probability_of_profit:.0%}"
            if opportunity.probability_of_profit is not None
            else "n/a"
        )
        delta = f"{opportunity.short_delta:.2f}" if opportunity.short_delta is not None else "n/a"
        liquidity = (
            f"{opportunity.liquidity_score:.2f}"
            if opportunity.liquidity_score is not None
            else "n/a"
        )
        capital = (
            f"{opportunity.capital:>9,.0f}" if opportunity.capital is not None else f"{'n/a':>9}"
        )
        print(
            f"{opportunity.symbol:<7}{opportunity.expiry!s:<12}"
            f"{opportunity.strategy.value:<20}{legs:<18}"
            f"{opportunity.credit:>7.2f}{opportunity.max_profit:>8.0f}{capital}"
            f"{annualized:>7}{pop:>6}{delta:>7}{liquidity:>5}{opportunity.score:>7.3f}"
        )
        if args.explain:
            parts = ", ".join(
                f"{name}={value:.2f}" if value is not None else f"{name}=n/a"
                for name, value in opportunity.components.as_dict().items()
            )
            print(f"         {parts}")
            for warning in opportunity.warnings:
                print(f"         ! {warning}")

    print()
    print(f"  {result.tally.summary()}")
    if result.opportunities and len(result.opportunities) > len(rows):
        print(f"  showing {len(rows)} of {len(result.opportunities)}")

    if args.gaps:
        _print_gaps(result)

    print()
    print("  Profit and capital are dollars per contract, net of modelled commissions.")
    print("  Scores rank candidates for review. They are not validated against outcomes.")
    return 0


def _print_gaps(result) -> None:
    if not result.gaps:
        print("\n  No gaps flagged.")
        return

    print(f"\n  Gaps flagged: {len(result.gaps)}")
    for gap in result.gaps[:10]:
        marker = "*" if gap.actionable else " "
        print(f"  {marker} [{gap.kind.value}] {gap.symbol} {gap.expiry}: {gap.description}")
        if not gap.actionable:
            print(f"      not actionable: {gap.executability.value}")
        for caveat in gap.caveats[:2]:
            print(f"      caveat: {caveat}")


def _cmd_config(settings: Settings, args: argparse.Namespace) -> int:
    print(_screen_config(settings, args.config).to_yaml())
    return 0


def _cmd_status(settings: Settings, args: argparse.Namespace) -> int:
    from optscan.jobs.snapshot import describe_state
    from optscan.market_calendar import session_date_for
    from optscan.storage import db

    now = datetime.now(UTC)
    print(f"provider     {settings.provider}")
    print(f"market       {describe_state(settings, now)}")
    session = session_date_for(now, settings.market_calendar, settings.market_timezone)
    print(f"session      {session or 'none right now'}")
    print(f"capture at   {settings.snapshot_time_local} {settings.market_timezone}")
    print(f"data         {settings.data_path}")

    with db.session(settings.sqlite_path) as conn:
        db.seed_watchlist(conn, settings.default_watchlist)
        symbols = db.list_watchlist(conn)
        runs = db.recent_runs(conn, args.runs)

    print(f"watchlist    {len(symbols)}: {', '.join(symbols) if symbols else 'empty'}")
    print(f"recent runs  {len(runs)}")
    for row in runs:
        status = "FAIL" if row["error"] else ("partial" if row["partial"] else "ok")
        detail = row["error"] or f"{row['expiries']} expiries, {row['contracts']} contracts"
        print(f"  {row['session_date']}  {row['symbol']:<6} {status:<7} {detail}")
    return 0


def _cmd_schedule(settings: Settings, args: argparse.Namespace) -> int:
    from optscan.jobs.schedule import install_task, task_command

    command = task_command(settings)
    if not args.install:
        print("Register the daily snapshot with:\n")
        print(f"  {command}\n")
        print("Or run: optscan schedule --install")
        return 0

    result = install_task(settings)
    print(result.message)
    return 0 if result.ok else 1


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = get_settings()
    startup(settings, create_dirs=not args.check)

    if args.check or args.command is None:
        return 0

    handlers = {
        "scan": _cmd_scan,
        "config": _cmd_config,
        "snapshot": _cmd_snapshot,
        "watchlist": _cmd_watchlist,
        "status": _cmd_status,
        "schedule": _cmd_schedule,
    }
    return handlers[args.command](settings, args)


if __name__ == "__main__":
    raise SystemExit(main())
