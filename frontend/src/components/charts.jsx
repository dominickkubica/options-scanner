// Hand rolled SVG charts.
//
// No charting library for these three. They are line plots over a hundred points with
// one or two reference lines, and the shapes are exactly the ones a library makes
// awkward: a payoff diagram needs the zero line to be a real axis rather than the
// bottom of the plot, and a skew curve is plotted against strike with spot marked on
// it. Candles are the exception and use lightweight-charts, which is what it is for.
//
// Everything here draws only what it was given. A null point is a gap in the line,
// not a zero, which is why the path builder breaks the line rather than bridging it.

import { pct, vol } from "../format.js";

export function IvRankGauge({ ivRank, note }) {
  const width = 260;
  const height = 96;
  const barY = 42;
  const barH = 14;
  const left = 16;
  const right = width - 16;

  if (!ivRank) {
    // Two different absences, and they need different sentences. A tenor mismatch is
    // about this capture and no amount of waiting fixes it; a missing history is
    // about this symbol and one download fixes it. Saying "needs months of captures"
    // for both was true until vendor exports could be imported, and is now advice
    // that would leave someone waiting for something they can have in a minute.
    return (
      <div className="empty-state">
        {note || (
          <>
            No volatility history for this symbol yet. Import a vendor daily export with{" "}
            <code>optscan import-history</code>, or wait for enough daily captures to
            accumulate.
          </>
        )}
      </div>
    );
  }

  const usable = ivRank.rank !== null && ivRank.rank !== undefined;

  return (
    <div>
      <svg viewBox={`0 0 ${width} ${height}`} width="100%" height={height} role="img">
        <rect x={left} y={barY} width={right - left} height={barH} rx="3" fill="var(--panel-2)" />
        {usable && (
          <>
            <rect
              x={left}
              y={barY}
              width={(right - left) * ivRank.rank}
              height={barH}
              rx="3"
              fill="var(--accent)"
            />
            <text x={left} y={barY - 8} fill="var(--text)" style={{ fontSize: 15 }}>
              {pct(ivRank.rank, 0)} rank
            </text>
            <text x={right} y={barY - 8} textAnchor="end">
              {pct(ivRank.percentile, 0)} percentile
            </text>
          </>
        )}
        {!usable && (
          <text x={left} y={barY - 8} fill="var(--muted)" style={{ fontSize: 14 }}>
            no rank published
          </text>
        )}
        <text x={left} y={barY + barH + 14}>
          IV {vol(ivRank.iv)} vol points, confidence {ivRank.confidence},{" "}
          {ivRank.observations} observations
        </text>
      </svg>
      {ivRank.caveat && <div className="chart-note">{ivRank.caveat}</div>}
    </div>
  );
}


// Re-exported so no view had to change its imports when these moved onto the shared
// plot. PayoffChart and the two volatility charts now live in their own files because
// each grew a readout and a hover model of its own, and three of those in one module
// was the file becoming a junk drawer.
export { default as PayoffChart } from "./PayoffChart.jsx";
export { SkewCurve, TermStructure } from "./VolCharts.jsx";
