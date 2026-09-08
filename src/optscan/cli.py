"""Command line entry point.

Thin by design: every command resolves settings, boots logging, and calls into a
module that is testable without a terminal.
"""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path

from optscan import __version__
from optscan.config import REPO_ROOT, Settings, get_settings
from optscan.logging import configure_logging, get_logger
from optscan.screener.config import ScreenConfig

#: Commands whose runs are logged, so `optscan health` can say whether the work has
#: happened. Exactly the scheduled jobs: logging `status` or `config` would bury the
#: rows that matter under rows nobody asked about.
TRACKED_JOBS = frozenset({"snapshot", "record", "resolve", "backup", "manage"})


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

    serve = sub.add_parser("serve", help="Run the dashboard API, and the built UI if present.")
    serve.add_argument("--host", default=None, help="Defaults to api_host in config.")
    serve.add_argument("--port", type=int, default=None, help="Defaults to api_port in config.")
    serve.add_argument(
        "--reload",
        action="store_true",
        help="Restart on source changes. Development only.",
    )

    status = sub.add_parser("status", help="Market state, watchlist size, recent captures.")
    status.add_argument("--runs", type=int, default=10, help="How many recent runs to show.")

    health = sub.add_parser(
        "health",
        help="Are the recurring jobs actually running? Every day one is missed is permanent.",
    )
    health.add_argument(
        "--quiet",
        action="store_true",
        help="Print only what needs attention, and exit 1 if anything does.",
    )

    # Positions. Entry is manual and the fill price is required, because it is the one
    # number the tool cannot reconstruct and the one every P/L is measured against.
    position = sub.add_parser("position", help="Track positions you actually hold.")
    position_sub = position.add_subparsers(dest="position_command", required=True)

    position_sub.add_parser("list", help="Open positions with their profit and greeks.")

    pos_add = position_sub.add_parser(
        "add",
        help="Record a position you opened.",
        description=(
            "Legs are given as ACTION:RIGHT:STRIKE:FILL, for example sell:P:700:5.20. "
            "Repeat --leg for a spread. Every leg needs the price you actually got, "
            "not the mid it was showing."
        ),
    )
    pos_add.add_argument("symbol", help="Underlying symbol.")
    pos_add.add_argument("--expiry", required=True, help="Expiry, YYYY-MM-DD.")
    pos_add.add_argument(
        "--leg",
        action="append",
        required=True,
        metavar="ACTION:RIGHT:STRIKE:FILL",
        help="Repeatable. sell:P:700:5.20",
    )
    pos_add.add_argument("--quantity", type=int, default=1, help="Contracts per leg.")
    pos_add.add_argument("--commission", type=float, default=0.0, help="Paid to open.")
    pos_add.add_argument("--strategy", default=None, help="Optional label, e.g. cash_secured_put.")
    pos_add.add_argument("--note", default=None)

    pos_close = position_sub.add_parser("close", help="Mark a position closed.")
    pos_close.add_argument("id", type=int)
    pos_close.add_argument(
        "--value",
        type=float,
        required=True,
        help="Net cash to close, credit positive. Paying 2.00 to buy back is -200.",
    )
    pos_close.add_argument("--commission", type=float, default=0.0)

    pos_delete = position_sub.add_parser(
        "delete",
        help="Remove a position entered by mistake. Not the same as closing one.",
    )
    pos_delete.add_argument("id", type=int)

    # Validation. Recording has to start on day one for the same reason the IV history
    # did: a candidate never logged when it was scored cannot be settled later.
    record = sub.add_parser(
        "record",
        help="Score the watchlist and log every candidate for later validation.",
    )
    record.add_argument("--config", type=Path, default=None, help="Config YAML to load.")
    record.add_argument("--symbols", nargs="*", default=None, help="Defaults to the watchlist.")
    record.add_argument(
        "--limit",
        type=int,
        default=None,
        help=(
            "Log only the top N. Off by default and biases the study when used: "
            "recording only what the score already likes cannot test the score."
        ),
    )

    resolve = sub.add_parser(
        "resolve",
        help="Settle logged candidates whose expiry has passed, from the underlying close.",
    )
    resolve.add_argument(
        "--offline",
        action="store_true",
        help="Skip the provider. Nothing can be settled without price history.",
    )

    validate_cmd = sub.add_parser(
        "validate",
        help="Report whether the score actually separates outcomes.",
    )
    validate_cmd.add_argument(
        "--buckets",
        type=int,
        default=4,
        help="Score buckets. Quartiles by default: deciles need far more data than exists.",
    )

    manage = sub.add_parser(
        "manage",
        help="Mark every open position, evaluate triggers, and send new alerts.",
    )
    manage.add_argument("--config", type=Path, default=None, help="Config YAML to load.")
    manage.add_argument(
        "--quiet",
        action="store_true",
        help="Evaluate and print without delivering alerts. Does not consume the once-only send.",
    )
    manage.add_argument(
        "--offline",
        action="store_true",
        help=(
            "Skip the provider. Positions still mark from stored snapshots; beta "
            "weighting and the event triggers are reported as unchecked."
        ),
    )

    sub.add_parser(
        "backup",
        help="Copy the database and mirror the captures to the backup directory.",
    )

    shortcut = sub.add_parser(
        "shortcut",
        help="Put a Desktop shortcut that opens the dashboard, no terminal needed.",
    )
    shortcut.add_argument(
        "--remove",
        action="store_true",
        help="Take the shortcut off the Desktop again.",
    )

    schedule = sub.add_parser(
        "schedule",
        help="Print or install the Windows Task Scheduler entries for the recurring jobs.",
    )
    schedule.add_argument(
        "--install",
        action="store_true",
        help="Actually register the scheduled tasks. Without this they are only printed.",
    )
    schedule.add_argument(
        "jobs",
        nargs="*",
        default=None,
        help=(
            "Which jobs to install. Defaults to the ones that protect history it is "
            "impossible to rebuild: snapshot, record, resolve."
        ),
    )

    import_cmd = sub.add_parser(
        "import",
        help="Import a broker activity export into the ledger.",
        description=(
            "Reads a broker statement and appends its rows to the ledger. Safe to run "
            "on overlapping exports: rows already held are counted and skipped, so "
            "importing this month's download after last month's adds only what is new."
        ),
    )
    import_cmd.add_argument("path", help="Path to the exported CSV.")
    import_cmd.add_argument(
        "--broker",
        default="robinhood",
        choices=["robinhood"],
        help="Which broker's export format this is. Default: robinhood.",
    )
    import_cmd.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and report, without writing anything to the ledger.",
    )

    import_history = sub.add_parser(
        "import-history",
        help="Import a Market Chameleon daily export, including its IV30 history.",
        description=(
            "Reads a downloaded daily history file and stores it as that vendor's own "
            "series. The reason this exists is the IV30 column: no provider this tool "
            "can reach publishes historical implied volatility, so IV rank has been "
            "unavailable since the project started. One file is twelve years of it.\n\n"
            "The format has no symbol column. The ticker comes from the filename and "
            "is printed before anything is written; pass --symbol to override it. "
            "Safe to re-run: sessions already held are counted and skipped, and any "
            "that disagree are reported rather than overwritten."
        ),
    )
    import_history.add_argument(
        "path",
        help="A downloaded CSV, or a directory of them to import in one go.",
    )
    import_history.add_argument(
        "--symbol",
        default=None,
        help=(
            "Which ticker this file is. Defaults to the one in the filename. Required "
            "when the filename does not carry one, because the file itself does not."
        ),
    )
    import_history.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and report, without writing anything.",
    )

    prices = sub.add_parser(
        "prices",
        help="Bulk daily price history for named symbol universes.",
        description=(
            "Fetches daily bars for many symbols at once and stores them per vendor. "
            "Keeps three volume fields, not one: shares, trade count and VWAP, because "
            "30 million shares in 500,000 prints and the same volume in 5,000 are very "
            "different sessions and volume alone cannot tell them apart.\n\n"
            "Safe to re-run: sessions already held are counted and skipped."
        ),
    )
    prices_sub = prices.add_subparsers(dest="prices_command", required=True)

    prices_sync = prices_sub.add_parser("sync", help="Fetch and store history.")
    prices_sync.add_argument(
        "groups",
        nargs="*",
        default=None,
        metavar="GROUP",
        help="Universe groups from universe.yaml. Defaults to all of them.",
    )
    prices_sync.add_argument(
        "--symbol",
        action="append",
        dest="symbols",
        metavar="TICKER",
        help="Sync this symbol instead of a group. Repeatable.",
    )
    prices_sync.add_argument(
        "--days",
        type=int,
        default=None,
        help="Calendar days of history. Defaults to ten years.",
    )

    prices_sub.add_parser("groups", help="List the universe groups and their sizes.")

    sub.add_parser(
        "history",
        help="What downloaded vendor history is held, per symbol.",
        description=(
            "Coverage of the imported daily histories: how many sessions, how many "
            "carry an implied vol, and whether that is enough for a rank."
        ),
    )

    trades = sub.add_parser(
        "trades",
        help="Realized results from the imported broker ledger.",
        description=(
            "What the account actually did, derived from imported statements. This is "
            "real fills, unlike `validate`, which measures the screen held to expiry."
        ),
    )
    trades.add_argument("--symbol", default=None, help="Only this underlying.")
    trades.add_argument("--limit", type=int, default=20, help="Round trips to list. Default 20.")

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

    # Rescored across the symbols actually being compared, for the same reason the
    # dashboard does it: a component only some symbols can compute makes the ones
    # without it look better, because the missing weight is renormalized into the
    # average of their other components. Display only; the record job does not.
    result.rank(config=config)

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


