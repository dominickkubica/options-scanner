import { api } from "../api.js";
import { ErrorBox, Note, Panel, useAsync } from "../components/common.jsx";
import SymbolSearch from "../components/SymbolSearch.jsx";
import { money, pct } from "../format.js";

// The landing page.
//
// It answers three questions in order, and the order is the point:
//
//   1. What is pinned, and what is each of those actually worth right now.
//   2. What else is held, and how to reach it.
//   3. What is wrong or waiting.
//
// The number it must not let anyone misread is coverage. There are a few hundred
// symbols with a decade of daily bars and a handful with captured option chains, and
// only the second can be screened. "293 symbols" on its own would be a lie of
// emphasis, so the tile says both and the smaller number is the one in the accent.

export default function Home({ onSelect, onGoto }) {
  const home = useAsync(() => api.home(), []);

  if (home.error) return <ErrorBox error={home.error} onRetry={home.reload} />;
  if (!home.data) return <div className="empty-state">loading</div>;

  const data = home.data;
  const pinned = data.watchlist || [];

  return (
    <div className="home">
      <Panel
        title="Find a ticker"
        right={
          <span className="provenance">
            {data.total_symbols} known · {data.with_prices} with prices ·{" "}
            <strong className="accent">{data.screenable} screenable</strong>
          </span>
        }
      >
        <SymbolSearch
          groups={data.groups}
          autoFocus
          onSelect={(entry) => onSelect?.(entry.symbol)}
          onChanged={() => home.reload()}
        />
        <p className="provenance home-hint">
          Price history is not enough to screen a symbol: a chain has to have been
          captured. Pinning one with the star makes the next snapshot run start
          capturing it.
        </p>
      </Panel>

      <Panel
        title="Pinned"
        right={
          <span className="provenance">
            {pinned.length} on the watchlist · these are what the screener scans
          </span>
        }
      >
        {pinned.length === 0 ? (
          <div className="empty-state">
            Nothing is pinned, so the screener has nothing to scan. Search above and
            star a ticker.
          </div>
        ) : (
          <div className="pinned-grid">
            {pinned.map((entry) => (
              <button
                key={entry.symbol}
                type="button"
                className={`pinned-card ${entry.screenable ? "" : "waiting"}`}
                onClick={() => onSelect?.(entry.symbol)}
              >
                <div className="pinned-head">
                  <span className="pinned-ticker">{entry.symbol}</span>
                  {entry.change_pct !== null && entry.change_pct !== undefined && (
                    <span className={entry.change_pct >= 0 ? "up" : "down"}>
                      {pct(entry.change_pct, 2)}
                    </span>
                  )}
                </div>
                <div className="pinned-price">{money(entry.last_close, 2)}</div>
                <div className="provenance">
                  {/* Dated, always. This is the last stored close, not a live price,
                      and the two are indistinguishable once the date is dropped. */}
                  close {entry.price_last || "n/a"}
                </div>
                <div className="pinned-tags">
                  {entry.has_iv_history && <span className="tag">IV history</span>}
                  {entry.screenable ? (
                    <span className="tag good">chains {entry.last_capture}</span>
                  ) : (
                    <span className="tag warn">no chains yet</span>
                  )}
                </div>
              </button>
            ))}
          </div>
        )}
      </Panel>

      <Panel
        title="Groups"
        right={<span className="provenance">curated by hand, not index membership</span>}
      >
        <div className="group-grid">
          {Object.entries(data.groups)
            .sort(([a], [b]) => a.localeCompare(b))
            .map(([name, count]) => (
              <button
                key={name}
                type="button"
                className="group-tile"
                onClick={() => onGoto?.("browse", { group: name })}
              >
                <span className="group-name">{name.replace(/_/g, " ")}</span>
                <span className="group-count">{count}</span>
              </button>
            ))}
        </div>
        {data.universe_note && <Note>{data.universe_note}</Note>}
      </Panel>

      {data.notes?.length > 0 && (
        <Panel title="Needs attention">
          {data.notes.map((note) => (
            <Note key={note}>{note}</Note>
          ))}
        </Panel>
      )}
    </div>
  );
}
