// The maths behind the indicators, separated from the registry that declares them.
//
// Every function here keeps the rule the registry states and the vol solver states
// before it: **emit a point only where the full window exists.** A fourteen period RSI
// computed over six bars is a different statistic wearing the same label, and on a
// shared axis it is indistinguishable from the real thing. These lead with nothing
// rather than with a ramp, and the chip reports the shortfall instead.
//
// Rolling accumulators rather than a fresh slice per point throughout. At 200 periods
// over 1,300 bars the difference is a quarter of a million redundant additions.

/** Wilder's smoothing, which is what RSI and ATR are actually defined on. */
function wilder(values, period) {
  if (values.length < period) return [];
  const out = [];
  let average = values.slice(0, period).reduce((sum, value) => sum + value, 0) / period;
  out.push(average);
  for (let index = period; index < values.length; index += 1) {
    // The recursive form, not a rolling mean. Using a simple mean here is the common
    // error that makes an RSI disagree with every other platform by a point or two.
    average = (average * (period - 1) + values[index]) / period;
    out.push(average);
  }
  return out;
}

/** Exponential moving average over closes. */
export function ema(bars, period) {
  if (!bars || bars.length < period || period < 1) return [];
  const k = 2 / (period + 1);
  const out = [];
  // Seeded with the simple average of the first window, so the series does not spend
  // its first fifty bars converging from an arbitrary starting value.
  let value = bars.slice(0, period).reduce((sum, bar) => sum + bar.close, 0) / period;
  out.push({ time: bars[period - 1].time, value });
  for (let index = period; index < bars.length; index += 1) {
    value = bars[index].close * k + value * (1 - k);
    out.push({ time: bars[index].time, value });
  }
  return out;
}

/** Rolling standard deviation of closes, population form, aligned with `sma`. */
function rollingStdev(bars, period) {
  const out = [];
  let sum = 0;
  let sumSquares = 0;
  for (let index = 0; index < bars.length; index += 1) {
    const value = bars[index].close;
    sum += value;
    sumSquares += value * value;
    if (index >= period) {
      const drop = bars[index - period].close;
      sum -= drop;
      sumSquares -= drop * drop;
    }
    if (index >= period - 1) {
      const mean = sum / period;
      // Clamped at zero: floating point can leave a tiny negative variance on a flat
      // window, and Math.sqrt of that is NaN, which lightweight-charts refuses.
      const variance = Math.max(sumSquares / period - mean * mean, 0);
      out.push({ time: bars[index].time, mean, sd: Math.sqrt(variance) });
    }
  }
  return out;
}

/** Bollinger bands: a moving average and a band of `deviations` standard deviations. */
export function bollinger(bars, period = 20, deviations = 2) {
  if (!bars || bars.length < period) return { middle: [], upper: [], lower: [] };
  const rows = rollingStdev(bars, period);
  return {
    middle: rows.map((row) => ({ time: row.time, value: row.mean })),
    upper: rows.map((row) => ({ time: row.time, value: row.mean + deviations * row.sd })),
    lower: rows.map((row) => ({ time: row.time, value: row.mean - deviations * row.sd })),
  };
}

/** True range per bar. The first bar has no previous close, so it has no true range. */
function trueRanges(bars) {
  const out = [];
  for (let index = 1; index < bars.length; index += 1) {
    const bar = bars[index];
    const previousClose = bars[index - 1].close;
    out.push(
      Math.max(
        bar.high - bar.low,
        Math.abs(bar.high - previousClose),
        Math.abs(bar.low - previousClose),
      ),
    );
  }
  return out;
}

/** Average true range, Wilder smoothed. */
export function atr(bars, period = 14) {
  if (!bars || bars.length < period + 1) return [];
  const ranges = trueRanges(bars);
  const smoothed = wilder(ranges, period);
  // trueRanges drops the first bar, and wilder consumes `period` of what is left, so
  // the first value lands on bar index `period`.
  return smoothed.map((value, index) => ({ time: bars[index + period].time, value }));
}

/** Keltner channels: an EMA with a band of `multiple` ATRs. */
export function keltner(bars, period = 20, multiple = 1.5) {
  const centre = ema(bars, period);
  const range = atr(bars, period);
  if (centre.length === 0 || range.length === 0) return { middle: [], upper: [], lower: [] };

  const byTime = new Map(range.map((point) => [point.time, point.value]));
  const middle = [];
  const upper = [];
  const lower = [];
  for (const point of centre) {
    const width = byTime.get(point.time);
    // A centre line without a width is not a channel. Both sides exist or the point
    // does not, rather than drawing a bare EMA under the Keltner label.
    if (width === undefined) continue;
    middle.push(point);
    upper.push({ time: point.time, value: point.value + multiple * width });
    lower.push({ time: point.time, value: point.value - multiple * width });
  }
  return { middle, upper, lower };
}

/** Donchian channel: the highest high and lowest low of the last `period` bars. */
export function donchian(bars, period = 20) {
  if (!bars || bars.length < period) return { upper: [], lower: [] };
  const upper = [];
  const lower = [];
  for (let index = period - 1; index < bars.length; index += 1) {
    let high = -Infinity;
    let low = Infinity;
    for (let back = index - period + 1; back <= index; back += 1) {
      if (bars[back].high > high) high = bars[back].high;
      if (bars[back].low < low) low = bars[back].low;
    }
    upper.push({ time: bars[index].time, value: high });
    lower.push({ time: bars[index].time, value: low });
  }
  return { upper, lower };
}

