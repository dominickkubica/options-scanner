import { useEffect, useMemo, useState } from "react";
import { api } from "./api.js";
import { useQuotes } from "./useQuotes.js";
import {
  ApiDown,
  ConnectionBadge,
  ErrorBoundary,
  ErrorBox,
  Provenance,
  QuoteAge,
  RefreshButton,
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
import Payoff from "./views/Payoff.jsx";
import Positions from "./views/Positions.jsx";
import Backtest from "./views/Backtest.jsx";
import Ideas from "./views/Ideas.jsx";
import Underlying from "./views/Underlying.jsx";
import News from "./views/News.jsx";
import Information from "./views/Information.jsx";
import KeyLevels from "./views/KeyLevels.jsx";
import { num } from "./format.js";

// The shell: pick a symbol, pick a view.
//
// The header says where the numbers came from and how old they are, on every screen.
// That is not a nicety. This whole dashboard is built on the last stored capture, and
// a UI that does not say so continuously is one where somebody eventually reads a
// three day old mark as the current market.

// Two levels of navigation, because the old flat list mixed two different kinds of
// thing. Half of those views answered a question about the whole account -- what should
// I look at, does this rule work, what do I hold -- and half answered a question about
// one ticker. Sitting in one column they read as thirteen equal choices, and picking a
// symbol then hunting for the right tab was the normal way to use the app.
//
// The sidebar now holds only the account-level views. Everything about a single symbol
// lives in a tab strip inside that symbol's page, which is where a broker puts it and
// where somebody already looking at a ticker expects to find it.
const MAIN_VIEWS = [
  { key: "home", label: "Home" },
  { key: "browse", label: "Browse" },
  { key: "ideas", label: "Trade ideas" },
  { key: "backtest", label: "Backtest" },
  { key: "positions", label: "Positions" },
  { key: "journal", label: "Journal" },
];

//: The per-symbol tabs, in reading order: what it did, what you can trade on it, what
//: is being said about it, what it is, what to watch, and what the screen likes.
//:
//: Payoff is here rather than in the sidebar because it is inherently about one
//: symbol's contracts. It is reached by clicking a play rather than by browsing to it,
//: so it sits last.
const TICKER_TABS = [
  { key: "chart", label: "Chart" },
  { key: "chain", label: "Options chain" },
  { key: "news", label: "News" },
  { key: "info", label: "Information" },
  { key: "levels", label: "Key levels" },
  { key: "best", label: "Best plays" },
  { key: "payoff", label: "Payoff" },
];

const TICKER_KEYS = new Set(TICKER_TABS.map((tab) => tab.key));

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

//: Account-level views name themselves in the header. A symbol page shows the ticker
//: instead, because the tab strip underneath already says which of its pages is open.
const TITLES = {
  home: "Epicfin",
  browse: "Browse",
  backtest: "Backtest",
  ideas: "Trade ideas",
  positions: "Positions",
  journal: "Journal",
};

export default function App() {
  const health = useAsync(() => api.health(), []);
  const watchlist = useAsync(() => api.watchlist(), []);
  const liveStatus = useAsync(() => api.liveStatus(), []);

  // Prices poll on their own clock; the watchlist itself does not. `watchlist` still
  // carries a first set so the pills paint immediately rather than after a second
  // round trip, and these overwrite them as soon as the first poll lands.
  const pinned = watchlist.data?.symbols || [];
  const [symbol, setSymbol] = useState(null);
  // The open symbol is quoted alongside the pins even when it is not pinned, because
  // the header price is the most prominent number in the app and it was reading its
  // value off the stored option chain -- a capture from 15:45 the previous session.
  // On a day AAPL was up 3.19% the header showed yesterday's 315.91 while the sidebar
  // pill beside it showed the live 325.40.
  const pinnedKey = pinned.join(",");
  const quoted = useMemo(
    () => [...new Set([...pinnedKey.split(",").filter(Boolean), symbol].filter(Boolean))],
    [pinnedKey, symbol],
  );
  const livePrices = useQuotes(quoted);
  const quotes = { ...(watchlist.data?.quotes || {}), ...livePrices.quotes };

  // What the pills are actually showing, in three words.
  //
  // Derived from the quotes themselves rather than from the poll's envelope, because
  // the first paint comes from /watchlist and has no envelope. The first version read
  // the delay off the poll state and therefore labelled a fifteen minute delayed price
  // "live" for as long as it took the first poll to land -- and forever in a tab that
  // never becomes visible. A quote now carries its own delay wherever it travels.
  //
  // The worst case wins: one stored close among six live quotes means the list as a
  // whole is a stored close, because the caveat has to cover the oldest pill on screen.
  const shown = Object.values(quotes);
  const delays = shown.map((quote) => quote.delay_minutes).filter((value) => value != null);
  const quoteFreshness = shown.some((quote) => quote.feed === "stored")
    ? "stored close"
    : delays.length
      ? `${Math.max(...delays)}m delayed`
      : shown.length === 0
        ? ""
        : shown.every((quote) => quote.realtime)
          ? "live"
          // A vendor that publishes no delay is not thereby real time, so this says
          // what is actually known rather than guessing zero.
          : "delay unknown";
  const quoteFreshnessHint = livePrices.note
    ? livePrices.note
    : delays.length
      ? `Consolidated tape, ${Math.max(...delays)} minutes behind. Real time ` +
        "consolidated quotes need an entitlement this account does not have."
      : "";

  const [view, setView] = useState("home");
  // Bumped by the refresh button. Panels that take it as a dependency refetch; those
  // that do not are reading from stores that did not age.
  const [refreshNonce, setRefreshNonce] = useState(0);
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

  // `refreshNonce` is a dependency so the button refetches the summary, which is the
  // panel carrying the chain's age and the spot the header shows.
  const summary = useAsync(() => api.symbol(symbol), [symbol, refreshNonce], {
    enabled: Boolean(symbol),
  });

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
  // Whether the main area is showing a symbol's page rather than an account-level one.
  const onTicker = TICKER_KEYS.has(view);

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
          Epicfin
          {/* The app's own version, not the data provider's name. Which vendor is
              serving is a per-panel fact and is already on every provenance badge; in
              the brand it read like the product was called yfinance. */}
          <small className={health.error ? "bad" : undefined}>
            {health.data
              ? `v${health.data.version}`
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
              if (view === "home" || view === "browse") setView("chart");
            }}
            onChanged={() => watchlist.reload()}
          />
        </div>

        <div>
          {/* The delay is stated, never absorbed. This account cannot buy a real
              time consolidated price at any polling rate -- feed=sip returns 403 --
              so the choice was fifteen minutes late and correct, or IEX real time at
              two percent of the volume. A late price that says it is late is safe to
              read; a wrong one that looks current is not. When the entitlement
              changes this label changes with it, because it is read off the feed. */}
          <div className="section-label">
            Pinned
            <span className="quote-freshness" title={quoteFreshnessHint}>
              {quoteFreshness}
            </span>
          </div>
          <div className="symbol-list">
            {(watchlist.data?.symbols || []).map((name) => {
              const captured = watchlist.data.captured[name];
              const quote = quotes[name];
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
                      setView("chart");
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
            {MAIN_VIEWS.map((item) => (
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

        {/* The standing sidebar disclaimer is gone, removed on request. It said the data
            was delayed and the scores unvalidated, and both remain true -- but it said
            them in the one place that could never be specific. The freshness of any
            given number is now stated next to that number: a quote age on the header
            price, "chain captured" on the option snapshot, "Nm delayed" over the pinned
            list, and a real age on every chart's provenance. A caveat attached to the
            thing it qualifies is worth more than a paragraph nobody reads twice. */}
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
          {/* The price and its provenance belong to a symbol, so they only appear on a
              symbol's page. They used to render wherever `summary` had loaded, which
              put a ticker and a capture time above the journal and the backtester --
              two pages that have nothing to do with the sidebar selection, where it
              read as a stale header nobody could dismiss. */}
          {onTicker && (quotes[symbol] || summary.data) && (
            <span className="spot">
              {num(quotes[symbol]?.last ?? summary.data?.spot)}
            </span>
          )}
          {/* Two different ages, so two different badges. The price above is a quote;
              the capture below is the option chain, which really is from yesterday and
              should keep saying so. Sharing one badge is what made a live price look
              24 hours old. */}
          {onTicker && quotes[symbol]?.as_of && (
            <QuoteAge quote={quotes[symbol]} />
          )}
          {onTicker && summary.data && (
            <Provenance provenance={summary.data.provenance} label="chain captured" />
          )}
          {/* Every panel refreshes on its own clock, and those clocks are chosen to be
              kind to a shared request budget. This is for the moment that is exactly
              wrong for: something moved and the screen is a cycle behind. */}
          <RefreshButton
            onDone={() => {
              watchlist.reload();
              setRefreshNonce((value) => value + 1);
            }}
          />
          <ConnectionBadge
            status={live.status || liveStatus.data}
            connection={wantsLive ? live.connection : CONNECTION.CLOSED}
            cycle={live.cycle}
          />
        </div>

        {/* The per-symbol tabs. Rendered under the header rather than in the sidebar
            so they read as belonging to the ticker above them, and only when a symbol
            page is open: a strip of ticker tabs on the Backtest page would be six
            controls that change nothing visible. */}
        {onTicker && symbol && (
          <div className="ticker-tabs" role="tablist" aria-label={`${symbol} views`}>
            {TICKER_TABS.map((tab) => (
              <button
                key={tab.key}
                type="button"
                role="tab"
                aria-selected={view === tab.key}
                className={`ticker-tab ${view === tab.key ? "active" : ""}`}
                onClick={() => setView(tab.key)}
              >
                {tab.label}
              </button>
            ))}
          </div>
        )}

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
              quotes={quotes}
              onSelect={(name) => {
                setSymbol(name);
                setView("chart");
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
                setView("chart");
              }}
            />
          )}

          {/* Scoped to the symbol whose page this is. Unscoped it ranks the whole
              watchlist, which is what Trade ideas is for. */}
          {!apiDown && view === "best" && symbol && (
            <BestPlays onOpenPayoff={openPayoff} symbols={[symbol]} />
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
          {view === "chart" && symbol && (
            <Underlying symbol={symbol} summary={summary.data} refresh={refreshNonce} />
          )}

          {!apiDown && view === "news" && symbol && <News symbol={symbol} />}

          {view === "info" && symbol && (
            <Information symbol={symbol} summary={summary.data} />
          )}

          {/* Levels and signals were separate views answering halves of one question:
              where price has been defended, and what is true about it right now. */}
          {!apiDown && view === "levels" && symbol && (
            <KeyLevels symbol={symbol} summary={summary.data} />
          )}

          {/* Not gated on a symbol: the list is the answer to "which symbol should I be
              looking at", so gating it on one would invert the point. */}
          {!apiDown && view === "ideas" && (
            <Ideas
              onSelect={(name) => {
                setSymbol(name);
                setView("chart");
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
