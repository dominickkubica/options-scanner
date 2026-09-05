"""HTTP routers. One module per group of related endpoints.

Every router is thin: resolve dependencies, call into a module that already had tests
before the API existed, convert with optscan.api.views. No analytics live here.
"""

from optscan.api.routers import health, journal, payoff, scan, symbols, watchlist

__all__ = ["health", "journal", "payoff", "scan", "symbols", "watchlist"]
