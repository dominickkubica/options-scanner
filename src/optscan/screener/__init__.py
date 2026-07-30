"""Strategy candidate generation, filtering, and scoring.

The pipeline is: solve the surface (context), enumerate what could be sold
(strategies), drop what does not qualify (filters), rank what is left (scoring), and
separately look for places the surface disagrees with itself (gaps).

Thresholds all come from ScreenConfig, which is loaded from YAML. Nothing in this
package hardcodes a number a user might want to change.
"""

from optscan.screener.config import ScreenConfig
from optscan.screener.context import ExpiryAnalysis, SymbolAnalysis, analyze_snapshot
from optscan.screener.filters import Rejection, RejectionTally
from optscan.screener.gaps import Gap, GapKind, find_gaps
from optscan.screener.scan import ScanResult, scan_analysis, scan_snapshot, scan_snapshots
from optscan.screener.scoring import score_candidate

__all__ = [
    "ExpiryAnalysis",
    "Gap",
    "GapKind",
    "Rejection",
    "RejectionTally",
    "ScanResult",
    "ScreenConfig",
    "SymbolAnalysis",
    "analyze_snapshot",
    "find_gaps",
    "scan_analysis",
    "scan_snapshot",
    "scan_snapshots",
    "score_candidate",
]
