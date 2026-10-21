import { useMemo } from "react";
import { useMeasuredWidth } from "./chart/plot.jsx";
import { compact, num, pct } from "../format.js";

// Open interest by strike, calls against puts, and the gamma exposure that follows.
//
// ## Why this chart is horizontal and the price charts are not
//
// The axis down the middle is a strike ladder, not time. Every other chart in the app
// reads left to right because its x axis is a clock; this one reads top to bottom
// because a reader is looking for where a price sits relative to a level, and that is
// the same orientation an option chain is quoted in. Turning it on its side to match
// the price chart would put strikes on the x axis and force the reader to rotate the
// whole ladder in their head before comparing it to a quote screen.
//
// ## Two views of the same strikes
//
// Open interest is a count and is drawn mirrored: puts left, calls right, both as
// positive lengths, because "more puts than calls here" is the question. Net GEX is
// signed and is drawn from a centre line, because there the sign is the whole point.
// They are not overlaid: one is contracts and the other is dollars per percent, and a
// shared axis would imply a relationship between the two scales that does not exist.
//
// ## The band
//
// Only strikes within `band` of spot are drawn. A full ladder runs from 300 to 1000
// against a 760 spot and the deep wings hold enough open interest to flatten everything
// near the money into invisibility -- which is exactly how a chart hides the strikes it
// was drawn to show. A level outside the band is not drawn at the edge; it is named
// underneath instead, because a line at the boundary would be read as a level there.

const ROW_HEIGHT = 15;
const MIN_HEIGHT = 200;
const MARGIN = { top: 16, right: 84, bottom: 26, left: 62 };

//: The lines drawn over the bars, in the order they are labelled underneath. Spot is
//: not in here: it is the reference every other line is measured against and it is
//: drawn differently on purpose.
const LEVEL_LINES = [
  { key: "call_wall", label: "call wall", colour: "var(--chart-up)" },
  { key: "put_wall", label: "put wall", colour: "var(--chart-down)" },
  { key: "zero_gamma", label: "zero gamma", colour: "var(--chart-ma-slow)" },
  { key: "pin", label: "pin", colour: "var(--chart-ma-fast)" },
];

