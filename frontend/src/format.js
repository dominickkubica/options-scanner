// Display formatting, and the one rule that matters most in it.
//
// Null is not zero. Every formatter here returns "n/a" for null or undefined and
// never falls back to 0, because a table cell reading 0.00 for "the solver refused
// this contract" is a lie the reader cannot see. The API is careful to send null; it
// would be pointless for the UI to undo that with `value || 0`.

export const MISSING = "n/a";

function absent(value) {
  return value === null || value === undefined || Number.isNaN(value);
}

export function num(value, digits = 2) {
  return absent(value) ? MISSING : Number(value).toFixed(digits);
}

export function pct(value, digits = 1) {
  return absent(value) ? MISSING : `${(Number(value) * 100).toFixed(digits)}%`;
}

export function vol(value) {
  // Implied vol is quoted in vol points, which is how anyone trading it says it.
  return absent(value) ? MISSING : `${(Number(value) * 100).toFixed(1)}`;
}

export function money(value, digits = 0) {
  if (absent(value)) return MISSING;
  const sign = value < 0 ? "-" : "";
  return `${sign}$${Math.abs(Number(value)).toFixed(digits)}`;
}

export function count(value) {
  return absent(value) ? MISSING : Number(value).toLocaleString();
}

// A count short enough to sit in a chart header. `count` is right in a table, where
// 39,551,200 lines up under its neighbours; it is wrong in a readout row where the
// only question is the order of magnitude. Same null rule as everything else here.
export function compact(value) {
  if (absent(value)) return MISSING;
  const size = Math.abs(Number(value));
  if (size >= 1e9) return `${(Number(value) / 1e9).toFixed(2)}B`;
  if (size >= 1e6) return `${(Number(value) / 1e6).toFixed(1)}M`;
  if (size >= 1e3) return `${(Number(value) / 1e3).toFixed(1)}K`;
  return String(Math.round(Number(value)));
}

export function age(seconds) {
  if (absent(seconds)) return MISSING;
  const minutes = seconds / 60;
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${Math.round(minutes)}m ago`;
  const hours = minutes / 60;
  if (hours < 48) return `${Math.round(hours)}h ago`;
  return `${Math.round(hours / 24)}d ago`;
}

// An elapsed span, not a point in the past. `age` says "1m ago", which is right after
// "updated" and wrong after "for". Two formatters because they are two sentences.
export function duration(seconds) {
  if (absent(seconds)) return MISSING;
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const minutes = seconds / 60;
  if (minutes < 60) return `${Math.round(minutes)}m`;
  return `${Math.round(minutes / 60)}h`;
}

export function strategyLabel(value) {
  return String(value).replace(/_/g, " ");
}

export function reasonLabel(value) {
  return String(value).replace(/_/g, " ");
}

// A 0 to 1 score to a hue on a cold-to-hot ramp. Used for the chain heatmaps, where
// the point is relative position within the visible column rather than an absolute
// reading, so the caller normalizes first.
export function heat(fraction, alpha = 0.55) {
  if (absent(fraction)) return "transparent";
  const clamped = Math.min(Math.max(fraction, 0), 1);
  const hue = 210 - clamped * 210;
  return `hsla(${hue}, 72%, 48%, ${alpha * (0.25 + 0.75 * clamped)})`;
}

export function normalize(values) {
  const usable = values.filter((value) => !absent(value));
  if (usable.length === 0) return () => null;
  const low = Math.min(...usable);
  const high = Math.max(...usable);
  if (high === low) return (value) => (absent(value) ? null : 0.5);
  return (value) => (absent(value) ? null : (value - low) / (high - low));
}
