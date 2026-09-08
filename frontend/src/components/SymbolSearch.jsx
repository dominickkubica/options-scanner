import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api.js";
import { pct } from "../format.js";

// Ticker search, with the one distinction this whole screen exists to make visible.
//
// There are now a few hundred symbols with a decade of daily bars each and six with
// captured option chains. Those are very different states and a result list that
// showed only "found" would invite somebody to search a ticker, click it, and get an
// empty screener with no explanation.
//
// So every row carries its status, and the pin button says what pinning will actually
// do rather than implying the data appears now.

const DEBOUNCE_MS = 180;

// Long enough that typing a three letter ticker does not fire three searches, short
// enough that the list feels attached to the keyboard.

export default function SymbolSearch({ onSelect, onChanged, groups = {}, autoFocus = false }) {
  const [text, setText] = useState("");
  const [group, setGroup] = useState(null);
  const [results, setResults] = useState([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [open, setOpen] = useState(false);
  const [pending, setPending] = useState(null);
  const boxRef = useRef(null);

  const groupNames = useMemo(() => Object.keys(groups).sort(), [groups]);

  useEffect(() => {
    if (!text.trim() && !group) {
      setResults([]);
      return undefined;
    }
    let cancelled = false;
    setBusy(true);
    const timer = setTimeout(() => {
      api
        .catalogue({ q: text, group, limit: 25 })
        .then((data) => {
          if (cancelled) return;
          setResults(data.entries || []);
          setError(null);
        })
        .catch((err) => !cancelled && setError(err.message))
        .finally(() => !cancelled && setBusy(false));
    }, DEBOUNCE_MS);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [text, group]);

  // Close on an outside click. A dropdown that stays open behind the rest of the page
  // is the most annoying thing a search box can do.
  useEffect(() => {
    function onDocClick(event) {
      if (boxRef.current && !boxRef.current.contains(event.target)) setOpen(false);
    }
    document.addEventListener("mousedown", onDocClick);
    return () => document.removeEventListener("mousedown", onDocClick);
  }, []);

  async function togglePin(entry, event) {
    event.stopPropagation();
    setPending(entry.symbol);
    try {
      const result = entry.on_watchlist
        ? await api.unpin(entry.symbol)
        : await api.pin(entry.symbol);
      setResults((rows) =>
        rows.map((row) =>
          row.symbol === entry.symbol ? { ...row, on_watchlist: result.on_watchlist } : row,
        ),
      );
      onChanged?.(result);
    } catch (err) {
      setError(err.message);
    } finally {
      setPending(null);
    }
  }

  const showing = open && (results.length > 0 || busy || error);

  return (
    <div className="symbol-search" ref={boxRef}>
      <input
        type="search"
        className="search-input"
        placeholder="Search tickers"
        value={text}
        autoFocus={autoFocus}
        onChange={(event) => {
          setText(event.target.value);
          setOpen(true);
        }}
        onFocus={() => setOpen(true)}
        aria-label="Search tickers"
      />

      {groupNames.length > 0 && (
        <div className="search-groups">
          <button
            type="button"
            className={`chip ${group === null ? "active" : ""}`}
            onClick={() => {
              setGroup(null);
              setOpen(true);
            }}
          >
            all
          </button>
          {groupNames.map((name) => (
            <button
              key={name}
              type="button"
              className={`chip ${group === name ? "active" : ""}`}
              onClick={() => {
                setGroup(group === name ? null : name);
                setOpen(true);
              }}
            >
              {name.replace(/_/g, " ")}
              <span className="chip-count">{groups[name]}</span>
            </button>
          ))}
        </div>
      )}

      {showing && (
        <div className="search-results">
          {error && <div className="empty-state">{error}</div>}
          {!error && busy && results.length === 0 && <div className="empty-state">searching</div>}
          {!error && !busy && results.length === 0 && (
            <div className="empty-state">Nothing matches.</div>
          )}
          {results.map((entry) => (
            <button
              key={entry.symbol}
              type="button"
              className="search-row"
              onClick={() => {
                onSelect?.(entry);
                setOpen(false);
              }}
            >
              <span className="search-ticker">{entry.symbol}</span>
              <span className="search-meta">
                {/* Status, not a checkmark. "has prices, no chains" is the common case
                    now and the one that explains an empty screener. */}
                <span className={entry.screenable ? "good" : "provenance"}>{entry.status}</span>
                {entry.groups.length > 0 && (
                  <span className="search-tags">{entry.groups.join(" · ").replace(/_/g, " ")}</span>
                )}
              </span>
              {entry.change_pct !== null && entry.change_pct !== undefined && (
                <span className={entry.change_pct >= 0 ? "up" : "down"}>
                  {pct(entry.change_pct, 1)}
                </span>
              )}
              <span
                className={`pin ${entry.on_watchlist ? "pinned" : ""}`}
                role="button"
                tabIndex={0}
                title={
                  entry.on_watchlist
                    ? "On the watchlist. Click to stop capturing it."
                    : "Pin: the next snapshot run starts capturing its chains."
                }
                onClick={(event) => togglePin(entry, event)}
                onKeyDown={(event) => event.key === "Enter" && togglePin(entry, event)}
              >
                {pending === entry.symbol ? "…" : entry.on_watchlist ? "★" : "☆"}
              </span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
