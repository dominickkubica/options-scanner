import { useEffect, useMemo, useRef, useState } from "react";
import { CrosshairMode, LineStyle, createChart } from "lightweight-charts";
import { aggregate } from "./aggregate.js";
import { DEFAULT_ENABLED, INDICATORS, INDICATORS_BY_ID, latestValue } from "./indicators.js";
import { readChartTheme, themeColour, withAlpha } from "./theme.js";
import { compact, num, pct } from "../../format.js";

// The price chart.
//
// Three ideas hold the design together, and each of them removes something rather than
// adding it.
//
//   1. **There is no floating tooltip.** Hovered values are written into a fixed header
//      above the canvas. A tooltip that follows the cursor covers the candles the
//      reader is pointing at, and it moves the numbers to a different place on every
//      frame. A fixed readout is easier to read and leaves the chart alone.
//   2. **There are no vertical gridlines and no axis borders.** A price chart already
//      has a strong horizontal structure. Every vertical rule competes with the candles
//      for the same lines.
//   3. **The price axis is locked to autoscale.** Dragging the axis to squash or
//      stretch the price range buys nothing on a daily chart and lets a stray gesture
//      flatten a thirty percent move into a straight line. Horizontal zoom stays.
//
// Intervals above a day are aggregated from the daily bars in `aggregate.js` rather
// than fetched, and indicators come from the registry in `indicators.js`. This file
// knows how to draw a series; it does not know what a moving average is.

//: Displayed bars past which candles stop being candles, on daily and above.
//:
//: At two years of daily bars a candle is about one pixel wide and the chart is a
//: smear with no readable body or wick. Past this count the area series is drawn
//: instead, whatever the toggle says, and the chart states why. Chosen on total
//: displayed bars rather than the visible range on purpose: reacting to zoom would
//: flip the rendering mid gesture, which is worse than a stable rule the reader can
//: predict and step around by picking a coarser interval.
//:
//: **It does not apply intraday.** One session at one minute is 390 bars of regular
//: trading and more with extended hours, so this rule turned every minute chart into a
//: line, which is the opposite of why somebody drills into a day. The escape it offers
//: is also missing there: the fix on a daily chart is a coarser interval or a shorter
//: window, and in session mode neither control exists. Intraday keeps its candles and
//: opens on a readable slice of them instead. See INTRADAY_VISIBLE_BARS.
const CANDLE_LIMIT = 400;

//: Bars an intraday chart opens showing, scrolled to the most recent. Wide enough that
//: a two minute candle has a readable body, and the rest of the session is one scroll
//: away rather than compressed into the same width.
const INTRADAY_VISIBLE_BARS = 130;

//: Pixels a band needs before its label is drawn. Below this the text is wider than the
//: band and would spill into the neighbouring phase, which is worse than no label.
const MIN_LABEL_WIDTH = 26;

//: Leading sessions fetched beyond the visible window so the long indicators can fill
//: their windows. Sized to the longest period in the registry, which is the 200 day
//: moving average.
//:
//: Without this a six month chart reported "MA200 needs 200 bars, have 126" and simply
//: refused, which confuses what is being *drawn* with what is being *looked at*: the
//: average needs 200 sessions of history behind the first plotted point, not 200 points
//: on the screen. The warmup is loaded, used, and left off to the left of the opening
//: view.
export const INDICATOR_WARMUP = 200;

//: The four phases of a US equity trading day, as minutes from midnight Eastern.
//:
//: Distinguishing them matters more than it looks. On a quiet name the extended-hours
//: bars are most of the chart, and a print at 05:00 is often a single order against a
//: spread nobody could have traded size into. Read as though it were a 10am print, it
//: invents support and resistance that never existed.
//:
//: Shown as background bands rather than by fading the candles: the candles are the
//: data and dimming them makes the quiet part of the day harder to read at exactly the
//: moment somebody has zoomed in to look at it. The band says the same thing behind
//: them.
const PHASES = [
  { key: "ON", label: "ON", title: "Overnight", from: 20 * 60, to: 24 * 60 },
  { key: "ON2", label: "ON", title: "Overnight", from: 0, to: 4 * 60 },
  { key: "PM", label: "PM", title: "Pre-market", from: 4 * 60, to: 9 * 60 + 30 },
  { key: "MH", label: "MH", title: "Market hours", from: 9 * 60 + 30, to: 16 * 60 },
  { key: "AH", label: "AH", title: "After hours", from: 16 * 60, to: 20 * 60 },
];

