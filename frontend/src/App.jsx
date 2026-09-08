import { useEffect, useState } from "react";
import { api } from "./api.js";
import {
  ApiDown,
  ConnectionBadge,
  ErrorBoundary,
  ErrorBox,
  Provenance,
  useAsync,
} from "./components/common.jsx";
import { CONNECTION, useLive } from "./live.js";
import SymbolSearch from "./components/SymbolSearch.jsx";
import BestPlays from "./views/BestPlays.jsx";
import Browse from "./views/Browse.jsx";
import Home from "./views/Home.jsx";
import Chain from "./views/Chain.jsx";
import Journal from "./views/Journal.jsx";
import Levels from "./views/Levels.jsx";
import Opportunities from "./views/Opportunities.jsx";
import Payoff from "./views/Payoff.jsx";
import Positions from "./views/Positions.jsx";
import Underlying from "./views/Underlying.jsx";
import { num } from "./format.js";

// The shell: pick a symbol, pick a view.
//
// The header says where the numbers came from and how old they are, on every screen.
// That is not a nicety. This whole dashboard is built on the last stored capture, and
// a UI that does not say so continuously is one where somebody eventually reads a
// three day old mark as the current market.

const VIEWS = [
  { key: "home", label: "Home" },
  { key: "best", label: "Best plays" },
  { key: "browse", label: "Browse" },
  { key: "opportunities", label: "Opportunities" },
  { key: "chain", label: "Chain" },
  { key: "underlying", label: "Underlying" },
  { key: "levels", label: "Levels" },
  { key: "payoff", label: "Payoff" },
  { key: "positions", label: "Positions" },
  { key: "journal", label: "Journal" },
];

