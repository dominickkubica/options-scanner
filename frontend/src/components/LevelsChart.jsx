import { useMemo, useState } from "react";
import { num, pct, vol } from "../format.js";

// The one chart Phase 6 exists to produce: price, the levels it respected, the expected
// move cone projected forward, and the candidate strikes, on one set of axes.
//
// Hand rolled SVG for the same reason the other charts here are. This one draws in two
// time regions on a shared price axis, with history on the left and projection on the
// right of a "today" divider, and no charting library models that without a fight.
//
// Two rules the drawing has to keep, both of which are easy to break by accident:
//
//   - **A level with weak evidence must not look like a strong one.** Line weight and
//     opacity track the p-value, and the legend says what the kinds mean, because the
//     strengths are not comparable across kinds.
//   - **The cone is not a forecast.** It is drawn as a translucent band with no
//     centre line, because a centre line reads as an expected path and the model has
//     nothing to say about direction.

const PADDING = { top: 16, right: 128, bottom: 28, left: 8 };
const HEIGHT = 380;

// Fraction of the plot given to the projection. The cone is the point of the panel,
// so it gets real room rather than a sliver at the right edge.
const PROJECTION_SHARE = 0.32;

const KIND_STYLE = {
  swing_high: { colour: "var(--bad, #c85f5f)", label: "swing high" },
  swing_low: { colour: "var(--good, #4f9d69)", label: "swing low" },
  point_of_control: { colour: "#c9a227", label: "point of control" },
  value_area: { colour: "#8a7a2f", label: "value area" },
  volume_node: { colour: "#6b7a8f", label: "volume node" },
  round_number: { colour: "#4a4a52", label: "round number" },
};

// Probability of profit to a colour. Deliberately not a red-to-green ramp: a high POP
// is not "good", it is a high probability of a small credit, and colouring it as a
// verdict would be the chart making a recommendation it has no business making.
function popColour(pop) {
  if (pop === null || pop === undefined) return "var(--dim)";
  const clamped = Math.min(Math.max(pop, 0), 1);
  // Cold to warm through blue: darker is a lower probability of profit.
  const light = 30 + clamped * 40;
  return `hsl(205, 65%, ${light}%)`;
}