def _cmd_serve(settings: Settings, args: argparse.Namespace) -> int:
    """Start uvicorn against the app factory.

    The import is deferred like every other command's, so `optscan status` does not
    pay for FastAPI. Nothing here reads the network beyond binding a local port.
    """
    import uvicorn

    from optscan.api.deps import frontend_dist

    host = args.host or settings.api_host
    port = args.port or settings.api_port

    print(f"API on http://{host}:{port}/api/health")
    if frontend_dist() is None:
        print("No built frontend. Run `npm run dev` in frontend/ and open http://localhost:5173")
    else:
        print(f"Dashboard on http://{host}:{port}/")

    uvicorn.run(
        "optscan.api.app:app",
        host=host,
        port=port,
        reload=args.reload,
        log_config=None,
    )
    return 0


def _cmd_status(settings: Settings, args: argparse.Namespace) -> int:
    from optscan.console import Console, pad
    from optscan.jobs.health import check, needs_attention
    from optscan.jobs.snapshot import describe_state
    from optscan.market_calendar import session_date_for
    from optscan.storage import db

    console = Console.for_stream(settings.color_mode)
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

    # The headline, because a status page that reports the watchlist and not whether
    # the jobs ran is reassuring about the wrong thing.
    urgent = needs_attention(check(settings, now=now))
    if urgent:
        summary = console.bad(f"{len(urgent)} needing attention")
        names = ", ".join(item.key for item in urgent)
        print(f"jobs         {summary}: {names}. Run optscan health.")
    else:
        print(f"jobs         {console.good('all running')}")

    print(f"watchlist    {len(symbols)}: {', '.join(symbols) if symbols else 'empty'}")
    print(f"recent runs  {len(runs)}")
    for row in runs:
        if row["error"]:
            status = console.bad("FAIL")
        elif row["partial"]:
            status = console.warn("partial")
        else:
            status = console.good("ok")
        detail = row["error"] or f"{row['expiries']} expiries, {row['contracts']} contracts"
        print(f"  {row['session_date']}  {row['symbol']:<6} {pad(status, 7)} {detail}")
    return 0


