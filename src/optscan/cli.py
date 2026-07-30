"""Command line entry point.

Phase 0 only boots the app: load config, configure logging, create data dirs, and
report what it is configured to do. Scanning arrives in Phase 3.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence

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
    return parser


def startup(settings: Settings, *, create_dirs: bool = True) -> None:
    """Boot sequence shared by the CLI and, later, the API server."""
    configure_logging(settings)
    log = get_logger("optscan.startup")

    if create_dirs:
        settings.ensure_dirs()

    log.info("optscan starting", version=__version__, **settings.safe_summary())


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = get_settings()
    startup(settings, create_dirs=not args.check)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
