// Daily bars to weekly and monthly candles.
//
// The API serves daily bars and nothing else, so a weekly or monthly candle is built
// here rather than fetched. That is deliberate: resampling costs one pass over data
// already in memory, while a second interval on the wire would mean more requests
// against a vendor that throttles silently and whose throttling would take the 15:45
// snapshot job down with it.
//
// Two rules this file exists to hold:
//
//   - **A partial trailing period is kept and is not marked.** The current week or
//     month is real and forming, and a trader wants to see it. Dropping it would hide
//     the most recent price action, which is the opposite of useful.
//   - **A period containing any unknown volume has unknown volume.** Summing the bars
//     that happen to carry a number would publish a partial total as if it were the
//     period's volume, which is the null-is-not-zero rule in its most tempting form.

export const INTERVALS = {
  "1D": { id: "1D", label: "1D", unit: "day" },
  "1W": { id: "1W", label: "1W", unit: "week" },
  "1M": { id: "1M", label: "1M", unit: "month" },
};

// Dates arrive as plain ISO days. Parsing them with the Date constructor would read
// them as UTC midnight and then answer day-of-week questions in local time, which on
// this machine is seven or eight hours behind and silently moves a Monday to a Sunday.
// Every calculation here is done in UTC on integers for that reason.
function parseIso(iso) {
  const [year, month, day] = iso.split("-").map(Number);
  return { year, month, day };
}

function toIso(timestamp) {
  return new Date(timestamp).toISOString().slice(0, 10);
}

const DAY_MS = 86400000;

/** The Monday of the ISO week a date falls in. */
function weekStart(iso) {
  const { year, month, day } = parseIso(iso);
  const timestamp = Date.UTC(year, month - 1, day);
  const weekday = new Date(timestamp).getUTCDay(); // 0 is Sunday
  const sinceMonday = (weekday + 6) % 7;
  return toIso(timestamp - sinceMonday * DAY_MS);
}

/** The first day of the month a date falls in. */
function monthStart(iso) {
  const { year, month } = parseIso(iso);
  return toIso(Date.UTC(year, month - 1, 1));
}

function bucketKey(iso, interval) {
  if (interval === "1W") return weekStart(iso);
  if (interval === "1M") return monthStart(iso);
  return iso;
}

/**
 * Collapse daily bars onto the given interval.
 *
 * Bars must already be sorted ascending, which the API guarantees. The result carries
 * the period's opening day as its time, so the axis reads as the start of the week or
 * month rather than as whichever day happened to close it.
 */
export function aggregate(bars, interval) {
  if (!bars || bars.length === 0) return [];
  if (interval === "1D") return bars;

  const out = [];
  let current = null;
  let currentKey = null;

  for (const bar of bars) {
    const key = bucketKey(bar.time, interval);
    if (key !== currentKey) {
      if (current) out.push(current);
      currentKey = key;
      current = {
        time: key,
        open: bar.open,
        high: bar.high,
        low: bar.low,
        close: bar.close,
        // An absent volume poisons the period's total rather than being treated as
        // zero. See the header.
        volume: bar.volume === null || bar.volume === undefined ? null : bar.volume,
      };
      continue;
    }

    current.high = Math.max(current.high, bar.high);
    current.low = Math.min(current.low, bar.low);
    current.close = bar.close;
    if (bar.volume === null || bar.volume === undefined) {
      current.volume = null;
    } else if (current.volume !== null) {
      current.volume += bar.volume;
    }
  }

  if (current) out.push(current);
  return out;
}
