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

import { num, pct, vol } from "../format.js";

const PAD = { top: 12, right: 14, bottom: 26, left: 46 };

function scales(width, height, xs, ys) {
  const usableX = xs.filter((value) => value !== null && value !== undefined);
  const usableY = ys.filter((value) => value !== null && value !== undefined);
  const xMin = Math.min(...usableX);
  const xMax = Math.max(...usableX);
  let yMin = Math.min(...usableY);
  let yMax = Math.max(...usableY);
  if (yMin === yMax) {
    yMin -= 1;
    yMax += 1;
  }
  const padY = (yMax - yMin) * 0.08;
  yMin -= padY;
  yMax += padY;

  const plotW = width - PAD.left - PAD.right;
  const plotH = height - PAD.top - PAD.bottom;
  return {
    x: (value) => PAD.left + ((value - xMin) / (xMax - xMin || 1)) * plotW,
    y: (value) => PAD.top + plotH - ((value - yMin) / (yMax - yMin || 1)) * plotH,
    xMin,
    xMax,
    yMin,
    yMax,
    plotW,
    plotH,
  };
}

// Breaks the path on a null instead of bridging it. A bridged gap draws a straight
// line through data that does not exist, which is the chart equivalent of rendering
// an unknown as zero.
function linePath(points, scale) {
  let path = "";
  let open = false;
  for (const [px, py] of points) {
    if (py === null || py === undefined) {
      open = false;
      continue;
    }
    path += `${open ? "L" : "M"}${scale.x(px).toFixed(2)},${scale.y(py).toFixed(2)}`;
    open = true;
  }
  return path;
}

function Axes({ scale, width, height, formatY, formatX, ticks = 4 }) {
  const yTicks = [];
  for (let i = 0; i <= ticks; i += 1) {
    const value = scale.yMin + ((scale.yMax - scale.yMin) * i) / ticks;
    yTicks.push(value);
  }
  const xTicks = [scale.xMin, (scale.xMin + scale.xMax) / 2, scale.xMax];

  return (
    <g>
      {yTicks.map((value) => (
        <g key={`y${value}`}>
          <line
            x1={PAD.left}
            x2={width - PAD.right}
            y1={scale.y(value)}
            y2={scale.y(value)}
            stroke="#262e39"
            strokeDasharray="2 3"
          />
          <text x={PAD.left - 6} y={scale.y(value) + 3} textAnchor="end">
            {formatY(value)}
          </text>
        </g>
      ))}
      {xTicks.map((value, index) => (
        <text
          key={`x${index}`}
          x={scale.x(value)}
          y={height - 8}
          textAnchor={index === 0 ? "start" : index === xTicks.length - 1 ? "end" : "middle"}
        >
          {formatX(value)}
        </text>
      ))}
    </g>
  );
}

export function PayoffChart({ payoff, height = 300 }) {
  const width = 700;
  const points = payoff.points;
  const xs = points.map((point) => point.price);
  const ys = [
    ...points.map((point) => point.at_expiry),
    ...points.map((point) => point.at_now).filter((value) => value !== null),
    0,
  ];
  const scale = scales(width, height, xs, ys);

  return (
    <div>
      <svg viewBox={`0 0 ${width} ${height}`} width="100%" height={height} role="img">
        <Axes
          scale={scale}
          width={width}
          height={height}
          formatY={(value) => `$${value.toFixed(0)}`}
          formatX={(value) => value.toFixed(0)}
        />

        {/* Profit and loss above and below the zero line, shaded so the sign is
            readable at a glance rather than only from the axis labels. */}
        <line
          x1={PAD.left}
          x2={width - PAD.right}
          y1={scale.y(0)}
          y2={scale.y(0)}
          stroke="#5d6875"
        />

        <line
          x1={scale.x(payoff.spot)}
          x2={scale.x(payoff.spot)}
          y1={PAD.top}
          y2={height - PAD.bottom}
          stroke="#4c9be8"
          strokeDasharray="3 3"
        />
        <text x={scale.x(payoff.spot) + 4} y={PAD.top + 10} fill="#4c9be8">
          spot {num(payoff.spot)}
        </text>

        {payoff.breakevens.map((value) => (
          <g key={value}>
            <line
              x1={scale.x(value)}
              x2={scale.x(value)}
              y1={PAD.top}
              y2={height - PAD.bottom}
              stroke="#d9a441"
              strokeDasharray="1 4"
            />
            <text x={scale.x(value) + 4} y={height - PAD.bottom - 4} fill="#d9a441">
              BE {num(value)}
            </text>
          </g>
        ))}

        <path
          d={linePath(
            points.map((point) => [point.price, point.at_now]),
            scale,
          )}
          fill="none"
          stroke="#8b97a6"
          strokeWidth="1.5"
          strokeDasharray="4 3"
        />
        <path
          d={linePath(
            points.map((point) => [point.price, point.at_expiry]),
            scale,
          )}
          fill="none"
          stroke="#46b17b"
          strokeWidth="2"
        />
      </svg>
      <div className="chart-note">
        Solid: at expiry. Dashed: at T plus zero, repriced at each leg&apos;s own entry
        volatility. A short premium position sits below its expiry curve for most of its
        life, which is the shape of being paid to wait.
      </div>
    </div>
  );
}

