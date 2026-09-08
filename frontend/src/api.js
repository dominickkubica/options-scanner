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
  history: (symbol, days) =>
    request(`/symbols/${encodeURIComponent(symbol)}/history${query({ days })}`),
  levels: (symbol, days, expiry) =>
    request(`/symbols/${encodeURIComponent(symbol)}/levels${query({ days, expiry })}`),
  // near_miss is opt in on the server because collecting it re-checks every rejected
  // candidate against every gate. Only the Best plays view asks for it.
  scan: (symbols, limit, { nearMiss = false } = {}) =>
    request(`/scan${query({ symbols, limit, near_miss: nearMiss || undefined })}`),
  gaps: (symbols) => request(`/gaps${query({ symbols })}`),
  journal: ({ symbol, strategy } = {}) => request(`/journal${query({ symbol, strategy })}`),
  payoff: (body) =>
    request("/payoff", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
};