def _cmd_backup(settings: Settings, args: argparse.Namespace) -> int:
    from optscan.jobs.backup import run_backup

    del args
    result = run_backup(settings)

    if not result.ok:
        for note in result.notes:
            print(f"  {note}")
        return 1

    print(f"database   {result.database}  ({result.database_bytes / 1024:.0f} KB, verified)")
    print(
        f"snapshots  {result.files_copied} copied "
        f"({result.bytes_copied / 1024:.0f} KB), {result.files_present} already there"
    )
    if result.rotated:
        print(
            f"rotated    {len(result.rotated)} old copies removed, keeping {settings.backup_keep}"
        )
    for note in result.notes:
        print(f"  note: {note}")
    return 0


def _cmd_health(settings: Settings, args: argparse.Namespace) -> int:
    from optscan.console import Console, pad
    from optscan.jobs.health import check, needs_attention

    console = Console.for_stream(settings.color_mode)
    results = check(settings)
    urgent = needs_attention(results)

    if args.quiet:
        for item in urgent:
            print(f"{item.verdict.upper():<9} {item.key:<9} {item.detail}")
        if not urgent:
            print(console.good("Every job is running."))
        return 1 if urgent else 0

    width = max((len(item.key) for item in results), default=8)
    for item in results:
        label = console.paint(item.verdict.value, item.verdict.tone)
        print(f"  {item.key:<{width}}  {pad(label, 9)}  {item.detail}")

    from optscan.jobs.health import Verdict

    print()
    if urgent:
        print(
            console.warn(
                f"{len(urgent)} of {len(results)} jobs need attention. "
                "A day the capture or the recording does not run cannot be filled in later."
            )
        )
    elif any(item.verdict is Verdict.OK for item in results):
        print(console.good("Every job is registered and running on time."))
    else:
        # Nothing is wrong and nothing has been confirmed right either. Saying the
        # first without the second is how a monitor earns trust it has not done
        # anything to deserve.
        print(
            console.dim(
                "Nothing is overdue, and nothing has been observed running yet either. "
                "Each job reports here after its first scheduled run."
            )
        )
    return 1 if urgent else 0


