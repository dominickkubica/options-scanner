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
import Signals from "./views/Signals.jsx";
import Backtest from "./views/Backtest.jsx";
import Ideas from "./views/Ideas.jsx";
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
  { key: "ideas", label: "Trade ideas" },
  { key: "signals", label: "Signals" },
  { key: "backtest", label: "Backtest" },
  { key: "levels", label: "Levels" },
  { key: "payoff", label: "Payoff" },
  { key: "positions", label: "Positions" },
  { key: "journal", label: "Journal" },
];

// Views that are about the whole universe rather than the selected symbol, so the
// topbar names the view instead of a ticker the panel below is not showing.
//: The three things the pinned column can show, in the order the control cycles them.
//:
//: Price first because it is the one people read without thinking, then the move in
//: dollars, then in percent. The dollar and percent forms both come from the server
//: rather than one being derived from the other in here, so the rounding happens once.
const asPercent = (q) =>
  q.change_pct === null || q.change_pct === undefined
    ? "-"
    : `${q.change_pct >= 0 ? "+" : ""}${(q.change_pct * 100).toFixed(2)}%`;

const asChange = (q) =>
  q.change === null || q.change === undefined
    ? "-"
    : `${q.change >= 0 ? "+" : ""}${num(q.change)}`;

const QUOTE_MODES = {
  price: {
    title: "last price",
    render: (q) => num(q.last),
    secondary: asPercent,
  },
  change: {
    title: "change on the day, in dollars",
    render: asChange,
    secondary: asPercent,
  },
  percent: {
    title: "change on the day, in percent",
    render: asPercent,
    // The percent is already in the pill, so the line underneath shows the price
    // instead. Repeating the same number twice would waste the row, and the price is
    // the thing you still want to know while reading a percent.
    secondary: (q) => num(q.last),
    // A price has no direction of its own, so the line stays neutral rather than
    // borrowing the pill's colour. In the reference a green pill sits above a red
    // secondary line whenever the two disagree: each number is coloured by its own
    // sign, not by its neighbour's.
    secondaryTone: "neutral",
  },
};

const QUOTE_ORDER = ["price", "change", "percent"];
const QUOTE_STORAGE_KEY = "optscan.pinned.mode";

function readQuoteMode() {
  try {
    const stored = window.localStorage.getItem(QUOTE_STORAGE_KEY);
    return QUOTE_ORDER.includes(stored) ? stored : "price";
  } catch {
    return "price";
  }
}

const TITLES = {
  home: "optscan",
  browse: "Browse",
  signals: "Signals",
  backtest: "Backtest",
  ideas: "Trade ideas",
};

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
  // Which quantity the pinned list shows. Remembered per browser: it is a reading
  // preference, and being asked to set it again on every reload is the kind of small
  // friction that makes somebody stop using the control.
  const [quoteMode, setQuoteMode] = useState(readQuoteMode);

  const nextQuoteMode = () => {
    const next = QUOTE_ORDER[(QUOTE_ORDER.indexOf(quoteMode) + 1) % QUOTE_ORDER.length];
    try {
      window.localStorage.setItem(QUOTE_STORAGE_KEY, next);
    } catch {
      // A browser refusing storage still gets the change for this session.
    }
    return next;
  };

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
              const quote = watchlist.data.quotes?.[name];
              // The pill's colour always follows the day's direction, even in price
              // mode where the number itself carries no sign. That is what makes the
              // column scannable: the colour answers "which way" before the eye has
              // read a single digit.
              const dir =
                quote?.change === null || quote?.change === undefined
                  ? null
                  : quote.change >= 0
                    ? "up"
                    : "down";
              const mode = QUOTE_MODES[quoteMode];
              return (
                <div
                  key={name}
                  className={`symbol-row ${name === symbol ? "active" : ""} ${
                    captured ? "" : "empty"
                  }`}
                >
                  <button
                    type="button"
                    className="symbol-btn"
                    onClick={() => {
                      setSymbol(name);
                      // Straight to the chart. Picking a symbol from the sidebar and
                      // staying on the payoff diagram or the journal means the click
                      // appears to have done nothing, because the view showing is not
                      // the one that changed.
                      setView("underlying");
                    }}
                    title={
                      captured ? `chains captured ${captured}` : "no chains captured"
                    }
                  >
                    {name}
                  </button>
                  {quote ? (
                    /* A sibling of the symbol button rather than inside it: a button
                       nested in a button is invalid, and the browser does not deliver
                       the inner click reliably. */
                    <button
                      type="button"
                      className="quote-stack"
                      onClick={() => setQuoteMode(nextQuoteMode)}
                      title={`Showing ${mode.title}. Click to cycle.`}
                    >
                      <span className={`quote-pill ${dir || "flat"}`}>
                        {mode.render(quote)}
                      </span>
                      <span
                        className={`quote-sub ${
                          mode.secondaryTone === "neutral" ? "flat" : dir || "flat"
                        }`}
                      >
                        {mode.secondary(quote)}
                      </span>
                    </button>
                  ) : (
                    <span className="captured">no data</span>
                  )}
                </div>
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
          <h1>{TITLES[view] || symbol || "no symbol"}</h1>
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

          {/* Not gated on a symbol either: signals span every pinned symbol, and the
              panel's whole job is to tell you which one to go and look at. */}
          {!apiDown && view === "signals" && (
            <Signals
              onSelect={(name) => {
                setSymbol(name);
                setView("underlying");
              }}
            />
          )}

          {/* Not gated on a symbol: the list is the answer to "which symbol should I be
              looking at", so gating it on one would invert the point. */}
          {!apiDown && view === "ideas" && (
            <Ideas
              onSelect={(name) => {
                setSymbol(name);
                setView("underlying");
              }}
            />
          )}

          {/* Not gated on a symbol either: a backtest chooses its own universe, and
              the sidebar selection has nothing to do with it. */}
          {!apiDown && view === "backtest" && <Backtest />}

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
