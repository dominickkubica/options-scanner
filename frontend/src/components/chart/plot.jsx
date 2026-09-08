import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";

// The shared foundation for every chart that is not a time series.
//
// ## Why these are not lightweight-charts
//
// The price chart uses it and should. Everything else here plots something against
// something that is not time: profit against underlying price, implied vol against
// strike, implied vol against days to expiry. A time series library asked to draw
// those needs its axis lied to, and the lie shows up as a date formatter on a strike
// axis. They stay SVG.
//
// What they were missing was never the library. It was the price chart's *behaviour*,
// and this module is that behaviour in one place:
//
//   - **Responsive width.** Every one of these was a fixed viewBox at 700, 460 or 260
//     pixels, so they rendered at one size and scaled the text with the container. A
//     measured width means the type stays 11px whether the panel is 400 or 1200 wide.
//   - **A readout in a fixed header, never a floating tooltip.** The price chart's
//     rule, and the reason for it is the same here: a tooltip covers the part of the
//     curve you are pointing at.
//   - **Horizontal gridlines only, and no axis rule.** Vertical lines on a chart with
//     a handful of x values are decoration, and a border around a plot is one more
//     line competing with the data inside it.
//   - **Colours from the same tokens.** styles.css stays the single source.
//
// ## The rule these keep, which is the same rule the indicators keep
//
// A plot draws what it has and says what it does not. `<Plot>` renders an `empty`
// message rather than an axis around nothing, because an empty pair of axes reads as
// a broken feature and a sentence reads as an answer.

/** Measure a container so the chart can be drawn at its real width. */
export function useMeasuredWidth(fallback = 640) {
  const ref = useRef(null);
  const [width, setWidth] = useState(fallback);

  // Measured synchronously before paint, so the first frame is drawn at the real
  // width. Waiting for the observer means one frame at the fallback, which on a narrow
  // panel is a visible jump and on a hidden one never resolves at all: a container that
  // is not being laid out reports zero and the observer has nothing to report.
  useLayoutEffect(() => {
    const node = ref.current;
    if (!node) return;
    const measured = Math.round(node.getBoundingClientRect().width);
    if (measured > 0) setWidth(measured);
  });

  useEffect(() => {
    const node = ref.current;
    if (!node) return undefined;
    // ResizeObserver rather than a window listener: a panel changes width when the
    // sidebar drawer opens or a grid reflows, neither of which resizes the window.
    const observer = new ResizeObserver((entries) => {
      const next = Math.round(entries[0].contentRect.width);
      if (next > 0) setWidth(next);
    });
    observer.observe(node);
    return () => observer.disconnect();
  }, []);

  return [ref, width];
}

/** Linear scales from data space to plot space, with the margins already applied. */
export function scales({ width, height, margin, xs, ys, yPad = 0.08 }) {
  const left = margin.left;
  const right = width - margin.right;
  const top = margin.top;
  const bottom = height - margin.bottom;

  const xMin = Math.min(...xs);
  const xMax = Math.max(...xs);
  let yMin = Math.min(...ys);
  let yMax = Math.max(...ys);

  // A flat series has zero range, which would divide by zero and put every point on
  // the same pixel. Give it a band so the line lands in the middle of the plot.
  if (yMin === yMax) {
    const nudge = Math.abs(yMin) * 0.05 || 1;
    yMin -= nudge;
    yMax += nudge;
  } else {
    const pad = (yMax - yMin) * yPad;
    yMin -= pad;
    yMax += pad;
  }

  const spanX = xMax - xMin || 1;
  const spanY = yMax - yMin || 1;

  return {
    xMin,
    xMax,
    yMin,
    yMax,
    left,
    right,
    top,
    bottom,
    toX: (value) => left + ((value - xMin) / spanX) * (right - left),
    toY: (value) => bottom - ((value - yMin) / spanY) * (bottom - top),
    fromX: (px) => xMin + ((px - left) / (right - left)) * spanX,
  };
}