def _cmd_shortcut(settings: Settings, args: argparse.Namespace) -> int:
    from optscan.jobs.launcher import dashboard_url, install_shortcut, remove_shortcut

    if args.remove:
        result = remove_shortcut()
        print(result.message)
        return 0 if result.ok else 1

    result = install_shortcut(settings)
    print(result.message)
    if result.ok:
        print(f"It opens {dashboard_url(settings)} and starts the server if nothing is serving.")
        print("The window it opens is the server. Close it to stop the dashboard.")
        if result.launcher:
            print(f"Launcher: {result.launcher}")
    return 0 if result.ok else 1


def _cmd_schedule(settings: Settings, args: argparse.Namespace) -> int:
    from optscan.console import Console
    from optscan.jobs.schedule import default_keys, describe, install_all

    console = Console.for_stream(settings.color_mode)
    selected = args.jobs or None

    if not args.install:
        print("Recurring jobs, their schedule in market time, and whether Windows has them:\n")
        for line in describe(settings, console):
            print(line)
        print(f"\nInstall with: optscan schedule --install [{' '.join(default_keys(settings))}]")
        return 0

    results = install_all(settings, selected)
    for result in results:
        print(result.message)
    return 0 if all(result.ok for result in results) else 1


def _parse_leg(spec: str, expiry: date, quantity: int):
    """ACTION:RIGHT:STRIKE:FILL into a PositionLeg.

    Four required fields with no defaults. A leg spec that omitted the fill and took
    the mid instead would produce a position whose profit and loss is fiction, so the
    parser refuses rather than helping.
    """
    from optscan.models.position import PositionLeg

    parts = spec.split(":")
    expected = 4
    if len(parts) != expected:
        raise ValueError(
            f"leg {spec!r} must be ACTION:RIGHT:STRIKE:FILL, for example sell:P:700:5.20"
        )
    action, right, strike, fill = parts
    return PositionLeg(
        action=action.strip().lower(),
        right=right,
        strike=float(strike),
        expiry=expiry,
        quantity=quantity,
        fill_price=float(fill),
    )


def _cmd_position(settings: Settings, args: argparse.Namespace) -> int:
    from optscan.models.position import Position
    from optscan.storage import db
    from optscan.storage import positions as store

    with db.session(settings.sqlite_path) as conn:
        if args.position_command == "list":
            held = store.list_positions(conn)
            if not held:
                print("No open positions. Add one with `optscan position add`.")
                return 0
            print(f"{'id':>4}  {'symbol':<6} {'expiry':<10} {'legs':>4}  {'credit':>9}  note")
            for item in held:
                legs = ", ".join(f"{leg.action}:{leg.right}:{leg.strike:g}" for leg in item.legs)
                print(
                    f"{item.id:>4}  {item.symbol:<6} {item.expiry}  {len(item.legs):>4}  "
                    f"{item.entry_credit:>9.2f}  {legs}"
                )
            return 0

        if args.position_command == "add":
            expiry = date.fromisoformat(args.expiry)
            legs = tuple(_parse_leg(spec, expiry, args.quantity) for spec in args.leg)
            stored = store.add_position(
                conn,
                Position(
                    symbol=args.symbol,
                    legs=legs,
                    opened_at=datetime.now(UTC),
                    strategy=args.strategy,
                    commission_open=args.commission,
                    note=args.note,
                ),
            )
            print(f"Recorded position {stored.id}: {stored.symbol} {stored.expiry}")
            print(f"  entry credit {stored.entry_credit:+.2f}, net {stored.net_credit:+.2f}")
            return 0

        if args.position_command == "close":
            closed = store.close_position(conn, args.id, args.value, commission=args.commission)
            if closed is None:
                print(f"No position {args.id}.")
                return 1
            print(f"Closed {args.id}. Realized {closed.realized:+.2f}")
            return 0

        if args.position_command == "delete":
            removed = store.delete_position(conn, args.id)
            print(f"Deleted {args.id}." if removed else f"No position {args.id}.")
            return 0 if removed else 1

    return 1