export function TermStructure({ points, backwardated, slope }) {
  const width = 460;
  const height = 200;
  if (!points || points.length === 0) {
    return <div className="empty-state">No expiry has a solvable at the money volatility.</div>;
  }
  const scale = scales(
    width,
    height,
    points.map((point) => point.dte),
    points.map((point) => point.iv),
  );

  return (
    <div>
      <svg viewBox={`0 0 ${width} ${height}`} width="100%" height={height} role="img">
        <Axes
          scale={scale}
          width={width}
          height={height}
          formatY={(value) => vol(value)}
          formatX={(value) => `${Math.round(value)}d`}
        />
        <path
          d={linePath(
            points.map((point) => [point.dte, point.iv]),
            scale,
          )}
          fill="none"
          stroke="#4c9be8"
          strokeWidth="2"
        />
        {points.map((point) => (
          <circle
            key={point.expiry}
            cx={scale.x(point.dte)}
            cy={scale.y(point.iv)}
            r="3"
            fill="#4c9be8"
          />
        ))}
      </svg>
      <div className="chart-note">
        {slope === null || slope === undefined
          ? "Not enough expiries past the front week to measure a slope."
          : `Slope past 7 DTE: ${(slope * 100).toFixed(1)} vol points. ${
              backwardated
                ? "Backwardated, so something is expected soon. Look for the event before selling the front."
                : "Normal upward term structure."
            }`}
      </div>
    </div>
  );
}

//: Moneyness band the smile is drawn over.
//:
//: Not cosmetic. A far out of the money strike quoted 0.00 by 0.05 solves to a real
//: implied volatility of 195 percent, and plotting it against a 14 percent at the
//: money vol compresses the entire meaningful part of the curve into the bottom two
//: pixels. The same band, and the same reason, as the smile fit in the gaps module.
const SKEW_BAND = 0.2;

export function SkewCurve({ putSkew, callSkew, spot, atmIv }) {
  const width = 460;
  const height = 200;
  const inBand = (points) =>
    (points || []).filter((point) => Math.abs(point.strike / spot - 1) <= SKEW_BAND);

  const puts = inBand(putSkew);
  const calls = inBand(callSkew);
  const all = [...puts, ...calls];
  const hidden = (putSkew?.length || 0) + (callSkew?.length || 0) - all.length;

  if (all.length === 0) {
    return (
      <div className="empty-state">
        No strike within {Math.round(SKEW_BAND * 100)} percent of spot solved to a
        volatility in this expiry.
      </div>
    );
  }
  const scale = scales(
    width,
    height,
    all.map((point) => point.strike),
    all.map((point) => point.iv),
  );

  return (
    <div>
      <svg viewBox={`0 0 ${width} ${height}`} width="100%" height={height} role="img">
        <Axes
          scale={scale}
          width={width}
          height={height}
          formatY={(value) => vol(value)}
          formatX={(value) => value.toFixed(0)}
        />
        <line
          x1={scale.x(spot)}
          x2={scale.x(spot)}
          y1={PAD.top}
          y2={height - PAD.bottom}
          stroke="#4c9be8"
          strokeDasharray="3 3"
        />
        <path
          d={linePath(
            puts.map((point) => [point.strike, point.iv]),
            scale,
          )}
          fill="none"
          stroke="#d9635f"
          strokeWidth="1.8"
        />
        <path
          d={linePath(
            calls.map((point) => [point.strike, point.iv]),
            scale,
          )}
          fill="none"
          stroke="#46b17b"
          strokeWidth="1.8"
        />
      </svg>
      <div className="chart-note">
        Red: puts. Green: calls. Each point is that strike&apos;s own solved volatility,
        in vol points. At the money: {vol(atmIv)}.
        {hidden > 0 && (
          <>
            {" "}
            {hidden} strikes beyond {Math.round(SKEW_BAND * 100)} percent of spot are off
            the chart: their quotes are wide enough that the solved volatility is about
            the quote rather than about the market.
          </>
        )}
      </div>
    </div>
  );
}

export function IvRankGauge({ ivRank }) {
  const width = 260;
  const height = 96;
  const barY = 42;
  const barH = 14;
  const left = 16;
  const right = width - 16;

  if (!ivRank) {
    return (
      <div className="empty-state">
        No volatility history for this symbol yet. IV rank needs months of daily captures
        and cannot be backfilled.
      </div>
    );
  }

  const usable = ivRank.rank !== null && ivRank.rank !== undefined;

  return (
    <div>
      <svg viewBox={`0 0 ${width} ${height}`} width="100%" height={height} role="img">
        <rect x={left} y={barY} width={right - left} height={barH} rx="3" fill="#1c232c" />
        {usable && (
          <>
            <rect
              x={left}
              y={barY}
              width={(right - left) * ivRank.rank}
              height={barH}
              rx="3"
              fill="#4c9be8"
            />
            <text x={left} y={barY - 8} fill="#d7dee7" style={{ fontSize: 15 }}>
              {pct(ivRank.rank, 0)} rank
            </text>
            <text x={right} y={barY - 8} textAnchor="end">
              {pct(ivRank.percentile, 0)} percentile
            </text>
          </>
        )}
        {!usable && (
          <text x={left} y={barY - 8} fill="#8b97a6" style={{ fontSize: 14 }}>
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
