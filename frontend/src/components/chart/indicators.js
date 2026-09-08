import {
  atr,
  bollinger,
  donchian,
  ema,
  keltner,
  macd,
  relativeVolume,
  rsi,
  stochastic,
  vwap,
} from "./compute.js";

// The indicator registry.
//
// Adding an indicator to the price chart should mean adding an entry to this file and
// nothing else. PriceChart iterates the registry, creates whatever series each entry
// declares, and knows nothing about what any of them mean. Three moving averages ship
// today; the contract is shaped for the ones that will follow.
//
// ## The contract
//
// ```
// {
//   id:        stable string, used as the toggle key and the series map key
//   label:     what the chip says
//   group:     "overlay" draws on the price pane. "lower" gets a pane of its own,
//              stacked underneath, with its own header and close button.
//   paramLabel: what goes in the brackets after the name, as "RSI(14)". Optional.
//   minBars:   bars required before anything can be drawn at all
//   plots:     one or more series this indicator owns. Multiple plots is what lets a
//              Bollinger band or a MACD arrive without touching the chart component.
//     { key, label, seriesType: "line" | "histogram" | "area",
//       colourToken, lineWidth }
//   compute(bars) -> { [plotKey]: [{ time, value }] }
//   unavailable(barCount) -> string explaining why it cannot draw
// }
// ```
//
// ## The rule every indicator has to keep
//
// **Emit a point only where the full window exists.** A twenty bar average computed
// over seven bars is a different statistic wearing the same label, and drawn on the
// same axis it is indistinguishable from the real thing. This is the chart's version
// of the rule the vol solver and the IV rank already follow: a number that cannot be
// computed honestly is not computed. `sma` below leads the series with nothing rather
// than with a ramp, and the chart reports the shortfall on the chip instead.
//
// ## Adding one
//
// An EMA is a second factory next to `movingAverage` with the same single plot shape.
// A Bollinger band is one entry whose `plots` are three lines and whose `compute`
// returns three keyed series from one pass. An RSI declares `group: "oscillator"`, at
// which point the chart needs a second price scale, which is the one extension this
// file cannot absorb on its own.

/**
 * Simple moving average over closes.
 *
 * Leads with nothing until the window is full. Rolling sum rather than a fresh slice
 * per point, which matters at 200 periods over 1300 bars.
 */
export function sma(bars, period) {
  if (!bars || bars.length < period || period < 1) return [];

  const out = [];
  let sum = 0;
  for (let index = 0; index < bars.length; index += 1) {
    sum += bars[index].close;
    if (index >= period) sum -= bars[index - period].close;
    if (index >= period - 1) {
      out.push({ time: bars[index].time, value: sum / period });
    }
  }
  return out;
}

function movingAverage(period, colourToken) {
  const label = `MA${period}`;
  return {
    id: `ma${period}`,
    label,
    group: "overlay",
    minBars: period,
    plots: [
      {
        key: "value",
        label,
        seriesType: "line",
        colourToken,
        lineWidth: 1.2,
      },
    ],
    compute: (bars) => ({ value: sma(bars, period) }),
    unavailable: (barCount) => `needs ${period} bars, have ${barCount}`,
  };
}

function expo(period, colourToken) {
  const label = `EMA${period}`;
  return {
    id: `ema${period}`,
    label,
    group: "overlay",
    minBars: period,
    plots: [{ key: "value", label, seriesType: "line", colourToken, lineWidth: 1.2 }],
    compute: (bars) => ({ value: ema(bars, period) }),
    unavailable: (barCount) => `needs ${period} bars, have ${barCount}`,
  };
}