def _cmd_manage(settings: Settings, args: argparse.Namespace) -> int:
    from optscan.jobs.manage import run_manage, summarize
    from optscan.providers import get_provider
    from optscan.screener.config import DEFAULT_CONFIG_FILENAME, ScreenConfig

    config = ScreenConfig.load(args.config or (REPO_ROOT / DEFAULT_CONFIG_FILENAME))

    provider = None
    if not args.offline:
        try:
            provider = get_provider(settings)
        except Exception as error:
            print(f"Provider unavailable, continuing without it: {error}")

    result = run_manage(settings, config, provider=provider, send_alerts=not args.quiet)

    if not result.portfolio.positions:
        print("No open positions.")
        for note in result.notes:
            print(f"  note: {note}")
        return 0

    print(f"{'id':>4}  {'symbol':<6} {'expiry':<10} {'dte':>5} {'profit':>11}")
    for line in summarize(result):
        print(line)

    book = result.portfolio
    print()
    print(f"  unrealized   {book.unrealized:+.2f}")
    for label, value, unit in (
        ("delta", book.delta, "shares"),
        ("theta", book.theta, "per day"),
        ("vega", book.vega, "per vol point"),
    ):
        print(f"  {label:<12} {value:+.2f} {unit}" if value is not None else f"  {label:<12} n/a")
    weighted = book.beta_weighted_delta
    print(
        f"  beta delta   {weighted:+.0f} dollars of {book.reference}"
        if weighted is not None
        else "  beta delta   n/a"
    )

    if result.alerts:
        print()
        print(f"  {len(result.alerts)} new alerts delivered")
    for note in result.notes:
        print(f"  note: {note}")
    return 0


def _cmd_record(settings: Settings, args: argparse.Namespace) -> int:
    from optscan.jobs.validate import run_record
    from optscan.screener.config import DEFAULT_CONFIG_FILENAME, ScreenConfig

    config = ScreenConfig.load(args.config or (REPO_ROOT / DEFAULT_CONFIG_FILENAME))
    result = run_record(settings, config, symbols=args.symbols, limit=args.limit)

    if result.scan_id is None:
        print("Nothing logged.")
    else:
        print(
            f"Logged {result.recorded} candidates as scan {result.scan_id} "
            f"across {len(result.symbols)} symbols."
        )
    for note in result.notes:
        print(f"  note: {note}")
    return 0


def _cmd_resolve(settings: Settings, args: argparse.Namespace) -> int:
    from optscan.jobs.validate import run_resolve
    from optscan.providers import get_provider

    provider = None
    if not args.offline:
        try:
            provider = get_provider(settings)
        except Exception as error:
            print(f"Provider unavailable: {error}")

    result = run_resolve(settings, provider=provider)
    print(f"Settled {result.resolved}. {result.pending} still waiting.")
    for note in result.notes:
        print(f"  note: {note}")
    return 0


