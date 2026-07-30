"""Command line entry point.

Thin by design: every command resolves settings, boots logging, and calls into a
module that is testable without a terminal.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from datetime import UTC, datetime

from optscan import __version__
from optscan.config import Settings, get_settings
from optscan.logging import configure_logging, get_logger


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
        "snapshot": _cmd_snapshot,
        "watchlist": _cmd_watchlist,
        "status": _cmd_status,
        "schedule": _cmd_schedule,
    }
    return handlers[args.command](settings, args)


if __name__ == "__main__":
    raise SystemExit(main())
