import Gex from "./Gex.jsx";
import Levels from "./Levels.jsx";
import Signals from "./Signals.jsx";

// Key levels, and what to look out for on this symbol.
//
// Two panels that were separate views and answer halves of one question. Levels says
// where price has been defended; signals says what is true about it right now. Reading
// either without the other is the usual mistake: a level with no condition attached is
// a line on a chart, and a condition with no level is a number with nowhere to act.
//
// Both are scoped to the selected symbol here. The signals view is also reachable
// across the whole watchlist from Trade ideas, which is the "which symbol should I look
// at" question rather than this one.
//
// The gamma panel sits between them and does not need `summary`, which is the point of
// where it is wired: it fetches its own chain by symbol, so it is the only thing on this
// tab that works for an index the nightly capture has never touched. `^SPX` has no
// stored snapshot and never will, and the panel still draws.
export default function KeyLevels({ symbol, summary }) {
  return (
    <>
      {summary ? (
        <Levels summary={summary} />
      ) : (
        <div className="empty-state">
          No option chain has been captured for {symbol}, so the level analysis that
          reads the surface is not available. The price levels below still are.
        </div>
      )}
      <Gex symbol={symbol} />
      <Signals symbol={symbol} />
    </>
  );
}
