// The whole API surface, in one file.
//
// Every call goes through `request`, which turns a non-2xx into an Error carrying the
// server's own `detail` string. Those strings are written to be shown: "No stored
// snapshot for NVDA. Run `optscan snapshot` to capture one." is more useful in a
// banner than "404", and the panels render it verbatim rather than replacing it with
// something generic.

const BASE = "/api";

async function request(path, options) {
  const response = await fetch(`${BASE}${path}`, options);
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      if (body && body.detail) {
        detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
      }
    } catch {
      // A non-JSON error body is still an error. Keep the status line.
    }
    throw new Error(detail);
  }
  return response.json();
}

function query(params) {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === "") continue;
    if (Array.isArray(value)) {
      value.forEach((item) => search.append(key, item));
    } else {
      search.append(key, value);
    }
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}

export const api = {
  health: () => request("/health"),
  liveStatus: () => request("/live/status"),
  positions: () => request("/positions"),
  watchlist: () => request("/watchlist"),
  // Polled. Deliberately separate from /watchlist, which seeds a table and stats a
  // directory per symbol -- none of which changes between two ticks of a price.
  quotes: (symbols) => request(`/quotes${query({ symbols: symbols.join(",") })}`),
  // Drops the server's short-lived caches. Fetches nothing itself: the panels the
  // browser has open do that, so a refresh costs what is on screen and no more.
  refresh: () => request("/refresh", { method: "POST" }),
  // The statement goes up as plain text rather than a multipart upload, which keeps a
  // parsing dependency out of the server for what is a CSV. Safe to repeat: the ledger
  // keys rows on their own contents, so an overlapping export adds only what is new.
  importStatement: (text) =>
    request("/journal/import", {
      method: "POST",
      headers: { "content-type": "text/csv" },
      body: text,
    }),
  home: () => request("/home"),
  catalogue: ({ q, group, onlyWatchlist, onlyScreenable, limit } = {}) =>
    request(
      `/catalogue${query({
        q,
        group,
        only_watchlist: onlyWatchlist || undefined,
        only_screenable: onlyScreenable || undefined,
        limit,
      })}`,
    ),
  // The only two calls in this client that write anything. Both are idempotent, and
  // both return a note the caller is expected to show: adding a symbol does not
  // capture a chain, it makes the next snapshot run fetch one.
  pin: (symbol) =>
    request(`/watchlist/${encodeURIComponent(symbol)}`, { method: "POST" }),
  unpin: (symbol) =>
    request(`/watchlist/${encodeURIComponent(symbol)}`, { method: "DELETE" }),
  symbol: (symbol) => request(`/symbols/${encodeURIComponent(symbol)}`),
  chain: (symbol, expiry) =>
    request(`/symbols/${encodeURIComponent(symbol)}/chain${query({ expiry })}`),
  // `session` asks for one named day at an intraday interval, which is a different
  // question from a rolling window and is why it is not just another `days` value.
  history: (symbol, { days, interval, session } = {}) =>
    request(
      `/symbols/${encodeURIComponent(symbol)}/history${query({ days, interval, session })}`,
    ),
  // Never cached. News ages out of relevance in hours, so a cached copy would look
  // current and be a day old.
  news: (symbol, limit) =>
    request(`/symbols/${encodeURIComponent(symbol)}/news${query({ limit })}`),
  levels: (symbol, days, expiry) =>
    request(`/symbols/${encodeURIComponent(symbol)}/levels${query({ days, expiry })}`),
  // near_miss is opt in on the server because collecting it re-checks every rejected
  // candidate against every gate. Only the Best plays view asks for it.
  scan: (symbols, limit, { nearMiss = false } = {}) =>
    request(`/scan${query({ symbols, limit, near_miss: nearMiss || undefined })}`),
  gaps: (symbols) => request(`/gaps${query({ symbols })}`),
  // `exclude` is a list of closing days and `normalize` scales every trade to the
  // median risk: a what-if view the server computes and never saves.
  journal: ({ symbol, strategy, tag, dte, exclude, normalize } = {}) =>
    request(
      `/journal${query({ symbol, strategy, tag, dte, exclude, normalize: normalize || undefined })}`,
    ),
  // What the trader adds to a position. The editor always sends every field, so a
  // cleared box clears the stored value rather than leaving the old one behind.
  saveAnnotation: (payload) =>
    request("/journal/annotation", {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(payload),
    }),
  // The image goes up raw, typed by its own header; it never leaves this machine.
  uploadScreenshot: (key, file) =>
    request(`/journal/screenshot${query({ key, name: file.name })}`, {
      method: "POST",
      headers: { "content-type": file.type },
      body: file,
    }),
  deleteScreenshot: (id) => request(`/journal/screenshot/${id}`, { method: "DELETE" }),
  screenshotUrl: (id) => `${BASE}/journal/screenshot/${id}`,
  setBalance: (value) =>
    request("/journal/balance", {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ starting_balance: value }),
    }),
  fetchRegime: () => request("/journal/regime", { method: "POST" }),
  journalExportUrl: (kind) => `${BASE}/journal/export.csv${query({ kind })}`,
  // Two calls, deliberately not one. `signals` evaluates and delivers nothing, so a
  // dashboard refresh cannot consume the once-per-session suppression the scheduled
  // scan relies on. `recentSignals` reads the record of what was actually delivered.
  signals: (symbol) => request(`/signals${query({ symbol })}`),
  // The evidence comes back in the same payload as the symbols, deliberately. A
  // separate call for it is the design that ends up showing tickers with no numbers.
  ideas: ({ freshness, provisional } = {}) =>
    request(`/ideas${query({ freshness, provisional: provisional || undefined })}`),
  backtestRules: () => request("/backtest/rules"),
  // The one POST in this client that computes rather than writes. It can take seconds,
  // so callers show a running state rather than assuming a request is instant.
  runBacktest: (spec) =>
    request("/backtest", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(spec),
    }),
  recentSignals: ({ days, symbol, minSeverity } = {}) =>
    request(`/signals/recent${query({ days, symbol, min_severity: minSeverity })}`),
  payoff: (body) =>
    request("/payoff", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
};
