import { useCallback, useEffect, useRef, useState } from "react";
import { CrosshairMode, LineStyle, createChart } from "lightweight-charts";
import { themeColour, withAlpha } from "./theme.js";
import { num } from "../../format.js";

// One indicator, in its own chart below the price.
//
// ## Why these are separate charts and not a squeezed price scale
//
// They used to be drawn on the price chart against a second scale pushed into the
// bottom fifth of the canvas. That is how most of the problems arose: an RSI given
// fourteen percent of the height is a flat wiggle whatever its values are, only one
// oscillator could exist at a time because they were sharing one pane, and there was
// nowhere to put a readout so the number under the cursor was simply unavailable.
//
// A separate chart per indicator fixes all of that at once. Each gets a real height, as
// many as you like can be open, each has a header with room for its parameters and its
// value at the crosshair, and each can be closed on its own.
//
// ## What has to be kept in step by hand
//
// The cost of separate charts is that nothing is shared automatically. Three things are
// synchronised explicitly:
//
//   - **The visible range.** Both directions, guarded by a module-level flag, because
//     applying a range fires the same event that applied it and two charts pointing at
//     each other will recurse until the stack gives out.
//   - **The crosshair.** Driven from the price chart, so moving the cursor over the
//     candles moves the line here too and the header reads the same instant.
//   - **The price axis width.** A `minimumWidth` on every scale, because a pane whose
//     axis is two characters narrower than the price chart's is a pane whose time axis
//     does not line up, and the eye catches that immediately.

//: Height of one pane. Tall enough that a bounded oscillator has a readable shape,
//: short enough that three of them still leave the price chart the main event.
const PANE_HEIGHT = 132;

//: Forced width for every price axis so the panes and the price chart share a left
//: edge. Chosen to fit "100.00" plus the label padding.
export const AXIS_MIN_WIDTH = 64;

/** True while a range is being applied, so the echo does not bounce back. */
let syncing = false;

function seriesFor(chart, plot, theme) {
  const colour = themeColour(theme, plot.colourToken);
  const shared = {
    priceLineVisible: false,
    lastValueVisible: false,
    crosshairMarkerVisible: plot.seriesType !== "histogram",
  };
  if (plot.seriesType === "histogram") {
    return chart.addHistogramSeries({ ...shared, color: withAlpha(colour, 0.5) });
  }
  return chart.addLineSeries({
    ...shared,
    color: colour,
    lineWidth: plot.lineWidth || 1.4,
  });
}