def _cmd_validate(settings: Settings, args: argparse.Namespace) -> int:
    from optscan.analytics.calibration import validate
    from optscan.jobs.validate import next_settlement, status_counts
    from optscan.storage import db
    from optscan.storage import validation as store

    counts = status_counts(settings)
    print(
        f"{counts['logged']} logged over {counts['scans']} scans, "
        f"{counts['resolved']} settled, {counts['pending']} pending."
    )

    with db.session(settings.sqlite_path) as conn:
        report = validate(store.resolved(conn), buckets=args.buckets)

    if report.resolved == 0:
        upcoming = next_settlement(settings)
        print()
        for note in report.notes:
            print(f"  {note}")
        if upcoming:
            print(f"  The earliest logged expiry is {upcoming}, so nothing can settle before then.")
        return 0

    print()
    print(f"  overall win rate  {report.overall_win_rate}")
    print(f"  mean profit       {report.mean_profit:+.2f} per contract")
    print(f"  total profit      {report.total_profit:+.2f}")
    if report.brier is not None:
        print(f"  brier score       {report.brier:.4f}  (0.25 is a coin flip, lower is better)")

    if report.buckets:
        print()
        print(
            f"  {'bucket':<8} {'score':<14} {'n':>4} {'clust':>6} {'win rate':<22} {'mean P/L':>10}"
        )
        for bucket in report.buckets:
            span = f"{bucket.low:.3f}-{bucket.high:.3f}"
            rate = str(bucket.win_rate) if bucket.win_rate else "n/a"
            print(
                f"  {bucket.label:<8} {span:<14} {bucket.count:>4} {bucket.clusters:>6} "
                f"{rate:<22} {bucket.mean_profit:>+10.2f}"
            )

    if report.calibration:
        print()
        print(f"  {'predicted':<12} {'actual':<10} {'n':>4} {'error':>8}")
        for point in report.calibration:
            print(
                f"  {point.predicted:<12.1%} {point.actual:<10.1%} {point.count:>4} "
                f"{point.error:>+8.1%}"
            )
        print("  Positive error means the model was optimistic, which is the expected")
        print("  direction: lognormal tails are thinner than real ones.")

    print()
    if report.scores_separate is None:
        print("  VERDICT: not enough independent data to say whether the score works.")
    elif report.scores_separate:
        print("  VERDICT: the score separates outcomes on this sample.")
    else:
        print("  VERDICT: this sample does not show the score separating outcomes.")

    for note in report.notes:
        print(f"  note: {note}")
    return 0


def _money(value: float, console=None) -> str:
    """A signed dollar figure.

    The sign is written out as well as coloured, because about one man in twelve
    cannot tell the red from the green and a terminal pasted into a chat window
    arrives as plain text.
    """
    text = f"{'+' if value >= 0 else '-'}${abs(value):,.2f}"
    if console is None:
        return text
    return console.good(text) if value >= 0 else console.bad(text)


def _cmd_import(settings: Settings, args: argparse.Namespace) -> int:
    from pathlib import Path

    from optscan.imports import RobinhoodParseError, parse_file
    from optscan.storage import db
    from optscan.storage.ledger import import_transactions

    path = Path(args.path).expanduser()
    if not path.is_file():
        print(f"No such file: {path}")
        return 1

    try:
        txns = parse_file(path)
    except RobinhoodParseError as error:
        # A parse failure names the row and the reason. It is deliberately fatal: a
        # partially imported statement is worse than none, because the profit it
        # produces looks complete.
        print(f"Could not read {path.name}: {error}")
        return 1

    kinds = Counter(str(t.kind) for t in txns)
    print(f"{path.name}: {len(txns)} transaction rows")
    for kind, count in sorted(kinds.items()):
        print(f"  {kind:20s} {count:5d}")

    fees = [t.fee for t in txns if t.fee is not None]
    if fees:
        print(f"  fees measured on {len(fees)} rows, total ${sum(fees):,.2f}")

    if args.dry_run:
        print("\nDry run: nothing was written.")
        return 0

    conn = db.connect(settings.sqlite_path)
    try:
        report = import_transactions(conn, txns)
    finally:
        conn.close()

    print(f"\n{report.summary()}")
    if report.already_known:
        print("Every row was already held. The ledger is unchanged.")
    return 0