/** Evenly spaced tick values across a range, including both ends. */
export function ticks(min, max, count = 4) {
  if (!Number.isFinite(min) || !Number.isFinite(max) || count < 2) return [];
  const step = (max - min) / (count - 1);
  return Array.from({ length: count }, (_, index) => min + step * index);
}

/** An SVG path through points already in plot space. */
export function linePath(points) {
  return points
    .map((point, index) => `${index === 0 ? "M" : "L"}${point.x.toFixed(2)},${point.y.toFixed(2)}`)
    .join(" ");
}

export const DEFAULT_MARGIN = { top: 12, right: 52, bottom: 24, left: 12 };

/**
 * A plot frame: horizontal gridlines, a value axis, and crosshair tracking.
 *
 * Children receive the scales and draw into the same coordinate space. The parent
 * owns the readout, because what is worth showing on hover is entirely per chart:
 * a payoff wants profit at that price, a skew wants the strike's own solved vol.
 */
export function Plot({
  height = 220,
  margin = DEFAULT_MARGIN,
  xs,
  ys,
  yTicks = 4,
  formatY = (value) => value.toFixed(2),
  empty = "Nothing to draw.",
  onHoverX,
  children,
}) {
  const [ref, width] = useMeasuredWidth();
  const [cursor, setCursor] = useState(null);

  const usable = xs?.length > 0 && ys?.length > 0;
  const scale = useMemo(
    () => (usable ? scales({ width, height, margin, xs, ys }) : null),
    [usable, width, height, margin, xs, ys],
  );

  const handleMove = useCallback(
    (event) => {
      if (!scale) return;
      const box = event.currentTarget.getBoundingClientRect();
      const px = ((event.clientX - box.left) / box.width) * width;
      setCursor(px);
      onHoverX?.(scale.fromX(px), px);
    },
    [scale, width, onHoverX],
  );

  const handleLeave = useCallback(() => {
    setCursor(null);
    onHoverX?.(null, null);
  }, [onHoverX]);

  if (!usable) {
    return (
      <div ref={ref} className="plot">
        <div className="empty-state">{empty}</div>
      </div>
    );
  }

  const gridValues = ticks(scale.yMin, scale.yMax, yTicks);

  return (
    <div ref={ref} className="plot">
      <svg
        viewBox={`0 0 ${width} ${height}`}
        width="100%"
        height={height}
        role="img"
        onMouseMove={handleMove}
        onMouseLeave={handleLeave}
      >
        {gridValues.map((value) => {
          const y = scale.toY(value);
          return (
            <g key={value}>
              <line
                x1={scale.left}
                x2={scale.right}
                y1={y}
                y2={y}
                stroke="var(--chart-grid)"
                strokeWidth="1"
              />
              {/* Labels sit outside the plot on the right, matching the price chart's
                  axis, so nothing is written over the data. */}
              <text
                x={scale.right + 6}
                y={y + 3}
                fill="var(--chart-axis)"
                style={{ fontSize: 10 }}
              >
                {formatY(value)}
              </text>
            </g>
          );
        })}

        {children({ scale, width, height })}

        {cursor !== null && cursor >= scale.left && cursor <= scale.right && (
          <line
            x1={cursor}
            x2={cursor}
            y1={scale.top}
            y2={scale.bottom}
            stroke="var(--chart-crosshair)"
            strokeWidth="1"
            strokeDasharray="3 3"
          />
        )}
      </svg>
    </div>
  );
}

/**
 * The readout row above a plot.
 *
 * A fixed header rather than a tooltip that follows the pointer. Same decision as the
 * price chart and for the same reason: the tooltip covers the thing being pointed at,
 * and a reader comparing two points has to remember the first one while the second is
 * obscured.
 */
export function PlotReadout({ items }) {
  return (
    <div className="plot-readout">
      {items.map((item) => (
        <span key={item.label} className={item.tone ? `plot-stat ${item.tone}` : "plot-stat"}>
          <em>{item.label}</em> {item.value}
        </span>
      ))}
    </div>
  );
}