//: Eastern wall-clock minutes for an epoch second, via the runtime's own tz database so
//: this stays right across daylight saving without a table to maintain. Verified at
//: 09:30 in both January and September.
const EASTERN = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/New_York",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
});

function easternMinutes(epochSeconds) {
  const parts = EASTERN.formatToParts(new Date(epochSeconds * 1000));
  const hour = Number(parts.find((p) => p.type === "hour")?.value);
  const minute = Number(parts.find((p) => p.type === "minute")?.value);
  if (Number.isNaN(hour) || Number.isNaN(minute)) return null;
  // Midnight formats as 24 under hour12:false in some engines.
  return (hour % 24) * 60 + minute;
}

/** Which phase a bar falls in, or null for a daily bar whose time is a date string. */
export function sessionPhase(time) {
  if (typeof time !== "number") return null;
  const minutes = easternMinutes(time);
  if (minutes === null) return null;
  const phase = PHASES.find((p) => minutes >= p.from && minutes < p.to);
  // "ON2" is the small hours half of overnight. It is a separate row because the phase
  // wraps midnight, and the reader should not see two different labels for one night.
  return phase ? { ...phase, key: phase.key === "ON2" ? "ON" : phase.key } : null;
}

/** Contiguous runs of one phase, as {phase, from, to} in bar time. */
export function sessionRuns(bars) {
  const runs = [];
  for (const bar of bars) {
    const phase = sessionPhase(bar.time);
    if (!phase) continue;
    const last = runs[runs.length - 1];
    if (last && last.key === phase.key) {
      last.to = bar.time;
    } else {
      runs.push({ key: phase.key, label: phase.label, title: phase.title, from: bar.time, to: bar.time });
    }
  }
  return runs;
}

const CHART_HEIGHT = 340;

//: Trading days in a calendar year, used to turn a window label into a bar count.
//:
//: The history endpoint's `days` parameter returns that many **bars**, not that many
//: calendar days: asking for 730 comes back with 730 sessions running from 2023-10-09,
//: which is nearly three years rather than two. Measured against the running API
//: rather than assumed. Labelling that button "2Y" would overstate the window by about
//: forty five percent, so every window below is sized in sessions and the label is the
//: calendar span those sessions actually cover.
const SESSIONS_PER_YEAR = 252;

//: Windows offered per interval, in sessions.
//:
//: Not the same list for every interval. Three months of monthly candles is three
//: bars, and five years of daily candles is a smear no zoom level rescues. Each
//: interval offers only the windows where it reads as a chart.
const WINDOWS = {
  "1D": {
    options: [
      { label: "1M", days: Math.round(SESSIONS_PER_YEAR / 12) },
      { label: "3M", days: Math.round(SESSIONS_PER_YEAR / 4) },
      { label: "6M", days: Math.round(SESSIONS_PER_YEAR / 2) },
      { label: "1Y", days: SESSIONS_PER_YEAR },
      { label: "2Y", days: SESSIONS_PER_YEAR * 2 },
    ],
    fallback: Math.round(SESSIONS_PER_YEAR / 2),
  },
  "1W": {
    options: [
      { label: "1Y", days: SESSIONS_PER_YEAR },
      { label: "2Y", days: SESSIONS_PER_YEAR * 2 },
      { label: "5Y", days: SESSIONS_PER_YEAR * 5 },
    ],
    fallback: SESSIONS_PER_YEAR * 2,
  },
  "1M": {
    options: [
      { label: "2Y", days: SESSIONS_PER_YEAR * 2 },
      { label: "5Y", days: SESSIONS_PER_YEAR * 5 },
    ],
    fallback: SESSIONS_PER_YEAR * 5,
  },
};

const INTERVAL_IDS = ["1D", "1W", "1M"];