export default function IndicatorPane({
  indicator,
  bars,
  theme,
  mainChart,
  onClose,
  onDragStart,
  onDragOver,
  onDrop,
  dragging,
}) {
  const container = useRef(null);
  const chartRef = useRef(null);
  const seriesRef = useRef(new Map());
  // Computed values indexed by bar time, so reading the crosshair is a map lookup.
  // The first version recomputed the whole indicator inside the crosshair handler,
  // which ran an RSI over every bar on every mouse move.
  const byTime = useRef(new Map());
  const [values, setValues] = useState(null);

  // Create once. Rebuilding on every data change flashes the pane.
  useEffect(() => {
    if (!container.current) return undefined;

    const chart = createChart(container.current, {
      height: PANE_HEIGHT,
      autoSize: true,
      layout: {
        background: { color: "transparent" },
        textColor: theme.axis,
        fontFamily: '"JetBrains Mono", "Cascadia Mono", Consolas, "SF Mono", monospace',
        fontSize: 10,
        attributionLogo: false,
      },
      grid: { vertLines: { visible: false }, horzLines: { color: theme.grid } },
      rightPriceScale: {
        borderVisible: false,
        minimumWidth: AXIS_MIN_WIDTH,
        scaleMargins: { top: 0.12, bottom: 0.12 },
      },
      // The time axis lives on the price chart alone. Repeating it under every pane is
      // three copies of the same row taking height from the data.
      timeScale: { visible: false, borderVisible: false, rightOffset: 4 },
      crosshair: {
        mode: CrosshairMode.Magnet,
        vertLine: { color: theme.crosshair, width: 1, style: LineStyle.Dashed, labelVisible: false },
        horzLine: { color: theme.crosshair, width: 1, style: LineStyle.Dashed, labelBackgroundColor: theme.panelRaised },
      },
      handleScroll: true,
      handleScale: true,
    });

    chartRef.current = chart;
    const map = new Map();
    for (const plot of indicator.plots) map.set(plot.key, seriesFor(chart, plot, theme));
    seriesRef.current = map;

    // Reference levels, straight from the registry. An oscillator without its levels is
    // a shape with no scale to read it against.
    const first = map.get(indicator.plots[0].key);
    const bounds = indicator.bounds;
    if (bounds && first) {
      for (const level of bounds.guides || []) {
        first.createPriceLine({
          price: level,
          color: themeColour(theme, "--chart-axis"),
          lineWidth: 1,
          lineStyle: LineStyle.Dashed,
          axisLabelVisible: true,
          title: "",
        });
      }
      if (bounds.min !== undefined && bounds.max !== undefined) {
        first.applyOptions({
          autoscaleInfoProvider: () => ({
            priceRange: { minValue: bounds.min, maxValue: bounds.max },
          }),
        });
      }
    }

    return () => {
      chart.remove();
      chartRef.current = null;
      seriesRef.current = new Map();
    };
  }, [indicator, theme]);

  // Data, and the time index the header reads from.
  useEffect(() => {
    const map = seriesRef.current;
    if (map.size === 0) return;
    const computed = indicator.compute(bars);

    const index = new Map();
    const last = {};
    for (const plot of indicator.plots) {
      const rows = computed[plot.key] || [];
      map.get(plot.key)?.setData(rows);
      for (const row of rows) {
        const slot = index.get(row.time) || {};
        slot[plot.key] = row.value;
        index.set(row.time, slot);
      }
      last[plot.key] = rows.length ? rows[rows.length - 1].value : null;
    }
    byTime.current = index;

    // The header shows the last value until the cursor says otherwise, so a pane that
    // has never been hovered is still reading something.
    setValues(last);
  }, [indicator, bars]);

  // Two-way range sync with the price chart, plus crosshair in one direction.
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart || !mainChart) return undefined;

    const mine = chart.timeScale();
    const theirs = mainChart.timeScale();

    const push = (range) => {
      if (syncing || !range) return;
      syncing = true;
      try {
        theirs.setVisibleLogicalRange(range);
      } finally {
        syncing = false;
      }
    };
    const pull = (range) => {
      if (syncing || !range) return;
      syncing = true;
      try {
        mine.setVisibleLogicalRange(range);
      } finally {
        syncing = false;
      }
    };

    // One handler for both jobs: move this pane's crosshair to match, and read the
    // value there into the header. Splitting them meant two subscriptions doing the
    // same lookup on every frame.
    const onMainCrosshair = (param) => {
      if (!param.time) {
        chart.clearCrosshairPosition();
        return;
      }
      const first = seriesRef.current.get(indicator.plots[0].key);
      if (first) chart.setCrosshairPosition(0, param.time, first);

      const hit = byTime.current.get(param.time);
      if (hit) setValues((current) => ({ ...current, ...hit }));
    };

    // Hovering the pane itself reads the same index, so both routes to the header
    // agree by construction rather than by two implementations happening to match.
    const onOwnCrosshair = (param) => {
      if (!param.time) return;
      const hit = byTime.current.get(param.time);
      if (hit) setValues((current) => ({ ...current, ...hit }));
    };

    mine.subscribeVisibleLogicalRangeChange(push);
    theirs.subscribeVisibleLogicalRangeChange(pull);
    mainChart.subscribeCrosshairMove(onMainCrosshair);
    chart.subscribeCrosshairMove(onOwnCrosshair);

    pull(theirs.getVisibleLogicalRange());

    return () => {
      mine.unsubscribeVisibleLogicalRangeChange(push);
      theirs.unsubscribeVisibleLogicalRangeChange(pull);
      mainChart.unsubscribeCrosshairMove(onMainCrosshair);
      chart.unsubscribeCrosshairMove(onOwnCrosshair);
    };
  }, [mainChart, indicator]);

  const stop = useCallback((event) => event.stopPropagation(), []);

  return (
    <div
      className={`indicator-pane ${dragging ? "dragging" : ""}`}
      draggable
      onDragStart={onDragStart}
      onDragOver={onDragOver}
      onDrop={onDrop}
    >
      <div className="indicator-pane-head">
        <span className="pane-grip" aria-hidden="true">
          ⠿
        </span>
        <span className="pane-name">
          {indicator.label}
          {indicator.paramLabel ? `(${indicator.paramLabel})` : ""}
        </span>
        {/* Every plot's value at the crosshair, coloured to match its line, so a MACD
            header reads the same order as the lines under it. */}
        {indicator.plots.map((plot) => (
          <span
            key={plot.key}
            className="pane-value"
            style={{ color: themeColour(theme, plot.colourToken) }}
          >
            {plot.label}:{values?.[plot.key] === null || values?.[plot.key] === undefined
              ? " -"
              : ` ${num(values[plot.key])}`}
          </span>
        ))}
        <button
          type="button"
          className="pane-close"
          aria-label={`Close ${indicator.label}`}
          title="Close"
          onClick={(event) => {
            stop(event);
            onClose();
          }}
        >
          ×
        </button>
      </div>
      <div className="indicator-pane-canvas" ref={container} />
    </div>
  );
}