export default function OiGexChart({ data, view = "oi", band = 0.03 }) {
  const [ref, width] = useMeasuredWidth();

  const { rows, hidden } = useMemo(() => {
    const low = data.spot * (1 - band);
    const high = data.spot * (1 + band);
    const inside = data.strikes.filter((row) => row.strike >= low && row.strike <= high);
    return { rows: inside, hidden: data.strikes.length - inside.length };
  }, [data.strikes, data.spot, band]);

  if (rows.length === 0) {
    return (
      <div ref={ref} className="empty-state">
        No listed strike falls within {pct(band, 0)} of spot, so there is nothing to draw
        here. The levels above are still computed from the whole chain.
      </div>
    );
  }

  const height = Math.max(MIN_HEIGHT, rows.length * ROW_HEIGHT + MARGIN.top + MARGIN.bottom);
  const plotLeft = MARGIN.left;
  const plotRight = Math.max(width - MARGIN.right, plotLeft + 60);
  const plotTop = MARGIN.top;
  const plotBottom = height - MARGIN.bottom;
  const centre = (plotLeft + plotRight) / 2;

  // Strikes run high at the top, the way a chain is quoted.
  const ordered = [...rows].sort((a, b) => b.strike - a.strike);
  const bandLow = Math.min(...ordered.map((row) => row.strike));
  const bandHigh = Math.max(...ordered.map((row) => row.strike));
  const span = bandHigh - bandLow || 1;
  const toY = (strike) => plotTop + ((bandHigh - strike) / span) * (plotBottom - plotTop);

  const gex = view === "gex";
  const peak = gex
    ? Math.max(...ordered.map((row) => Math.abs(row.net_gex ?? 0)), 0)
    : Math.max(...ordered.map((row) => Math.max(row.call_oi, row.put_oi)), 0);
  const halfWidth = (plotRight - plotLeft) / 2;
  const toLength = (value) => (peak > 0 ? (Math.abs(value) / peak) * halfWidth : 0);

  const rowHeight = Math.max((plotBottom - plotTop) / Math.max(ordered.length, 1) - 1.5, 2);

  return (
    <div ref={ref} className="plot">
      <svg viewBox={`0 0 ${width} ${height}`} width="100%" height={height} role="img">
        <title>
          {gex ? "Net gamma exposure" : "Open interest"} by strike for {data.symbol},{" "}
          {data.expiry}
        </title>

        {ordered.map((row) => {
          const y = toY(row.strike) - rowHeight / 2;
          const bars = gex ? gexBars(row) : oiBars(row);
          return (
            <g key={row.strike}>
              {bars.map((bar) => {
                const length = toLength(bar.value);
                if (!Number.isFinite(length) || length <= 0) return null;
                const x = bar.side === "left" ? centre - length : centre;
                return (
                  <rect
                    key={bar.key}
                    x={x}
                    y={y}
                    width={length}
                    height={rowHeight}
                    fill={bar.colour}
                    opacity={0.85}
                  >
                    <title>
                      {num(row.strike)} {bar.label}: {bar.format(bar.value)}
                    </title>
                  </rect>
                );
              })}
              {/* Unknown is drawn as nothing, and a tick says the strike was here and
                  could not be priced. A zero length bar would read as a flat strike. */}
              {gex && row.net_gex === null && row.call_oi + row.put_oi > 0 && (
                <rect x={centre - 3} y={y} width={6} height={rowHeight} fill="var(--line-strong)">
                  <title>
                    {num(row.strike)}: {compact(row.call_oi + row.put_oi)} contracts held, no
                    implied vol to price gamma with
                  </title>
                </rect>
              )}
            </g>
          );
        })}

        {/* Strike labels, thinned so they never collide at a tight ladder. */}
        {ordered.map((row, index) => {
          const every = Math.ceil((ordered.length * 11) / Math.max(plotBottom - plotTop, 1));
          if (index % Math.max(every, 1) !== 0) return null;
          return (
            <text
              key={`label-${row.strike}`}
              x={plotLeft - 6}
              y={toY(row.strike) + 3}
              textAnchor="end"
              fill="var(--chart-axis)"
              style={{ fontSize: 9 }}
            >
              {num(row.strike, row.strike >= 1000 ? 0 : 1)}
            </text>
          );
        })}

        <line
          x1={centre}
          x2={centre}
          y1={plotTop}
          y2={plotBottom}
          stroke="var(--line-strong)"
          strokeWidth="1"
        />

        {LEVEL_LINES.map((line) => {
          const level = data[line.key];
          if (!level || level.price < bandLow || level.price > bandHigh) return null;
          return (
            <g key={line.key}>
              <line
                x1={plotLeft}
                x2={plotRight}
                y1={toY(level.price)}
                y2={toY(level.price)}
                stroke={line.colour}
                strokeWidth="1"
                strokeDasharray="4 3"
                opacity={0.9}
              />
              <text
                x={plotRight + 4}
                y={toY(level.price) + 3}
                fill={line.colour}
                style={{ fontSize: 9 }}
              >
                {line.label}
              </text>
            </g>
          );
        })}

        {/* Spot last, so it is never hidden under a level that happens to sit on it. */}
        <line
          x1={plotLeft}
          x2={plotRight}
          y1={toY(data.spot)}
          y2={toY(data.spot)}
          stroke="var(--text)"
          strokeWidth="1.5"
        />
        <text x={plotRight + 4} y={toY(data.spot) + 3} fill="var(--text)" style={{ fontSize: 9 }}>
          spot
        </text>

        <text x={plotLeft} y={height - 8} fill="var(--chart-axis)" style={{ fontSize: 9 }}>
          {gex ? "negative gamma" : "put OI"}
        </text>
        <text
          x={plotRight}
          y={height - 8}
          textAnchor="end"
          fill="var(--chart-axis)"
          style={{ fontSize: 9 }}
        >
          {gex ? "positive gamma" : "call OI"}
        </text>
      </svg>

      {hidden > 0 && (
        <div className="chart-note">
          {hidden} listed {hidden === 1 ? "strike sits" : "strikes sit"} outside{" "}
          {pct(band, 0)} of spot and {hidden === 1 ? "is" : "are"} not drawn. They are still
          counted in every level above.
        </div>
      )}
    </div>
  );
}

function oiBars(row) {
  return [
    {
      key: "put",
      side: "left",
      value: row.put_oi,
      colour: "var(--chart-down)",
      label: "put OI",
      format: (value) => `${compact(value)} contracts`,
    },
    {
      key: "call",
      side: "right",
      value: row.call_oi,
      colour: "var(--chart-up)",
      label: "call OI",
      format: (value) => `${compact(value)} contracts`,
    },
  ];
}

function gexBars(row) {
  if (row.net_gex === null || row.net_gex === undefined) return [];
  return [
    {
      key: "gex",
      side: row.net_gex < 0 ? "left" : "right",
      value: row.net_gex,
      colour: row.net_gex < 0 ? "var(--chart-down)" : "var(--chart-up)",
      label: "net gamma",
      format: (value) => `${value < 0 ? "-" : ""}$${compact(Math.abs(value))} per 1% move`,
    },
  ];
}