// The crosshair reports the time it is over. Daily times go in as ISO strings and come
// back as strings, but the library is documented to normalize some inputs to a business
// day object, so both shapes are handled rather than trusting one and rendering "n/a"
// across the whole header if the other ever arrives.
function timeKey(time) {
  // Intraday series carry epoch seconds rather than an ISO date, so a number is a
  // valid key and is returned as-is. Stringifying it here would stop it matching
  // `bar.time` and silently kill the crosshair readout on every intraday chart.
  if (typeof time === "number") return time;
  if (typeof time === "string") return time;
  if (time && typeof time === "object" && "year" in time) {
    const month = String(time.month).padStart(2, "0");
    const day = String(time.day).padStart(2, "0");
    return `${time.year}-${month}-${day}`;
  }
  return null;
}

/**
 * Draw an indicator's declared reference levels, and pin its scale if it is bounded.
 *
 * An oscillator without its levels is unreadable: RSI at 44 means nothing until 30 and
 * 70 are on the page, and a pane that rescales to the visible data makes the same shape
 * appear at every zoom. A bounded indicator therefore gets a fixed autoscale as well, so
 * "near the top" keeps meaning the same thing.
 */
function applyBounds(entry, indicator, theme) {
  const bounds = indicator.bounds;
  if (!bounds) return;
  const target = entry.get(indicator.plots[0].key);
  if (!target) return;

  for (const level of bounds.guides || []) {
    target.createPriceLine({
      price: level,
      color: themeColour(theme, "--chart-axis"),
      lineWidth: 1,
      lineStyle: LineStyle.Dashed,
      axisLabelVisible: true,
      title: "",
    });
  }

  if (bounds.min !== undefined && bounds.max !== undefined) {
    target.applyOptions({
      autoscaleInfoProvider: () => ({
        priceRange: { minValue: bounds.min, maxValue: bounds.max },
      }),
    });
  }
}

function addPlotSeries(chart, plot, theme, priceScaleId) {
  const colour = themeColour(theme, plot.colourToken);
  const shared = {
    priceLineVisible: false,
    lastValueVisible: false,
    crosshairMarkerVisible: false,
    // Overlays share the price axis. A lower pane indicator gets its own, because an
    // RSI bounded 0 to 100 drawn against a price axis is a flat line at the bottom of
    // the chart and a MACD centred on zero is a flat line off the top.
    ...(priceScaleId ? { priceScaleId } : {}),
  };
  if (plot.seriesType === "histogram") {
    return chart.addHistogramSeries({ ...shared, color: colour });
  }
  if (plot.seriesType === "area") {
    return chart.addAreaSeries({
      ...shared,
      lineColor: colour,
      topColor: withAlpha(colour, 0.24),
      bottomColor: withAlpha(colour, 0),
      lineWidth: plot.lineWidth || 2,
    });
  }
  return chart.addLineSeries({
    ...shared,
    color: colour,
    lineWidth: plot.lineWidth || 1.2,
  });
}

//: Intraday intervals offered when drilling into a single session. Deliberately
//: short: a whole day at one minute is about 880 bars including extended hours, and
//: anything finer than a minute is not served.
const SESSION_INTERVALS = ["1Min", "2Min", "5Min"];

//: Where the lower pane sits, as fractions of the chart height. Above the volume
//: histogram at 0.82 and below the price, so all three read as stacked bands rather
//: than as overlapping series.
const LOWER_PANE_MARGINS = { top: 0.66, bottom: 0.2 };

//: How far the price is squeezed when a lower pane indicator is on. Without this the
//: candles would draw straight through the oscillator.
const PRICE_MARGINS_WITH_LOWER = { top: 0.06, bottom: 0.42 };
const PRICE_MARGINS_ALONE = { top: 0.08, bottom: 0.28 };

function ChartTypeToggle({ chartType, setChartType }) {
  return (
    <div className="seg" role="group" aria-label="chart type">
      {[
        ["candles", "Candles"],
        ["area", "Area"],
      ].map(([id, label]) => (
        <button
          key={id}
          type="button"
          className={`seg-btn ${chartType === id ? "active" : ""}`}
          onClick={() => setChartType(id)}
        >
          {label}
        </button>
      ))}
    </div>
  );
}

