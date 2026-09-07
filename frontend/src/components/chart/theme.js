// Bridging CSS custom properties into a canvas.
//
// lightweight-charts draws to a canvas, so it cannot read `var(--chart-up)` the way
// the SVG charts do. Without this module the palette would have to be duplicated as
// hex literals in JS, and the two copies would drift the first time either is touched.
// Reading the computed values at mount keeps styles.css the single source of truth for
// every chart in the app, canvas or not.
//
// Resolution happens once per mount rather than per draw. The app has no theme switch,
// so a value that changed after mount would be a stylesheet edit during development,
// and a reload covers that.

const FALLBACK = {
  "--chart-up": "#3fb98a",
  "--chart-down": "#e26762",
  "--chart-grid": "rgba(255,255,255,0.045)",
  "--chart-axis": "#7b8899",
  "--chart-crosshair": "#6b7684",
  "--chart-ma-fast": "#d9a441",
  "--chart-ma-slow": "#9d7bd8",
  "--accent": "#4c9be8",
  "--panel": "#1d2333",
  "--panel-2": "#2e3446",
  "--text": "#dae0e5",
  "--dim": "#6b7684",
};

// A token read before the stylesheet has applied comes back empty rather than throwing,
// which would paint the chart in the library's default blue and look like a bug in the
// chart rather than a race. The fallback table is the same palette, so a miss is
// invisible instead of wrong.
function token(styles, name) {
  const value = styles.getPropertyValue(name).trim();
  return value || FALLBACK[name] || "#000000";
}

/** Resolve every colour the chart needs, once. */
export function readChartTheme() {
  const styles = getComputedStyle(document.documentElement);
  const resolved = {};
  for (const name of Object.keys(FALLBACK)) {
    resolved[name] = token(styles, name);
  }

  return {
    up: resolved["--chart-up"],
    down: resolved["--chart-down"],
    grid: resolved["--chart-grid"],
    axis: resolved["--chart-axis"],
    crosshair: resolved["--chart-crosshair"],
    maFast: resolved["--chart-ma-fast"],
    maSlow: resolved["--chart-ma-slow"],
    accent: resolved["--accent"],
    panel: resolved["--panel"],
    panelRaised: resolved["--panel-2"],
    text: resolved["--text"],
    dim: resolved["--dim"],
  };
}

/** A token name to its resolved colour, for indicators that name their colour in CSS. */
export function themeColour(theme, tokenName) {
  const map = {
    "--chart-up": theme.up,
    "--chart-down": theme.down,
    "--chart-axis": theme.axis,
    "--chart-ma-fast": theme.maFast,
    "--chart-ma-slow": theme.maSlow,
    "--accent": theme.accent,
    "--dim": theme.dim,
  };
  return map[tokenName] || theme.axis;
}

// Hex to rgba, so one palette entry can serve both a solid line and a translucent fill
// without a second token holding the same colour at a different opacity. Anything that
// is already a functional colour is passed through: the grid token is authored as rgba
// because a grid line has no meaningful solid form.
export function withAlpha(colour, alpha) {
  if (!colour.startsWith("#")) return colour;
  const hex = colour.slice(1);
  const full =
    hex.length === 3
      ? hex
          .split("")
          .map((char) => char + char)
          .join("")
      : hex;
  const int = Number.parseInt(full, 16);
  if (Number.isNaN(int)) return colour;
  const r = (int >> 16) & 255;
  const g = (int >> 8) & 255;
  const b = int & 255;
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}