export default function App() {
  const health = useAsync(() => api.health(), []);
  const watchlist = useAsync(() => api.watchlist(), []);
  const liveStatus = useAsync(() => api.liveStatus(), []);

  const [symbol, setSymbol] = useState(null);
  const [view, setView] = useState("home");
  const [expiry, setExpiry] = useState(null);
  const [handover, setHandover] = useState(null);
  const [browseGroup, setBrowseGroup] = useState(null);
  // Drawer state. Only has an effect below the mobile breakpoint, where the
  // sidebar is off canvas; on a wide screen the class is inert.
  const [navOpen, setNavOpen] = useState(false);

  // Subscribed only while a view that can actually show live numbers is open. A
  // stream held open behind the payoff diagram would spend the request budget
  // refreshing a chain nobody is looking at.
  const wantsLive = Boolean(liveStatus.data?.state && liveStatus.data.state !== "disabled");
  const live = useLive(symbol, expiry, { enabled: wantsLive && view === "chain" });

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

  useEffect(() => {
    setNavOpen(false);
  }, [view, symbol]);

  // /health is the cheapest endpoint there is, so its failure is the clearest evidence
  // that the server itself is gone rather than one request having gone wrong.
  const apiDown = Boolean(health.error);
  const retryConnection = () => {
    health.reload();
    watchlist.reload();
    liveStatus.reload();
  };

  const openPayoff = (opportunity) => {
    setSymbol(opportunity.symbol);
    setHandover(opportunity);
    setView("payoff");
  };

  return (
    <div className={`app ${navOpen ? "nav-open" : ""}`}>
      <button
        type="button"
        className="nav-backdrop"
        aria-label="Close navigation"
        onClick={() => setNavOpen(false)}
      />

      <aside className="sidebar">
        <div className="brand">
          optscan
          <small className={health.error ? "bad" : undefined}>
            {health.data
              ? `${health.data.provider} v${health.data.version}`
              : health.error
                ? "no connection"
                : "connecting"}
          </small>
        </div>

        <div>
          <div className="section-label">Search</div>
          <SymbolSearch
            groups={{}}
            onSelect={(entry) => {
              setSymbol(entry.symbol);
              if (view === "home" || view === "browse") setView("underlying");
            }}
            onChanged={() => watchlist.reload()}
          />
        </div>

        <div>
          <div className="section-label">Pinned</div>
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
              {health.data.delay_minutes
                ? `Data is delayed by ${health.data.delay_minutes} minutes. `
                : "Delayed data. "}
              This tool ranks and displays. It never places orders, and its scores have
              not been validated against outcomes.
            </>
          )}
        </div>
      </aside>

      <main className="main">
        <div className="topbar">
          <button
            type="button"
            className="nav-toggle"
            aria-label="Menu"
            aria-expanded={navOpen}
            onClick={() => setNavOpen((open) => !open)}
          >
            ☰
          </button>
          <h1>{view === "home" ? "optscan" : view === "browse" ? "Browse" : symbol || "no symbol"}</h1>
          {summary.data && <span className="spot">{num(summary.data.spot)}</span>}
          {summary.data && <Provenance provenance={summary.data.provenance} />}
          <ConnectionBadge
            status={live.status || liveStatus.data}
            connection={wantsLive ? live.connection : CONNECTION.CLOSED}
            cycle={live.cycle}
          />
        </div>

        {/* One cause, one message. When the server is unreachable every panel below
            fails too, and three stacked copies of the same outage say less than one
            sentence naming it. */}
        <ApiDown error={health.error} onRetry={retryConnection} />
        {!apiDown && <ErrorBox error={watchlist.error} onRetry={watchlist.reload} />}
        {/* Not shown on views that work without a capture. Underlying draws its chart
            from stored daily bars, so "No stored snapshot for AA" here would contradict
            the calmer note the view itself renders, and say it in red first. */}
        {!apiDown && view !== "underlying" && (
          <ErrorBox error={summary.error} onRetry={summary.reload} />
        )}
        {summary.loading && <div className="loading">Loading {symbol}...</div>}

        {/* Keyed on the view alone so a crash in one panel clears when you navigate
            away rather than following you around the app. Deliberately not keyed on
            the symbol too: the symbol resolves a moment after load, and remounting on
            it put a crashed panel straight back into the crash, which loops until
            React escalates past the boundary and blanks the page.

            Not rendered at all during an outage. Every panel fetches from the same
            server, so leaving them mounted repeats the same failure once per panel
            underneath the sentence that already explained it. */}
        <ErrorBoundary key={view}>
          {!apiDown && view === "home" && (
            <Home
              onSelect={(name) => {
                setSymbol(name);
                setView("underlying");
              }}
              onGoto={(next, options) => {
                if (options?.group) setBrowseGroup(options.group);
                setView(next);
              }}
            />
          )}

          {!apiDown && view === "browse" && (
            <Browse
              group={browseGroup}
              onSelectGroup={setBrowseGroup}
              onSelect={(name) => {
                setSymbol(name);
                setView("underlying");
              }}
            />
          )}

          {!apiDown && view === "best" && (
            <BestPlays
              onOpenPayoff={openPayoff}
              onSeeAll={() => setView("opportunities")}
            />
          )}

          {!apiDown && view === "opportunities" && (
            <Opportunities symbols={symbol ? [symbol] : []} onOpenPayoff={openPayoff} />
          )}

          {view === "chain" && summary.data && (
            <Chain
              symbol={symbol}
              expiries={summary.data.expiries}
              expiry={expiry}
              onExpiry={setExpiry}
              live={live}
            />
          )}

          {/* Not gated on summary.data: a symbol with stored prices and no captured
              chain still has a chart worth drawing, and gating made every unpinned
              ticker a dead page. */}
          {view === "underlying" && symbol && (
            <Underlying symbol={symbol} summary={summary.data} />
          )}

          {view === "levels" && summary.data && <Levels summary={summary.data} />}

          {/* Not gated on a symbol: the portfolio spans every symbol held, and a
              positions page that went blank because the sidebar selection had no
              capture would be hiding open risk. */}
          {!apiDown && view === "positions" && <Positions />}

          {!apiDown && view === "journal" && <Journal />}

          {view === "payoff" && summary.data && (
            <Payoff
              symbol={symbol}
              expiries={summary.data.expiries}
              initialPosition={handover}
              onConsumed={() => setHandover(null)}
            />
          )}
        </ErrorBoundary>
      </main>
    </div>
  );
}