function IndicatorChips({ chips, theme, onToggle }) {
  return (
    <div className="chip-toggles">
      {chips.map((chip) => (
        <button
          key={chip.indicator.id}
          type="button"
          className={`chip-toggle ${chip.on ? "on" : ""} ${chip.drawable ? "" : "unavailable"}`}
          disabled={!chip.drawable}
          title={chip.reason || undefined}
          style={
            chip.on
              ? { color: themeColour(theme, chip.indicator.plots[0].colourToken) }
              : undefined
          }
          onClick={() => onToggle(chip.indicator.id)}
        >
          {chip.indicator.label}
          {chip.on && chip.value !== null && <span> {num(chip.value)}</span>}
          {!chip.drawable && <span className="chip-reason"> {chip.reason}</span>}
        </button>
      ))}
    </div>
  );
}

export default function PriceChart({
  symbol,
  bars,
  loading,
  days,
  onDaysChange,
  // Session drill-in. When `session` is set the parent is supplying one day of
  // intraday bars, so the aggregation and window controls below are meaningless and
  // are replaced rather than disabled: a control that cannot do anything is worse
  // than one that is not there.
  interval: dataInterval = "1Day",
  onIntervalChange,
  session = null,
  onOpenSession,
  onBackToDaily,
}) {
  // `chooseInterval` rather than `setInterval`, which would shadow the global timer
  // function for the whole component.
  const [interval, chooseInterval] = useState("1D");
  const [chartType, setChartType] = useState("candles");
  const [enabled, setEnabled] = useState(DEFAULT_ENABLED);
  const [hoveredKey, setHoveredKey] = useState(null);
  // Resolved once, during the first render, so the chip colours are right on the very
  // first paint rather than one state change later. The app has no theme switch, so
  // this never needs to change afterwards.
  const [theme] = useState(readChartTheme);

  const container = useRef(null);
  const chartRef = useRef(null);
  const seriesRef = useRef({ candle: null, area: null, volume: null });
  const indicatorSeries = useRef(new Map());
  // The crosshair handler is subscribed once and must see the current bars without
  // being torn down and resubscribed on every data change.
  const barsRef = useRef([]);
  const clickRef = useRef(null);
  const [pendingDay, setPendingDay] = useState(null);

  const displayBars = useMemo(() => aggregate(bars || [], interval), [bars, interval]);
  const isIntraday = Boolean(session);

  // How many aggregated bars the requested window comes to. Derived by aggregating the
  // window's own slice rather than by dividing, because a week is not a fixed number of
  // sessions once holidays are involved and an approximation here would drift the
  // opening view a little further every month.
  // Session bands, in pixels. Recomputed whenever the visible range moves, because a
  // band is a time range and the pixel it maps to changes on every scroll and zoom.
  const [bands, setBands] = useState([]);

  const runs = useMemo(
    () => (isIntraday ? sessionRuns(displayBars) : []),
    [displayBars, isIntraday],
  );

  useEffect(() => {
    const chart = chartRef.current;
    if (!chart || runs.length === 0) {
      setBands([]);
      return undefined;
    }

    const scale = chart.timeScale();
    const reposition = () => {
      const width = scale.width();
      const next = [];
      for (const run of runs) {
        // Null means the edge is scrolled out of view, so clamp to the visible edge
        // rather than dropping the band: a market-hours block that starts off-screen is
        // still the band the reader is looking at.
        const rawLeft = scale.timeToCoordinate(run.from);
        const rawRight = scale.timeToCoordinate(run.to);
        if (rawLeft === null && rawRight === null) continue;
        const left = Math.max(0, rawLeft ?? 0);
        const right = Math.min(width, rawRight ?? width);
        if (right - left <= 0) continue;
        next.push({ ...run, left, width: right - left });
      }
      setBands(next);
    };

    reposition();
    scale.subscribeVisibleLogicalRangeChange(reposition);
    return () => scale.unsubscribeVisibleLogicalRangeChange(reposition);
  }, [runs]);

  const windowBars = useMemo(
    () => (isIntraday ? 0 : aggregate((bars || []).slice(-days), interval).length),
    [bars, days, interval, isIntraday],
  );
  barsRef.current = displayBars;

  // Intraday is exempt: see CANDLE_LIMIT. A minute chart is meant to be scrolled, not
  // flattened, and session mode has neither of the controls the degradation notice
  // tells the reader to reach for.
  const degraded =
    !isIntraday && chartType === "candles" && displayBars.length > CANDLE_LIMIT;
  const effectiveType = degraded ? "area" : chartType;

  // Create the chart exactly once. Every later change is applyOptions or setData on the
  // series that already exist, because tearing the chart down and rebuilding it flashes
  // the panel on something as small as toggling a moving average.
  useEffect(() => {
    if (!container.current) return undefined;

    const chart = createChart(container.current, {
      height: CHART_HEIGHT,
      autoSize: true,
      layout: {
        background: { color: "transparent" },
        textColor: theme.axis,
        fontFamily: '"JetBrains Mono", "Cascadia Mono", Consolas, "SF Mono", monospace',
        fontSize: 10.5,
        attributionLogo: false,
      },
      grid: {
        // The single largest declutter on the whole panel.
        vertLines: { visible: false },
        horzLines: { color: theme.grid },
      },
      rightPriceScale: {
        borderVisible: false,
        autoScale: true,
        scaleMargins: { top: 0.08, bottom: 0.28 },
      },
      timeScale: {
        borderVisible: false,
        rightOffset: 4,
        fixLeftEdge: true,
        fixRightEdge: true,
      },
      crosshair: {
        mode: CrosshairMode.Magnet,
        vertLine: {
          color: theme.crosshair,
          width: 1,
          style: LineStyle.Dashed,
          labelBackgroundColor: theme.panelRaised,
        },
        horzLine: {
          color: theme.crosshair,
          width: 1,
          style: LineStyle.Dashed,
          labelBackgroundColor: theme.panelRaised,
        },
      },
      // Horizontal navigation only. See the header comment on the locked price axis.
      //
      // The wheel is deliberately given back to the page. A chart that captures the
      // wheel is right when it owns the whole screen, and wrong in a panel stacked
      // among five others: scrolling the page with the pointer anywhere over the chart
      // silently zoomed it instead, which both blocked the scroll and left the view
      // drifted away from the latest bar with no visible cause. Zoom stays available by
      // dragging the time axis, and pinch still works on a trackpad.
      handleScale: {
        axisPressedMouseMove: { price: false, time: true },
        axisDoubleClickReset: { price: false, time: true },
        mouseWheel: false,
        pinch: true,
      },
      handleScroll: {
        mouseWheel: false,
        pressedMouseMove: true,
        horzTouchDrag: true,
        vertTouchDrag: false,
      },
    });

    const candle = chart.addCandlestickSeries({
      upColor: theme.up,
      downColor: theme.down,
      wickUpColor: theme.up,
      wickDownColor: theme.down,
      // Solid bodies. A border in a second colour at this bar width is noise.
      borderVisible: false,
      priceLineStyle: LineStyle.Dashed,
      priceLineWidth: 1,
    });

    const area = chart.addAreaSeries({
      lineColor: theme.accent,
      topColor: withAlpha(theme.accent, 0.28),
      bottomColor: withAlpha(theme.accent, 0),
      lineWidth: 2,
      priceLineStyle: LineStyle.Dashed,
      priceLineWidth: 1,
      visible: false,
      priceLineVisible: false,
    });

    const volume = chart.addHistogramSeries({
      priceFormat: { type: "volume" },
      priceScaleId: "volume",
      // Without both of these the volume prints its last value as a tag on the price
      // axis, so a share count lands among the dollar prices. It is the one piece of
      // the old chart that was actively wrong rather than merely plain.
      lastValueVisible: false,
      priceLineVisible: false,
    });
    chart.priceScale("volume").applyOptions({
      scaleMargins: { top: 0.82, bottom: 0 },
      borderVisible: false,
    });

    chartRef.current = chart;
    seriesRef.current = { candle, area, volume };

    chart.subscribeCrosshairMove((param) => {
      const key = param?.time ? timeKey(param.time) : null;
      setHoveredKey(key);
    });

    // Clicking a candle offers to open that day. Handled through a ref rather than
    // closing over the prop: this subscription is set up once, and capturing the
    // first render's callback would leave it pointing at a stale session forever.
    chart.subscribeClick((param) => {
      if (!param?.time) return;
      clickRef.current?.(timeKey(param.time));
    });

    // Double click anywhere on the plot returns to the whole series. The library's own
    // double click reset only covers the axes.
    const node = container.current;
    const reset = () => chart.timeScale().fitContent();
    node.addEventListener("dblclick", reset);

    return () => {
      node.removeEventListener("dblclick", reset);
      chart.remove();
      chartRef.current = null;
      seriesRef.current = { candle: null, area: null, volume: null };
      indicatorSeries.current = new Map();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Price and volume data, plus which of the two price renderings is showing.
  useEffect(() => {
    const chart = chartRef.current;
    const { candle, area, volume } = seriesRef.current;
    if (!chart || !candle) return;

    // Set here rather than in an effect of its own. A separate effect has its own
    // lifecycle and can fire after the creation effect's cleanup has called
    // chart.remove(), which the library reports as "Value is null" and which blanks
    // the whole page. This effect already proved the chart is alive.
    //
    // The axis must show clock times on an intraday series: without it every label on
    // a one day chart is the same date and the axis says nothing.
    chart.applyOptions({
      timeScale: { timeVisible: isIntraday, secondsVisible: false },
    });

    const priceData = displayBars.map((bar) => ({
      time: bar.time,
      open: bar.open,
      high: bar.high,
      low: bar.low,
      close: bar.close,
    }));
    candle.setData(priceData);
    area.setData(displayBars.map((bar) => ({ time: bar.time, value: bar.close })));

    // Both series always hold the data so switching between them is instant, but only
    // the visible one may draw a last price line. `visible: false` hides a series'
    // marks without suppressing its price line, so leaving both on put two labels on
    // the axis, one of them belonging to a series nobody can see.
    const showingCandles = effectiveType === "candles";
    candle.applyOptions({ visible: showingCandles, priceLineVisible: showingCandles });
    area.applyOptions({ visible: !showingCandles, priceLineVisible: !showingCandles });

    // A bar with no volume is left out rather than plotted at zero. An unknown that
    // renders as a flat bar reads as a session that did not trade.
    const withVolume = displayBars.filter(
      (bar) => bar.volume !== null && bar.volume !== undefined,
    );
    volume.setData(
      withVolume.map((bar) => ({
        time: bar.time,
        value: bar.volume,
        color: withAlpha(bar.close >= bar.open ? theme.up : theme.down, 0.35),
      })),
    );

    // What to show is not what was loaded. Intraday opens on the most recent slice so a
    // two minute candle has a readable body; a daily chart opens on the window that was
    // asked for, with the indicator warmup sitting off to the left rather than shrinking
    // everything to fit. fitContent is only right when there is nothing extra to hide.
    const visible = isIntraday
      ? Math.min(INTRADAY_VISIBLE_BARS, displayBars.length)
      : windowBars;

    if (visible > 0 && visible < displayBars.length) {
      chart.timeScale().setVisibleLogicalRange({
        from: displayBars.length - visible,
        to: displayBars.length,
      });
    } else {
      chart.timeScale().fitContent();
    }
  }, [displayBars, effectiveType, theme, isIntraday, windowBars]);

  // Indicator series, reconciled against the registry. Nothing here names an indicator.
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;

    const live = indicatorSeries.current;

    for (const [id, entry] of [...live.entries()]) {
      if (!enabled.includes(id)) {
        for (const series of entry.values()) chart.removeSeries(series);
        live.delete(id);
      }
    }

    for (const id of enabled) {
      const indicator = INDICATORS_BY_ID.get(id);
      if (!indicator) continue;

      let entry = live.get(id);
      if (!entry) {
        entry = new Map();
        // One scale per lower indicator, keyed by its id. Two of them would fight over
        // one axis, which is why the chip row only ever enables one.
        const scaleId = indicator.group === "lower" ? `lower-${id}` : undefined;
        for (const plot of indicator.plots) {
          entry.set(plot.key, addPlotSeries(chart, plot, theme, scaleId));
        }
        if (scaleId) {
          chart.priceScale(scaleId).applyOptions({
            scaleMargins: LOWER_PANE_MARGINS,
            borderVisible: false,
          });
          // The registry has declared `bounds` since the lower pane was added and
          // nothing consumed it, so RSI drew as a bare wiggle with no 30 or 70 to read
          // it against and no fixed scale between refreshes. Both come from the
          // indicator rather than from here: this file still does not know what an RSI
          // is, only that some indicators have levels worth marking.
          applyBounds(entry, indicator, theme);
        }
        live.set(id, entry);
      }

      const computed = indicator.compute(displayBars);
      for (const plot of indicator.plots) {
        entry.get(plot.key).setData(computed[plot.key] || []);
      }
    }
    // The price pane gives up room only while something is using it. Applied here
    // rather than at creation because it has to follow the chips.
    const hasLower = enabled.some((id) => INDICATORS_BY_ID.get(id)?.group === "lower");
    chart.priceScale("right").applyOptions({
      scaleMargins: hasLower ? PRICE_MARGINS_WITH_LOWER : PRICE_MARGINS_ALONE,
    });
  }, [enabled, displayBars, theme]);

  // Chip state for every registered indicator, whether on or off, so a chip can say
  // why it cannot draw instead of silently doing nothing when clicked.
  const chips = useMemo(
    () =>
      INDICATORS.map((indicator) => {
        const drawable = displayBars.length >= indicator.minBars;
        const on = enabled.includes(indicator.id);
        const value =
          drawable && on
            ? latestValue(indicator.compute(displayBars)[indicator.plots[0].key])
            : null;
        return {
          indicator,
          drawable,
          on,
          value,
          reason: drawable ? null : indicator.unavailable(displayBars.length),
        };
      }),
    [displayBars, enabled],
  );

  const readout = useMemo(() => {
    if (displayBars.length === 0) return null;
    const index = hoveredKey
      ? displayBars.findIndex((bar) => bar.time === hoveredKey)
      : displayBars.length - 1;
    const at = index >= 0 ? index : displayBars.length - 1;
    const bar = displayBars[at];
    const previous = at > 0 ? displayBars[at - 1] : null;
    const change = previous ? bar.close - previous.close : null;
    const changePct = previous && previous.close ? change / previous.close : null;
    return { bar, change, changePct, hovering: Boolean(hoveredKey) };
  }, [displayBars, hoveredKey]);

  const windows = WINDOWS[interval];

  // Kept current on every render so the click subscription, which is installed once,
  // always calls the latest one.
  clickRef.current = (day) => {
    if (session || !onOpenSession) return; // already inside a day, or drill-in is off
    setPendingDay(day);
  };

  function changeInterval(next) {
    chooseInterval(next);
    const allowed = WINDOWS[next].options;
    // Switching to monthly while on a ninety day window would leave three candles on
    // screen, so the window moves with the interval when the current one is not offered.
    if (!allowed.some((option) => option.days === days)) {
      onDaysChange(WINDOWS[next].fallback);
    }
  }

  function toggleIndicator(id) {
    setEnabled((current) => {
      if (current.includes(id)) return current.filter((item) => item !== id);

      // Turning on a lower pane indicator turns off whichever one was there. They do
      // not share an axis: RSI is bounded 0 to 100 and MACD is unbounded and centred
      // on zero, so stacking them would draw one of them against the other's scale.
      // Replacing is the honest behaviour and it needs no explanation on screen,
      // because the previous chip visibly turns off as the new one turns on.
      const incoming = INDICATORS_BY_ID.get(id);
      const cleared =
        incoming?.group === "lower"
          ? current.filter((item) => INDICATORS_BY_ID.get(item)?.group !== "lower")
          : current;
      return [...cleared, id];
    });
  }

  const direction = readout && readout.change !== null && readout.change < 0 ? "neg" : "pos";

  return (
    <div className="price-chart">
      <div className="chart-head">
        <div className="chart-head-main">
          <span className="chart-symbol">{symbol}</span>
          {readout ? (
            <>
              <span className="chart-last">{num(readout.bar.close)}</span>
              {readout.change !== null && (
                <span className={`chart-change ${direction}`}>
                  {readout.change >= 0 ? "+" : ""}
                  {num(readout.change)} ({readout.change >= 0 ? "+" : ""}
                  {pct(readout.changePct)})
                </span>
              )}
            </>
          ) : (
            <span className="chart-last dim">no candles</span>
          )}
        </div>

        {readout && (
          <div className="chart-ohlc">
            <span>
              <em>O</em> {num(readout.bar.open)}
            </span>
            <span>
              <em>H</em> {num(readout.bar.high)}
            </span>
            <span>
              <em>L</em> {num(readout.bar.low)}
            </span>
            <span>
              <em>C</em> {num(readout.bar.close)}
            </span>
            <span>
              <em>Vol</em> {compact(readout.bar.volume)}
            </span>
          </div>
        )}
      </div>

      {pendingDay && (
        <div className="day-prompt">
          <span>
            Open <strong>{pendingDay}</strong> intraday?
          </span>
          <div className="seg" role="group" aria-label="intraday interval">
            {SESSION_INTERVALS.map((id) => (
              <button
                key={id}
                type="button"
                className="seg-btn"
                onClick={() => {
                  onOpenSession(pendingDay, id);
                  setPendingDay(null);
                }}
              >
                {id.replace("Min", "m")}
              </button>
            ))}
          </div>
          <button type="button" className="btn" onClick={() => setPendingDay(null)}>
            Cancel
          </button>
        </div>
      )}

      {session ? (
        <div className="chart-controls">
          <button type="button" className="btn primary" onClick={onBackToDaily}>
            Back to daily
          </button>
          <span className="provenance">showing {session}</span>
          <div className="seg" role="group" aria-label="intraday interval">
            {SESSION_INTERVALS.map((id) => (
              <button
                key={id}
                type="button"
                className={`seg-btn ${dataInterval === id ? "active" : ""}`}
                onClick={() => onIntervalChange?.(id)}
              >
                {id.replace("Min", "m")}
              </button>
            ))}
          </div>
          {/* Session mode used to stop here, which meant no chart type toggle and no
              indicators at all once you drilled into a day. Drilling in is exactly when
              somebody wants a moving average and an RSI on the thing they are looking
              at, so both rows are shared with the daily view now. */}
          <ChartTypeToggle chartType={chartType} setChartType={setChartType} />
          <IndicatorChips chips={chips} theme={theme} onToggle={toggleIndicator} />
        </div>
      ) : (
      <div className="chart-controls">
        <div className="seg" role="group" aria-label="interval">
          {INTERVAL_IDS.map((id) => (
            <button
              key={id}
              type="button"
              className={`seg-btn ${interval === id ? "active" : ""}`}
              onClick={() => changeInterval(id)}
            >
              {id}
            </button>
          ))}
        </div>

        <div className="seg" role="group" aria-label="window">
          {windows.options.map((option) => (
            <button
              key={option.days}
              type="button"
              className={`seg-btn ${days === option.days ? "active" : ""}`}
              onClick={() => onDaysChange(option.days)}
            >
              {option.label}
            </button>
          ))}
        </div>

        <ChartTypeToggle chartType={chartType} setChartType={setChartType} />
        <IndicatorChips chips={chips} theme={theme} onToggle={toggleIndicator} />
      </div>
      )}

      <div className="chart-canvas" ref={container}>
        {loading && <div className="chart-skeleton" />}
        <div className="session-bands" aria-hidden="true">
          {bands.map((band) => (
            <div
              key={`${band.key}-${band.from}`}
              className={`session-band phase-${band.key.toLowerCase()}`}
              style={{ left: `${band.left}px`, width: `${band.width}px` }}
              title={band.title}
            >
              {band.width > MIN_LABEL_WIDTH && (
                <span className="session-band-label">{band.label}</span>
              )}
            </div>
          ))}
        </div>
      </div>

      {isIntraday && (
        <div className="chart-note">
          The shaded bands are market hours (MH). Outside them, pre-market (PM), after
          hours (AH) and overnight (ON) prints are thin and the spread is wide, so a bar
          there is often a single order rather than a price anyone could have traded size
          at.
        </div>
      )}

      {degraded && (
        <div className="chart-note">
          {displayBars.length} bars is past the point where a candle body is wide enough
          to read, so this window is drawn as a line.{" "}
          {/* The advice has to match the controls that exist. In session mode there is
              no weekly interval and no window control, so naming them sends the reader
              looking for buttons that are not on the screen. */}
          {isIntraday
            ? "Choose a coarser interval above for candles back."
            : "Switch to a weekly or monthly interval, or a shorter window, to get candles back."}
        </div>
      )}
    </div>
  );
}
