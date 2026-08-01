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
  watchlist: () => request("/watchlist"),
  symbol: (symbol) => request(`/symbols/${encodeURIComponent(symbol)}`),
  chain: (symbol, expiry) =>
    request(`/symbols/${encodeURIComponent(symbol)}/chain${query({ expiry })}`),
  history: (symbol, days) =>
    request(`/symbols/${encodeURIComponent(symbol)}/history${query({ days })}`),
  levels: (symbol, days, expiry) =>
    request(`/symbols/${encodeURIComponent(symbol)}/levels${query({ days, expiry })}`),
  scan: (symbols, limit) => request(`/scan${query({ symbols, limit })}`),
  gaps: (symbols) => request(`/gaps${query({ symbols })}`),
  payoff: (body) =>
    request("/payoff", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
};
