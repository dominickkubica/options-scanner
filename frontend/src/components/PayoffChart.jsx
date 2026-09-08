import { useCallback, useMemo, useState } from "react";
import { Plot, PlotReadout, linePath } from "./chart/plot.jsx";
import { money, num } from "../format.js";

// Profit and loss against underlying price.
//
// Rebuilt on the shared plot so it gets what the price chart already had and this did
// not: a real width rather than a 700 pixel viewBox scaled to fit, and a readout that
// answers the question the chart exists for. Hovering a payoff diagram is asking "what
// do I make if it finishes here", and before this the answer had to be estimated off
// the axis by eye.
//
// The two curves are the point of the panel and are kept distinct by weight as well as
// colour: solid for expiry, dashed for today. A colourblind reader gets the same
// separation, and the legend below names both.

export default function PayoffChart({ payoff, height = 300 }) {
  const [hover, setHover] = useState(null);
  const points = payoff.points || [];

  const xs = useMemo(() => points.map((point) => point.price), [points]);
  const ys = useMemo(
    () => [
      ...points.map((point) => point.at_expiry),
      ...points.map((point) => point.at_now).filter((value) => value !== null),
      // Zero is always in range: a payoff chart that cropped the breakeven line out
      // would hide the only level on it that matters.
      0,
    ],
    [points],
  );

  const onHoverX = useCallback(
    (price) => {
      if (price === null || points.length === 0) {
        setHover(null);
        return;
      }
      // Nearest sampled point rather than an interpolation. The curve has kinks at
      // every strike, and interpolating across one would report a profit the position
      // cannot actually produce.
      let best = points[0];
      for (const point of points) {
        if (Math.abs(point.price - price) < Math.abs(best.price - price)) best = point;
      }
      setHover(best);
    },
    [points],
  );

  const shown = hover || null;

  return (
    <div>
      <PlotReadout
        items={[
          { label: "at", value: shown ? num(shown.price) : num(payoff.spot) },
          {
            label: "expiry",
            value: shown ? money(shown.at_expiry, 0) : "hover",
            tone: shown ? (shown.at_expiry >= 0 ? "up" : "down") : undefined,
          },
          {
            label: "now",
            value: shown && shown.at_now !== null ? money(shown.at_now, 0) : "n/a",
            tone:
              shown && shown.at_now !== null ? (shown.at_now >= 0 ? "up" : "down") : undefined,
          },
        ]}
      />

      <Plot
        height={height}
        xs={xs}
        ys={ys}
        formatY={(value) => `$${value.toFixed(0)}`}
        onHoverX={onHoverX}
        empty="No payoff to draw. The position needs at least one priced leg."
      >
        {({ scale }) => (
          <>
            {/* Breakeven. Drawn first so every curve sits on top of it. */}
            <line
              x1={scale.left}
              x2={scale.right}
              y1={scale.toY(0)}
              y2={scale.toY(0)}
              stroke="var(--chart-axis)"
              strokeWidth="1"
            />

            <line
              x1={scale.toX(payoff.spot)}
              x2={scale.toX(payoff.spot)}
              y1={scale.top}
              y2={scale.bottom}
              stroke="var(--accent)"
              strokeDasharray="3 3"
            />
            <text
              x={scale.toX(payoff.spot) + 4}
              y={scale.top + 10}
              fill="var(--accent)"
              style={{ fontSize: 10 }}
            >
              spot {num(payoff.spot)}
            </text>

            {(payoff.breakevens || []).map((value) => (
              <g key={value}>
                <line
                  x1={scale.toX(value)}
                  x2={scale.toX(value)}
                  y1={scale.top}
                  y2={scale.bottom}
                  stroke="var(--warn)"
                  strokeDasharray="1 4"
                />
                <text
                  x={scale.toX(value) + 4}
                  y={scale.bottom - 4}
                  fill="var(--warn)"
                  style={{ fontSize: 10 }}
                >
                  BE {num(value)}
                </text>
              </g>
            ))}

            <path
              d={linePath(
                points
                  .filter((point) => point.at_now !== null)
                  .map((point) => ({ x: scale.toX(point.price), y: scale.toY(point.at_now) })),
              )}
              fill="none"
              stroke="var(--muted)"
              strokeWidth="1.5"
              strokeDasharray="4 3"
            />
            <path
              d={linePath(
                points.map((point) => ({
                  x: scale.toX(point.price),
                  y: scale.toY(point.at_expiry),
                })),
              )}
              fill="none"
              stroke="var(--chart-up)"
              strokeWidth="2"
            />

            {shown && (
              <circle
                cx={scale.toX(shown.price)}
                cy={scale.toY(shown.at_expiry)}
                r="3.5"
                fill="var(--chart-up)"
              />
            )}
          </>
        )}
      </Plot>

      <div className="chart-note">
        Solid: at expiry. Dashed: at T plus zero, repriced at each leg&apos;s own entry
        volatility. A short premium position sits below its expiry curve for most of its
        life, which is the shape of being paid to wait.
      </div>
    </div>
  );
}
