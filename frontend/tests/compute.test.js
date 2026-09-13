// compute.js against levels.py, through a golden file both sides are checked against.
//
// The chart draws these indicators, the alerts fire on levels.py's, and the backtests
// measure rules.py's. The day they drift, the chart shows one line and the alert fires
// on another, and nothing fails loudly. tests/test_indicator_golden.py pins the golden
// file to levels.py and runs this file, so the Python suite fails when this does.
//
// Run on its own with `npm test` from frontend/. Node's built in runner: no framework.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import { atr, bollinger, ema, keltner, rsi } from "../src/components/chart/compute.js";

const fixtures = new URL("../../tests/fixtures/", import.meta.url);
const read = (name) => JSON.parse(readFileSync(new URL(name, fixtures), "utf8"));

const fixture = read("spy_daily_bars.json");
const golden = read("indicator_golden.json");

const bars = fixture.bars.map((row) => ({
  time: row.ts,
  open: row.open,
  high: row.high,
  low: row.low,
  close: row.close,
  volume: row.volume,
}));

// Relative. The Bollinger band is summed differently on each side (a rolling sum of
// squares here, two passes there), which moves the last few digits and nothing more.
const TOLERANCE = 1e-9;

const series = {
  rsi14: rsi(bars, 14),
  ema20: ema(bars, 20),
  atr14: atr(bars, 14),
};
const bands = bollinger(bars, 20, 2);
const channels = keltner(bars, 20, 1.5);
const lines = {
  ...series,
  bb_middle: bands.middle,
  bb_upper: bands.upper,
  bb_lower: bands.lower,
  kc_middle: channels.middle,
  kc_upper: channels.upper,
  kc_lower: channels.lower,
};

function valueAt(line, time) {
  const point = line.find((candidate) => candidate.time === time);
  return point === undefined ? undefined : point.value;
}

for (const point of golden.points) {
  test(`compute.js matches levels.py at bar ${point.index}`, () => {
    const time = bars[point.index].time;
    assert.equal(time, point.ts, "the golden file was written from a different fixture");

    for (const [name, line] of Object.entries(lines)) {
      const actual = valueAt(line, time);
      const expected = point[name];
      assert.notEqual(actual, undefined, `${name}: compute.js has no point on this bar`);
      const scale = Math.max(1, Math.abs(expected));
      assert.ok(
        Math.abs(actual - expected) <= TOLERANCE * scale,
        `${name}: compute.js ${actual}, levels.py ${expected}`,
      );
    }
  });
}