def _import_one_history(conn, file: Path, symbol: str | None, *, dry_run: bool) -> bool:
    """Import one export. True when it landed cleanly, False when it needs a human."""
    from optscan.imports.marketchameleon import (
        MarketChameleonParseError,
        parse_file,
        symbol_from_filename,
    )
    from optscan.storage.vendor import import_daily_bars

    symbol = symbol or symbol_from_filename(file)
    if not symbol:
        print(
            f"{file.name}: cannot tell which ticker this is. The Market Chameleon "
            "format has no symbol column, so pass --symbol."
        )
        return False

    try:
        bars = parse_file(file, symbol.upper())
    except MarketChameleonParseError as error:
        # Fatal per file, not per run: one unreadable download should not stop the
        # others in the folder from landing.
        print(f"{file.name}: {error}")
        return False

    with_iv = sum(1 for bar in bars if bar.iv30 is not None)
    print(f"\n{file.name} -> {symbol.upper()}")
    print(
        f"  {len(bars)} sessions, {bars[0].session_date} to {bars[-1].session_date}, "
        f"{with_iv} with an implied vol"
    )
    if dry_run:
        return True

    report = import_daily_bars(conn, bars, file_name=file.name)
    print(f"  {report.summary()}")
    warning = report.warning()
    if warning:
        print(f"  ! {warning}")
        for example in report.conflict_examples:
            print(f"      {example}")
        return False
    if report.already_known:
        print("  Every session was already held. Nothing changed.")
    return True


def _cmd_import_history(settings: Settings, args: argparse.Namespace) -> int:
    from optscan.imports.marketchameleon import SOURCE
    from optscan.storage import db

    path = Path(args.path).expanduser()
    if path.is_dir():
        files = sorted(path.glob("*.csv"))
        if not files:
            print(f"No CSV files in {path}")
            return 1
    elif path.is_file():
        files = [path]
    else:
        print(f"No such file or directory: {path}")
        return 1

    if args.symbol and len(files) > 1:
        # One --symbol cannot describe several files, and applying it to all of them
        # would file every ticker under one name.
        print("--symbol takes a single file, not a directory.")
        return 1

    conn = db.connect(settings.sqlite_path)
    try:
        results = [
            _import_one_history(conn, file, args.symbol, dry_run=args.dry_run) for file in files
        ]
    finally:
        conn.close()

    if args.dry_run:
        print("\nDry run: nothing was written.")
        return 0

    print(f"\nStored as source '{SOURCE}'. Run `optscan history` to see coverage.")
    return 0 if all(results) else 1


def _cmd_prices(settings: Settings, args: argparse.Namespace) -> int:
    from optscan.console import Console, pad
    from optscan.jobs.prices import DEFAULT_DAYS, sync_daily_history
    from optscan.universe import UniverseError, load_universe

    console = Console.for_stream(settings.color_mode)
    try:
        universe = load_universe()
    except UniverseError as error:
        print(f"Could not read the universe: {error}")
        return 1

    if args.prices_command == "groups":
        print(f"{len(universe.groups)} groups, {len(universe.symbols('all'))} unique symbols")
        for name in universe.names:
            print(f"  {pad(name, 16)} {len(universe.groups[name]):4d}")
        stale = universe.staleness_note()
        if stale:
            print(f"\n{console.bad(stale)}")
        elif universe.date_checked:
            print(f"\nLast checked by hand {universe.date_checked}.")
        print(
            "\nThese are curated lists, not index membership. Nothing this tool can "
            "reach publishes sector or index data, so they are only as current as the file."
        )
        return 0

    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols]
        label = f"{len(symbols)} symbol(s)"
    else:
        try:
            symbols = universe.symbols(*(args.groups or []))
        except UniverseError as error:
            print(str(error))
            return 1
        label = ", ".join(args.groups) if args.groups else "every group"

    days = args.days or DEFAULT_DAYS
    print(f"Syncing {len(symbols)} symbols ({label}), {days} days of history.")
    stale = universe.staleness_note()
    if stale:
        print(console.bad(stale))

    report = sync_daily_history(settings, symbols, days=days)
    print(f"\n{report.summary()}")
    warning = report.warning()
    if warning:
        print(f"\n! {warning}")
    print("\nRun `optscan history` to see coverage.")
    return 1 if report.failed else 0


