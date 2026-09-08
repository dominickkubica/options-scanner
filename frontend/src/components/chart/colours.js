// Per-line colour overrides for the chart.
//
// ## Why these key on the plot rather than on the CSS token
//
// Indicators name their colour with a token like `--chart-ma-slow`, which keeps
// styles.css the single source of the palette and is the right default. It is the wrong
// unit to customise, though, because tokens are shared: Bollinger's upper, middle and
// lower lines are all `--chart-axis`, and Keltner's three are all `--chart-ma-slow`.
// Recolouring by token would repaint every band at once and could never make the upper
// line differ from the lower, which is most of what somebody wants when they say the
// bands should be adjustable.
//
// So an override is keyed `indicatorId.plotKey` — "bb.upper", "keltner.lower" — and the
// token remains the default underneath it. Nothing is written until something is
// changed, so the palette still lives in the stylesheet for everyone who never opens
// the picker.
//
// ## Storage
//
// localStorage, per browser. This is a per-viewer convenience rather than shared state:
// it never leaves the machine, and it is wrapped because a private window or a browser
// set to block site data throws on access rather than returning empty.

import { themeColour } from "./theme.js";

const STORAGE_KEY = "optscan.chart.colours";

//: Colours that are not part of any indicator but are the first thing people want to
//: change. Kept in the same map so one reset clears everything.
export const CORE_COLOURS = [
  { key: "candle.up", label: "Candle up", token: "--chart-up" },
  { key: "candle.down", label: "Candle down", token: "--chart-down" },
];

export function plotKey(indicatorId, plotKey_) {
  return `${indicatorId}.${plotKey_}`;
}

/** Overrides from storage, or an empty map if there are none or it cannot be read. */
export function loadColours() {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw);
    // A hand-edited or half-written entry should not take the chart down. Anything that
    // is not a string is dropped rather than handed to the canvas.
    return Object.fromEntries(
      Object.entries(parsed).filter(([, value]) => typeof value === "string"),
    );
  } catch {
    return {};
  }
}

export function saveColours(colours) {
  try {
    if (Object.keys(colours).length === 0) window.localStorage.removeItem(STORAGE_KEY);
    else window.localStorage.setItem(STORAGE_KEY, JSON.stringify(colours));
  } catch {
    // A browser refusing storage is not a reason to refuse the colour change. It
    // applies for this session and is forgotten on reload, which is the honest
    // degradation.
  }
}

/** The colour a plot should draw in: the override if there is one, else its token. */
export function resolvePlotColour(theme, colours, indicatorId, plot) {
  return colours[plotKey(indicatorId, plot.key)] || themeColour(theme, plot.colourToken);
}

/** The colour for one of the core entries above. */
export function resolveCoreColour(theme, colours, key) {
  const entry = CORE_COLOURS.find((item) => item.key === key);
  if (!entry) return theme.axis;
  return colours[key] || themeColour(theme, entry.token);
}

/**
 * Every adjustable line, as flat rows for the picker.
 *
 * Built from the indicator registry rather than listed by hand, so an indicator added
 * later is adjustable without anybody remembering to come back here.
 */
export function colourRows(indicators, theme, colours) {
  const rows = CORE_COLOURS.map((item) => ({
    key: item.key,
    group: "Price",
    label: item.label,
    value: resolveCoreColour(theme, colours, item.key),
    overridden: Boolean(colours[item.key]),
  }));

  for (const indicator of indicators) {
    for (const plot of indicator.plots) {
      const key = plotKey(indicator.id, plot.key);
      rows.push({
        key,
        group: indicator.label,
        label: plot.label,
        value: resolvePlotColour(theme, colours, indicator.id, plot),
        overridden: Boolean(colours[key]),
      });
    }
  }
  return rows;
}

/**
 * A hex string the browser's colour input will accept, or null.
 *
 * The input only speaks `#rrggbb`. Several palette entries are authored as rgba because
 * they are meant to be translucent, and handing one of those to the input silently
 * resets it to black, which looks like the picker corrupting the value.
 */
export function toHexInput(colour) {
  if (typeof colour !== "string") return null;
  if (/^#[0-9a-f]{6}$/i.test(colour)) return colour;
  if (/^#[0-9a-f]{3}$/i.test(colour)) {
    const [, r, g, b] = colour.match(/^#(.)(.)(.)$/i);
    return `#${r}${r}${g}${g}${b}${b}`;
  }
  const rgb = colour.match(/^rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)/i);
  if (rgb) {
    const hex = [rgb[1], rgb[2], rgb[3]]
      .map((part) => Number(part).toString(16).padStart(2, "0"))
      .join("");
    return `#${hex}`;
  }
  return null;
}
