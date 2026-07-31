import { useEffect, useState } from "react";
import { api } from "./api.js";
import { ErrorBox, Provenance, useAsync } from "./components/common.jsx";
import Chain from "./views/Chain.jsx";
import Opportunities from "./views/Opportunities.jsx";
import Payoff from "./views/Payoff.jsx";
import Underlying from "./views/Underlying.jsx";
import { num } from "./format.js";

// The shell: pick a symbol, pick a view.
//
// The header says where the numbers came from and how old they are, on every screen.
// That is not a nicety. This whole dashboard is built on the last stored capture, and
// a UI that does not say so continuously is one where somebody eventually reads a
// three day old mark as the current market.

const VIEWS = [
  { key: "opportunities", label: "Opportunities" },
  { key: "chain", label: "Chain" },
  { key: "underlying", label: "Underlying" },
  { key: "payoff", label: "Payoff" },
];

export default function App() {
  const health = useAsync(() => api.health(), []);
  const watchlist = useAsync(() => api.watchlist(), []);

  const [symbol, setSymbol] = useState(null);
  const [view, setView] = useState("opportunities");
  const [expiry, setExpiry] = useState(null);
  const [handover, setHandover] = useState(null);

  // Open on the first symbol that actually has data. Landing on an empty one and
  // showing a 404 is a bad first run when a captured symbol is sitting right there.
  useEffect(() => {
    if (symbol || !watchlist.data) return;
    const withData = watchlist.data.symbols.find((name) => watchlist.data.captured[name]);
    setSymbol(withData || watchlist.data.symbols[0] || null);
  }, [watchlist.data, symbol]);

  const summary = useAsync(() => api.symbol(symbol), [symbol], { enabled: Boolean(symbol) });

  // Null means "let the server choose". It picks the first expiry at or beyond the
  // screen's own minimum DTE, which is a config value the browser has no business
  // knowing. Opening on the front expiry would land on a zero or one day chain, whose
  // volatility is the least representative on the board.
  useEffect(() => {
    if (!summary.data) return;
    const listed = summary.data.expiries.map((item) => item.expiry);
    if (expiry && !listed.includes(expiry)) setExpiry(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [summary.data]);

  useEffect(() => {
    setExpiry(null);
  }, [symbol]);

  const openPayoff = (opportunity) => {
    setSymbol(opportunity.symbol);
    setHandover(opportunity);
    setView("payoff");
  };

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand">
          optscan
          <small>
            {health.data ? `${health.data.provider} v${health.data.version}` : "connecting"}
          </small>
        </div>

        <div>
          <div className="section-label">Symbols</div>
          <div className="symbol-list">
            {(watchlist.data?.symbols || []).map((name) => {
              const captured = watchlist.data.captured[name];
              return (
                <button
                  key={name}
                  type="button"
                  className={`symbol-btn ${name === symbol ? "active" : ""} ${
                    captured ? "" : "empty"
                  }`}
                  onClick={() => setSymbol(name)}
                  title={captured ? `last captured ${captured}` : "never captured"}
                >
                  {name}
                  <span className="captured">{captured || "no data"}</span>
                </button>
              );
            })}
          </div>
        </div>

        <div>
          <div className="section-label">Views</div>
          <div className="nav-list">
            {VIEWS.map((item) => (
              <button
                key={item.key}
                type="button"
                className={`nav-btn ${view === item.key ? "active" : ""}`}
                onClick={() => setView(item.key)}
              >
                {item.label}
              </button>
            ))}
          </div>
        </div>

        <div style={{ marginTop: "auto", padding: "0 16px" }} className="provenance">
          {health.data && !health.data.realtime && (
            <>
              Delayed data. This tool ranks and displays. It never places orders, and its
              scores have not been validated against outcomes.
            </>
          )}
        </div>
      </aside>

      <main className="main">
        <div className="topbar">
          <h1>{symbol || "no symbol"}</h1>
          {summary.data && <span className="spot">{num(summary.data.spot)}</span>}
          {summary.data && <Provenance provenance={summary.data.provenance} />}
        </div>

        <ErrorBox error={watchlist.error} />
        <ErrorBox error={summary.error} />
        {summary.loading && <div className="loading">Loading {symbol}...</div>}

        {view === "opportunities" && (
          <Opportunities symbols={symbol ? [symbol] : []} onOpenPayoff={openPayoff} />
        )}

        {view === "chain" && summary.data && (
          <Chain
            symbol={symbol}
            expiries={summary.data.expiries}
            expiry={expiry}
            onExpiry={setExpiry}
          />
        )}

        {view === "underlying" && summary.data && <Underlying summary={summary.data} />}

        {view === "payoff" && summary.data && (
          <Payoff
            symbol={symbol}
            expiries={summary.data.expiries}
            initialPosition={handover}
            onConsumed={() => setHandover(null)}
          />
        )}
      </main>
    </div>
  );
}