/**
 * Volume weighted average price over a rolling window.
 *
 * A rolling window rather than a session anchor, because these are daily bars: a
 * session VWAP needs intraday data and would be a single point per day. On the
 * intraday chart the same function over the day's bars is the familiar one.
 *
 * A bar with no volume makes the window unanswerable rather than contributing zero
 * weight, which is the same null-is-not-zero rule the storage layer keeps.
 */
export function vwap(bars, period = 20) {
  if (!bars || bars.length < period) return [];
  const out = [];
  for (let index = period - 1; index < bars.length; index += 1) {
    let priceVolume = 0;
    let volume = 0;
    let usable = true;
    for (let back = index - period + 1; back <= index; back += 1) {
      const bar = bars[back];
      if (bar.volume === null || bar.volume === undefined) {
        usable = false;
        break;
      }
      const typical = (bar.high + bar.low + bar.close) / 3;
      priceVolume += typical * bar.volume;
      volume += bar.volume;
    }
    if (usable && volume > 0) {
      out.push({ time: bars[index].time, value: priceVolume / volume });
    }
  }
  return out;
}

/** Relative volume: this bar's volume against the average of the previous `period`. */
export function relativeVolume(bars, period = 20) {
  if (!bars || bars.length < period + 1) return [];
  const out = [];
  for (let index = period; index < bars.length; index += 1) {
    let sum = 0;
    let usable = bars[index].volume !== null && bars[index].volume !== undefined;
    for (let back = index - period; back < index && usable; back += 1) {
      const volume = bars[back].volume;
      if (volume === null || volume === undefined) usable = false;
      else sum += volume;
    }
    const average = sum / period;
    if (usable && average > 0) {
      out.push({ time: bars[index].time, value: bars[index].volume / average });
    }
  }
  return out;
}

/** Relative strength index, Wilder smoothed. */
export function rsi(bars, period = 14) {
  if (!bars || bars.length < period + 1) return [];
  const gains = [];
  const losses = [];
  for (let index = 1; index < bars.length; index += 1) {
    const change = bars[index].close - bars[index - 1].close;
    gains.push(Math.max(change, 0));
    losses.push(Math.max(-change, 0));
  }
  const avgGain = wilder(gains, period);
  const avgLoss = wilder(losses, period);

  return avgGain.map((gain, index) => {
    const loss = avgLoss[index];
    // No losses in the window is not a divide by zero, it is an RSI of 100: the
    // definition's limit, and the state a strong trend actually reaches.
    const value = loss === 0 ? 100 : 100 - 100 / (1 + gain / loss);
    return { time: bars[index + period].time, value };
  });
}

/** MACD line, signal line, and the histogram between them. */
export function macd(bars, fast = 12, slow = 26, signal = 9) {
  const fastLine = ema(bars, fast);
  const slowLine = ema(bars, slow);
  if (fastLine.length === 0 || slowLine.length === 0) {
    return { macd: [], signal: [], histogram: [] };
  }

  const fastByTime = new Map(fastLine.map((point) => [point.time, point.value]));
  const line = [];
  for (const point of slowLine) {
    const quick = fastByTime.get(point.time);
    if (quick !== undefined) line.push({ time: point.time, value: quick - point.value });
  }

  // The signal is an EMA of the MACD line, so it is seeded the same way: reuse `ema`
  // by presenting the line as bars rather than writing a second smoother that could
  // drift from the first.
  const signalLine = ema(
    line.map((point) => ({ time: point.time, close: point.value })),
    signal,
  );
  const signalByTime = new Map(signalLine.map((point) => [point.time, point.value]));

  const histogram = [];
  for (const point of line) {
    const smoothed = signalByTime.get(point.time);
    if (smoothed !== undefined) {
      histogram.push({ time: point.time, value: point.value - smoothed });
    }
  }
  return { macd: line, signal: signalLine, histogram };
}

/** Stochastic oscillator: %K over `period`, and %D as its `smooth` bar average. */
export function stochastic(bars, period = 14, smooth = 3) {
  if (!bars || bars.length < period) return { k: [], d: [] };
  const k = [];
  for (let index = period - 1; index < bars.length; index += 1) {
    let high = -Infinity;
    let low = Infinity;
    for (let back = index - period + 1; back <= index; back += 1) {
      if (bars[back].high > high) high = bars[back].high;
      if (bars[back].low < low) low = bars[back].low;
    }
    const span = high - low;
    // A window with no range has no position within it. Fifty is the honest midpoint
    // rather than a divide by zero, and it is what a flat window means.
    const value = span === 0 ? 50 : ((bars[index].close - low) / span) * 100;
    k.push({ time: bars[index].time, value });
  }

  const d = [];
  let sum = 0;
  for (let index = 0; index < k.length; index += 1) {
    sum += k[index].value;
    if (index >= smooth) sum -= k[index - smooth].value;
    if (index >= smooth - 1) d.push({ time: k[index].time, value: sum / smooth });
  }
  return { k, d };
}