export default function LevelsChart({ data, width = 900 }) {
  const [hovered, setHovered] = useState(null);

  const geometry = useMemo(() => {
    if (!data || !data.bars || data.bars.length === 0) return null;

    const bars = data.bars;
    const plotWidth = width - PADDING.left - PADDING.right;
    const plotHeight = HEIGHT - PADDING.top - PADDING.bottom;
    const historyWidth = plotWidth * (1 - PROJECTION_SHARE);

    // The price axis has to hold the bars, every level, and the widest cone band, or
    // the cone gets clipped at exactly the moment it matters.
    const candidates = [
      ...bars.map((bar) => bar.high),
      ...bars.map((bar) => bar.low),
      ...data.levels.map((level) => level.price),
      ...data.cone.flatMap((point) => point.bands.flatMap((band) => [band.low, band.high])),
      ...data.candidates.map((item) => item.strike),
    ].filter((value) => Number.isFinite(value));

    if (candidates.length === 0) return null;
    const rawLow = Math.min(...candidates);
    const rawHigh = Math.max(...candidates);
    const pad = (rawHigh - rawLow) * 0.04 || 1;
    const low = rawLow - pad;
    const high = rawHigh + pad;

    const y = (price) => PADDING.top + ((high - price) / (high - low)) * plotHeight;
    const xHistory = (index) =>
      PADDING.left + (index / Math.max(bars.length - 1, 1)) * historyWidth;

    const maxDte = Math.max(...data.cone.map((point) => point.dte), 1);
    const xProjection = (dte) =>
      PADDING.left + historyWidth + (dte / maxDte) * (plotWidth - historyWidth);

    return { bars, low, high, y, xHistory, xProjection, plotWidth, plotHeight, historyWidth };
  }, [data, width]);

  if (!geometry) {
    return <div className="chart-note">No price history, so there is nothing to draw.</div>;
  }

  const { bars, y, xHistory, xProjection, plotHeight, historyWidth } = geometry;
  const spotY = y(data.spot);

  const closeLine = bars
    .map((bar, index) => `${index === 0 ? "M" : "L"} ${xHistory(index)} ${y(bar.close)}`)
    .join(" ");

  return (
    <div>
      <svg viewBox={`0 0 ${width} ${HEIGHT}`} style={{ width: "100%", height: "auto" }}>
        {/* The cone, drawn first so price and levels sit on top of it. Widest band
            first so the one sigma band reads as denser than the two sigma one. */}
        {[...data.cone[0]?.bands ?? []]
          .sort((a, b) => b.deviations - a.deviations)
          .map((band) => {
            const deviations = band.deviations;
            const upper = data.cone
              .map((point) => {
                const match = point.bands.find((item) => item.deviations === deviations);
                return match ? `${xProjection(point.dte)},${y(match.high)}` : null;
              })
              .filter(Boolean);
            const lower = data.cone
              .map((point) => {
                const match = point.bands.find((item) => item.deviations === deviations);
                return match ? `${xProjection(point.dte)},${y(match.low)}` : null;
              })
              .filter(Boolean)
              .reverse();
            if (upper.length === 0) return null;
            const start = `${PADDING.left + historyWidth},${spotY}`;
            return (
              <polygon
                key={deviations}
                points={`${start} ${upper.join(" ")} ${lower.join(" ")}`}
                fill="var(--accent, #4f7fbf)"
                opacity={deviations >= 2 ? 0.08 : 0.16}
                stroke="none"
              />
            );
          })}

        {/* Levels, spanning the whole width because a level is a price and does not
            stop being one when the chart crosses into projection. */}
        {data.levels.map((level, index) => {
          const style = KIND_STYLE[level.kind] || { colour: "var(--dim)" };
          // Weak evidence must not look like strong evidence. Round numbers and
          // volume nodes have no p-value at all and are drawn faintest.
          const confidence = level.p_value === null ? 0.3 : 1 - level.p_value;
          return (
            <g key={`${level.kind}-${index}`}>
              <line
                x1={PADDING.left}
                x2={width - PADDING.right}
                y1={y(level.price)}
                y2={y(level.price)}
                stroke={style.colour}
                strokeWidth={level.kind.startsWith("swing") ? 1.4 : 1}
                strokeDasharray={level.p_value === null ? "3 4" : undefined}
                opacity={0.25 + 0.55 * confidence}
              />
              <text x={width - PADDING.right + 4} y={y(level.price) + 3} fontSize="9">
                {num(level.price, 0)}
              </text>
            </g>
          );
        })}

        {/* Price. A close line rather than candles: this panel is about where price
            sat relative to levels over a year, and a year of candles at this width is
            a smear. The Underlying view has the candles. */}
        <path d={closeLine} fill="none" stroke="var(--text, #d8d8e0)" strokeWidth="1.3" />

        {/* The divider between what happened and what is projected. */}
        <line
          x1={PADDING.left + historyWidth}
          x2={PADDING.left + historyWidth}
          y1={PADDING.top}
          y2={PADDING.top + plotHeight}
          stroke="var(--line)"
          strokeDasharray="2 3"
        />
        <text
          x={PADDING.left + historyWidth + 4}
          y={PADDING.top + 10}
          fontSize="9"
          fill="var(--dim)"
        >
          today
        </text>

        {/* Candidate strikes, on the projection side only, because a strike is a
            statement about the future and drawing it back across history would
            suggest it meant something then. */}
        {data.candidates.map((item, index) => (
          <g
            key={`${item.strike}-${item.right}-${index}`}
            onMouseEnter={() => setHovered(item)}
            onMouseLeave={() => setHovered(null)}
          >
            <line
              x1={PADDING.left + historyWidth}
              x2={width - PADDING.right}
              y1={y(item.strike)}
              y2={y(item.strike)}
              stroke={popColour(item.probability_of_profit)}
              strokeWidth={hovered === item ? 3 : 2}
              opacity={0.9}
            />
            <text
              x={width - PADDING.right + 4}
              y={y(item.strike) + 3}
              fontSize="9"
              fill={popColour(item.probability_of_profit)}
            >
              {num(item.strike, 0)}
              {item.right} {pct(item.probability_of_profit, 0)}
            </text>
          </g>
        ))}

        {/* Spot last, so it is never hidden under a level. */}
        <line
          x1={PADDING.left}
          x2={width - PADDING.right}
          y1={spotY}
          y2={spotY}
          stroke="var(--accent, #4f7fbf)"
          strokeWidth="1"
        />
        <text x={width - PADDING.right + 4} y={spotY - 4} fontSize="9" fill="var(--accent)">
          spot {num(data.spot)}
        </text>
      </svg>

      {hovered && (
        <div className="chart-note">
          {hovered.strategy} {num(hovered.strike, 0)}
          {hovered.right} expiring {hovered.expiry} ({hovered.dte}d): credit{" "}
          {num(hovered.credit)}, probability of profit {pct(hovered.probability_of_profit)},
          short delta {num(hovered.short_delta, 2)}
        </div>
      )}

      <div className="legend">
        {Object.entries(KIND_STYLE)
          .filter(([kind]) => data.levels.some((level) => level.kind === kind))
          .map(([kind, style]) => (
            <span key={kind}>
              <span
                className="swatch"
                style={{ background: style.colour, opacity: 0.8 }}
              />
              {style.label}
            </span>
          ))}
        {data.candidates.length > 0 && (
          <span>
            <span className="swatch" style={{ background: popColour(0.8) }} />
            candidate strike, shaded by probability of profit
          </span>
        )}
      </div>

      {data.candidates.length > 0 && (
        <div className="chart-note">
          A strike&apos;s shade is the probability of profit of the position the screener
          built around it, not of the strike on its own. Two neighbouring strikes can
          therefore read quite differently when one is a spread and the other is a
          single leg, which is a fact about the positions rather than about the market.
          Hover a line to see which strategy it came from.
        </div>
      )}

      <div className="chart-note">
        The cone uses each expiry&apos;s own implied volatility, so its shape follows the
        term structure rather than one volatility stretched over a square root of time.
        It is drawn without a centre line on purpose: it says how wide, not which way.
        {data.realized_vol !== null && data.implied_vol !== null && (
          <>
            {" "}
            Implied {vol(data.implied_vol)} against realized {vol(data.realized_vol)} gives
            a variance risk premium of {vol(data.variance_risk_premium)} vol points.
          </>
        )}
      </div>
    </div>
  );
}
