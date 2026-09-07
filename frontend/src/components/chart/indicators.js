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
//   group:     "overlay" draws on the price pane. "oscillator" is reserved for a
//              separate pane and is not yet handled by the chart.
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

// Order here is the order of the chips.
export const INDICATORS = [
  movingAverage(20, "--chart-ma-fast"),
  movingAverage(50, "--chart-ma-slow"),
  movingAverage(200, "--chart-axis"),
];

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