def _cmd_history(settings: Settings, args: argparse.Namespace) -> int:
    from optscan.analytics.ivrank import MIN_OBSERVATIONS
    from optscan.console import Console, pad
    from optscan.storage import db
    from optscan.storage.vendor import coverage

    console = Console.for_stream(settings.color_mode)

    with db.session(settings.sqlite_path) as conn:
        rows = coverage(conn)

    if not rows:
        print("No downloaded vendor history has been imported.")
        print("Get a daily export and run: optscan import-history <file.csv>")
        return 0

    header = ("symbol", "source", "sessions", "with iv", "first", "last", "rank")
    widths = (8, 16, 10, 9, 12, 12, 0)
    print("  ".join(pad(name, width) for name, width in zip(header, widths, strict=True)))

    for row in rows:
        held = row["with_iv30"] or 0
        # Three states, not two. A price-only vendor has no implied vol to rank and
        # never will, which is a different fact from a vol series that is merely too
        # short, and "needs 20" against a source that publishes none reads as a job
        # that has not run yet.
        if held == 0:
            verdict = console.dim("no vol series")
        elif held >= MIN_OBSERVATIONS:
            verdict = console.good("yes")
        else:
            verdict = console.bad(f"needs {MIN_OBSERVATIONS}")
        cells = (
            pad(row["symbol"], 8),
            pad(row["source"], 16),
            pad(str(row["sessions"]), 10),
            pad(str(row["with_iv30"] or 0), 9),
            pad(row["first_session"], 12),
            pad(row["last_session"], 12),
            verdict,
        )
        print("  ".join(cells))

    return 0


def _cmd_trades(settings: Settings, args: argparse.Namespace) -> int:
    from optscan.analytics.ledger import build_trades, summarize
    from optscan.console import Console, pad
    from optscan.storage import db
    from optscan.storage.ledger import all_transactions

    console = Console.for_stream(settings.color_mode)

    with db.session(settings.sqlite_path) as conn:
        txns = all_transactions(conn)

    if not txns:
        print("The broker ledger is empty. Import a statement with `optscan import`.")
        return 0

    trades = build_trades(txns)
    summary = summarize(txns, trades)

    if args.symbol:
        wanted = args.symbol.strip().upper()
        trades = [t for t in trades if t.key.symbol == wanted]

    print(f"Ledger: {len(txns)} rows from imported statements\n")
    print(
        f"  options   {summary.option_trades:4d} closed   "
        f"{_money(summary.option_realized, console)}   fees ${summary.option_fees:,.2f}"
    )
    print(
        f"  equities  {summary.equity_trades:4d} closed   "
        f"{_money(summary.equity_realized, console)}"
    )
    # Open positions are excluded rather than counted at whatever cash they have taken
    # in so far, which is how an unclosed loser reads as a winner.
    print(f"  open      {summary.open_trades:4d}          excluded from the figures above")
    if summary.cash_flows:
        flows = ", ".join(f"{k} ${v:,.2f}" for k, v in summary.cash_flows.items())
        print(f"  cash movements, not results: {flows}")

    closed = [t for t in trades if not t.open_at_end]
    closed.sort(key=lambda t: t.closed_at or t.opened_at, reverse=True)
    if closed:
        print(f"\n  {'closed':<12}{'contract':<32}{'held':>5}  result")
        for trade in closed[: args.limit]:
            held = "" if trade.held_days is None else f"{trade.held_days}d"
            # pad, not an f-string width: padding counts the escape sequence and
            # silently misaligns every coloured column.
            print(
                f"  {trade.closed_at!s:<12}{trade.key!s:<32}{held:>5}  "
                + pad(_money(trade.cash, console), 12)
            )
        if len(closed) > args.limit:
            print(f"  ... {len(closed) - args.limit} more")
    return 0


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
        "serve": _cmd_serve,
        "position": _cmd_position,
        "manage": _cmd_manage,
        "record": _cmd_record,
        "resolve": _cmd_resolve,
        "validate": _cmd_validate,
        "backup": _cmd_backup,
        "shortcut": _cmd_shortcut,
        "health": _cmd_health,
        "import": _cmd_import,
        "import-history": _cmd_import_history,
        "prices": _cmd_prices,
        "history": _cmd_history,
        "trades": _cmd_trades,
    }
    handler = handlers[args.command]

    if args.command not in TRACKED_JOBS:
        return handler(settings, args)

    # Logged whoever started it. A hole filled by hand is filled, so the health report
    # asks whether the work happened rather than who asked for it.
    from optscan.storage import jobs as job_log

    with job_log.track(settings.sqlite_path, args.command) as run:
        code = handler(settings, args)
        if code != 0:
            # A non-zero return is a failure the job reported about itself, and it has
            # to reach the log the same way an exception does. Otherwise the most likely
            # way a job goes wrong is the one way health cannot see.
            run.failed(f"the command exited {code}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