// Order here is the order of the chips. Overlays first, then the lower pane, which is
// the order they stack on the screen.
export const INDICATORS = [
  movingAverage(20, "--chart-ma-fast"),
  movingAverage(50, "--chart-ma-slow"),
  movingAverage(200, "--chart-axis"),
  expo(9, "--chart-ma-fast"),
  expo(21, "--chart-ma-slow"),

  {
    id: "vwap",
    label: "VWAP",
    group: "overlay",
    minBars: 20,
    plots: [
      { key: "value", label: "VWAP20", seriesType: "line", colourToken: "--accent", lineWidth: 1.4 },
    ],
    compute: (bars) => ({ value: vwap(bars, 20) }),
    unavailable: (barCount) =>
      barCount < 20 ? `needs 20 bars, have ${barCount}` : "no volume on these bars",
  },

  {
    id: "bb",
    label: "Bollinger",
    group: "overlay",
    minBars: 20,
    plots: [
      { key: "upper", label: "BB up", seriesType: "line", colourToken: "--chart-axis", lineWidth: 1 },
      { key: "middle", label: "BB mid", seriesType: "line", colourToken: "--chart-axis", lineWidth: 0.8 },
      { key: "lower", label: "BB low", seriesType: "line", colourToken: "--chart-axis", lineWidth: 1 },
    ],
    compute: (bars) => bollinger(bars, 20, 2),
    unavailable: (barCount) => `needs 20 bars, have ${barCount}`,
  },

  {
    id: "keltner",
    label: "Keltner",
    group: "overlay",
    minBars: 21,
    plots: [
      { key: "upper", label: "KC up", seriesType: "line", colourToken: "--chart-ma-slow", lineWidth: 1 },
      { key: "middle", label: "KC mid", seriesType: "line", colourToken: "--chart-ma-slow", lineWidth: 0.8 },
      { key: "lower", label: "KC low", seriesType: "line", colourToken: "--chart-ma-slow", lineWidth: 1 },
    ],
    compute: (bars) => keltner(bars, 20, 1.5),
    unavailable: (barCount) => `needs 21 bars, have ${barCount}`,
  },

  {
    id: "donchian",
    label: "Donchian",
    group: "overlay",
    minBars: 20,
    plots: [
      { key: "upper", label: "DC high", seriesType: "line", colourToken: "--chart-up", lineWidth: 1 },
      { key: "lower", label: "DC low", seriesType: "line", colourToken: "--chart-down", lineWidth: 1 },
    ],
    compute: (bars) => donchian(bars, 20),
    unavailable: (barCount) => `needs 20 bars, have ${barCount}`,
  },

  // Lower pane. One at a time: RSI is bounded 0 to 100 and MACD is unbounded and
  // centred on zero, so sharing an axis would put one of them in a corner and label it
  // with the other's scale. The chart enforces the swap and the chip says so.
  {
    id: "rsi",
    label: "RSI",
    paramLabel: "14",
    group: "lower",
    minBars: 15,
    bounds: { min: 0, max: 100, guides: [30, 70] },
    plots: [
      { key: "value", label: "RSI14", seriesType: "line", colourToken: "--accent", lineWidth: 1.4 },
    ],
    compute: (bars) => ({ value: rsi(bars, 14) }),
    unavailable: (barCount) => `needs 15 bars, have ${barCount}`,
  },

  {
    id: "macd",
    label: "MACD",
    paramLabel: "12,26,9",
    group: "lower",
    minBars: 35,
    bounds: { guides: [0] },
    plots: [
      { key: "histogram", label: "hist", seriesType: "histogram", colourToken: "--chart-axis" },
      { key: "macd", label: "MACD", seriesType: "line", colourToken: "--accent", lineWidth: 1.4 },
      { key: "signal", label: "signal", seriesType: "line", colourToken: "--chart-ma-fast", lineWidth: 1.1 },
    ],
    compute: (bars) => macd(bars, 12, 26, 9),
    unavailable: (barCount) => `needs 35 bars, have ${barCount}`,
  },

  {
    id: "stoch",
    label: "Stochastic",
    group: "lower",
    minBars: 16,
    bounds: { min: 0, max: 100, guides: [20, 80] },
    plots: [
      { key: "k", label: "%K", seriesType: "line", colourToken: "--accent", lineWidth: 1.3 },
      { key: "d", label: "%D", seriesType: "line", colourToken: "--chart-ma-fast", lineWidth: 1.1 },
    ],
    compute: (bars) => stochastic(bars, 14, 3),
    unavailable: (barCount) => `needs 16 bars, have ${barCount}`,
  },

  {
    id: "atr",
    label: "ATR",
    group: "lower",
    minBars: 15,
    plots: [
      { key: "value", label: "ATR14", seriesType: "line", colourToken: "--chart-ma-slow", lineWidth: 1.3 },
    ],
    compute: (bars) => ({ value: atr(bars, 14) }),
    unavailable: (barCount) => `needs 15 bars, have ${barCount}`,
  },

  {
    id: "relvol",
    label: "Rel volume",
    group: "lower",
    minBars: 21,
    // One is average. The guide is what makes the series readable at a glance, which
    // is the whole reason to plot a ratio rather than raw volume.
    bounds: { min: 0, guides: [1] },
    plots: [
      { key: "value", label: "rel vol", seriesType: "histogram", colourToken: "--accent" },
    ],
    compute: (bars) => ({ value: relativeVolume(bars, 20) }),
    unavailable: (barCount) =>
      barCount < 21 ? `needs 21 bars, have ${barCount}` : "no volume on these bars",
  },
];

/** Indicators that draw on the price pane. */
export const OVERLAYS = INDICATORS.filter((item) => item.group === "overlay");

/** Indicators that need the lower pane. At most one is shown at a time. */
export const LOWER = INDICATORS.filter((item) => item.group === "lower");

export const INDICATORS_BY_ID = new Map(
  INDICATORS.map((indicator) => [indicator.id, indicator]),
);

// Default off, so the chart opens on price alone. Every indicator is one click away.
export const DEFAULT_ENABLED = [];

/** The last value of a computed plot, for the chip readout. Null when it cannot draw. */
export function latestValue(series) {
  if (!series || series.length === 0) return null;
  return series[series.length - 1].value;
}
