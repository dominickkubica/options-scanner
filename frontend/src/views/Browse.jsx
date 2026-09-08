import { useEffect, useState } from "react";
import { api } from "../api.js";
import { ErrorBox, Note, Panel, useAsync } from "../components/common.jsx";
import { money, pct } from "../format.js";

// Every symbol in a group, as a table you can pin from.
//
// A dropdown is the wrong shape once a group has a hundred members: the question here
// is "what is in metals and which of it can I actually screen", which is a comparison
// across rows rather than a lookup of one.
//
// The `screenable` column is the one that matters and it is mostly empty on purpose.
// Hundreds of symbols have a decade of daily bars; six have captured option chains.
// Showing that as a column rather than a footnote is what stops somebody pinning
// twenty tickers and wondering why the screener is unchanged.

const LIMIT = 200;

export default function Browse({ group, onSelectGroup, onSelect }) {
  const [rows, setRows] = useState([]);
  const [pending, setPending] = useState(null);
  const [flash, setFlash] = useState(null);
  const [onlyScreenable, setOnlyScreenable] = useState(false);

  const data = useAsync(
    () => api.catalogue({ group, onlyScreenable, limit: LIMIT }),
    [group, onlyScreenable],
  );

  useEffect(() => {
    if (data.data) setRows(data.data.entries || []);
  }, [data.data]);

  async function togglePin(entry) {
    setPending(entry.symbol);
    try {
      const result = entry.on_watchlist
        ? await api.unpin(entry.symbol)
        : await api.pin(entry.symbol);
      setRows((current) =>
        current.map((row) =>
          row.symbol === entry.symbol ? { ...row, on_watchlist: result.on_watchlist } : row,
        ),
      );
      // The note is the point of the write, not a courtesy: pinning does not capture
      // a chain, and an empty screener afterwards would otherwise read as a bug.
      setFlash(result.note);
    } catch (error) {
      setFlash(error.message);
    } finally {
      setPending(null);
    }
  }

  if (data.error) return <ErrorBox error={data.error} onRetry={data.reload} />;

  const groups = data.data?.groups || {};
  const pinnedHere = rows.filter((row) => row.on_watchlist).length;

  return (
    <div>
      <Panel
        title="Browse"
        right={
          <span className="provenance">
            {rows.length} shown · {pinnedHere} pinned
          </span>
        }
      >
        <div className="search-groups">
          {Object.keys(groups)
            .sort()
            .map((name) => (
              <button
                key={name}
                type="button"
                className={`chip ${group === name ? "active" : ""}`}
                onClick={() => onSelectGroup?.(group === name ? null : name)}
              >
                {name.replace(/_/g, " ")}
                <span className="chip-count">{groups[name]}</span>
              </button>
            ))}
          <button
            type="button"
            className={`chip ${onlyScreenable ? "active" : ""}`}
            onClick={() => setOnlyScreenable((value) => !value)}
            title="Only symbols with a captured option chain, which is the only kind the screener can use."
          >
            screenable only
          </button>
        </div>

        {flash && <Note>{flash}</Note>}

        {!data.data ? (
          <div className="empty-state">loading</div>
        ) : rows.length === 0 ? (
          <div className="empty-state">
            {onlyScreenable
              ? "Nothing in this group has a captured chain yet. Pin a ticker and the next snapshot run will fetch one."
              : "Nothing here."}
          </div>
        ) : (
          <div className="table-scroll">
            <table className="browse-table">
              <thead>
                <tr>
                  <th aria-label="pin" />
                  <th>symbol</th>
                  <th className="num">last close</th>
                  <th className="num">change</th>
                  <th className="num">sessions</th>
                  <th>groups</th>
                  <th>held</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr key={row.symbol} className={row.screenable ? "" : "dim-row"}>
                    <td>
                      <button
                        type="button"
                        className={`pin ${row.on_watchlist ? "pinned" : ""}`}
                        onClick={() => togglePin(row)}
                        title={
                          row.on_watchlist
                            ? "On the watchlist. Click to stop capturing it."
                            : "Pin: the next snapshot run starts capturing its chains."
                        }
                      >
                        {pending === row.symbol ? "…" : row.on_watchlist ? "★" : "☆"}
                      </button>
                    </td>
                    <td>
                      <button
                        type="button"
                        className="link-cell"
                        onClick={() => onSelect?.(row.symbol)}
                      >
                        {row.symbol}
                      </button>
                    </td>
                    <td className="num">{money(row.last_close, 2)}</td>
                    <td
                      className={`num ${
                        row.change_pct === null || row.change_pct === undefined
                          ? ""
                          : row.change_pct >= 0
                            ? "up"
                            : "down"
                      }`}
                    >
                      {row.change_pct === null || row.change_pct === undefined
                        ? "n/a"
                        : pct(row.change_pct, 2)}
                    </td>
                    <td className="num">{row.price_sessions.toLocaleString()}</td>
                    <td className="provenance">{row.groups.join(" · ").replace(/_/g, " ")}</td>
                    <td className={row.screenable ? "good" : "provenance"}>{row.status}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {data.data?.universe_note && <Note>{data.data.universe_note}</Note>}
      </Panel>
    </div>
  );
}
