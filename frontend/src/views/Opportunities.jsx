import { useMemo, useState } from "react";
import { api } from "../api.js";
import { Cell, ErrorBox, Notes, Panel, useAsync } from "../components/common.jsx";
import { count, money, num, pct, reasonLabel, strategyLabel } from "../format.js";

// The ranked table.
//
// Two things on this screen are not decoration. The rejection tally under the table
// is what turns "nothing passed" from a shrug into a diagnosis, and the expanded score
// breakdown is what stops the composite score from being a number people either
// distrust or obey. Both come straight off the API and neither is optional.

const COLUMNS = [
  { key: "symbol", label: "symbol", align: "left" },
  { key: "expiry", label: "expiry", align: "left" },
  { key: "strategy", label: "strategy", align: "left" },
  { key: "legs", label: "legs", align: "left", sortable: false },
  { key: "dte", label: "dte" },
  { key: "credit", label: "credit" },
  { key: "max_profit", label: "profit" },
  { key: "capital", label: "capital" },
  { key: "annualized_return", label: "ann" },
  { key: "probability_of_profit", label: "pop" },
  { key: "short_delta", label: "delta" },
  { key: "liquidity_score", label: "liq" },
  { key: "score", label: "score" },
];

function legText(row) {
  return row.legs
    .map((leg) => `${leg.action === "sell" ? "-" : "+"}${leg.strike}${leg.right}`)
    .join(" / ");
}

function compare(a, b, key) {
  const left = key === "legs" ? legText(a) : a[key];
  const right = key === "legs" ? legText(b) : b[key];
  // Nulls sort last in both directions. A missing return is not a low return, and
  // letting it float to the top of an ascending sort would present it as one.
  if (left === null || left === undefined) return 1;
  if (right === null || right === undefined) return -1;
  if (typeof left === "number") return left - right;
  return String(left).localeCompare(String(right));
}

export default function Opportunities({ symbols, onOpenPayoff }) {
  const { data, error, loading } = useAsync(() => api.scan(symbols, 200), [symbols.join(",")]);
  const [sort, setSort] = useState({ key: "score", desc: true });
  const [expanded, setExpanded] = useState(null);
  const [minScore, setMinScore] = useState(0);
  const [strategy, setStrategy] = useState("");

  const rows = useMemo(() => {
    if (!data) return [];
    const filtered = data.opportunities.filter(
      (row) => row.score >= minScore && (!strategy || row.strategy === strategy),
    );
    const sorted = [...filtered].sort((a, b) => compare(a, b, sort.key));
    return sort.desc ? sorted.reverse() : sorted;
  }, [data, sort, minScore, strategy]);

  const strategies = useMemo(
    () => (data ? [...new Set(data.opportunities.map((row) => row.strategy))].sort() : []),
    [data],
  );

  if (loading) return <div className="loading">Scanning the stored snapshots...</div>;
  if (error) return <ErrorBox error={error} />;
  if (!data) return null;

  return (
    <>
      <Notes items={data.notes} />
      {!data.events_checked && (
        <div className="note">
          The earnings exclusion did not run, so this list can contain positions that
          straddle a report. The same screen run from the CLI would have dropped them.
        </div>
      )}

      <Panel
        title="Candidates"
        right={
          <span className="provenance">
            {count(data.considered)} considered, {count(data.passed)} passed the filters
          </span>
        }
      >
        <div className="controls">
          <label>
            min score{" "}
            <input
              type="number"
              step="0.05"
              min="0"
              max="1"
              value={minScore}
              onChange={(event) => setMinScore(Number(event.target.value))}
              style={{ width: 70 }}
            />
          </label>
          <label>
            strategy{" "}
            <select value={strategy} onChange={(event) => setStrategy(event.target.value)}>
              <option value="">all</option>
              {strategies.map((name) => (
                <option key={name} value={name}>
                  {strategyLabel(name)}
                </option>
              ))}
            </select>
          </label>
          <span className="provenance">
            showing {rows.length} of {data.opportunities.length}
          </span>
        </div>

        {rows.length === 0 ? (
          <div className="empty-state">
            No candidate passed the screen. The tally below says what happened to
            everything that did not, which is usually a threshold in screen.yaml rather
            than an empty market.
          </div>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  {COLUMNS.map((column) => (
                    <th
                      key={column.key}
                      className={`${column.align === "left" ? "left" : ""} ${
                        column.sortable === false ? "" : "sortable"
                      }`}
                      onClick={() =>
                        column.sortable !== false &&
                        setSort((prev) =>
                          prev.key === column.key
                            ? { key: column.key, desc: !prev.desc }
                            : { key: column.key, desc: true },
                        )
                      }
                    >
                      {column.label}
                      {sort.key === column.key ? (sort.desc ? " v" : " ^") : ""}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <RowGroup
                    key={row.id}
                    row={row}
                    expanded={expanded === row.id}
                    onToggle={() => setExpanded(expanded === row.id ? null : row.id)}
                    onOpenPayoff={onOpenPayoff}
                  />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>

      <Panel title="Why candidates were rejected">
        {data.rejections.length === 0 ? (
          <div className="empty-state">Nothing was rejected.</div>
        ) : (
          <div className="chip-row">
            {data.rejections.map((item) => (
              <span key={item.reason} className="chip">
                {reasonLabel(item.reason)} {count(item.count)}
              </span>
            ))}
          </div>
        )}
        <div className="provenance">{data.disclaimer}</div>
      </Panel>
    </>
  );
}

function RowGroup({ row, expanded, onToggle, onOpenPayoff }) {
  return (
    <>
      <tr className="expandable" onClick={onToggle}>
        <td className="left">{row.symbol}</td>
        <td className="left">{row.expiry}</td>
        <td className="left">{strategyLabel(row.strategy)}</td>
        <td className="left">{legText(row)}</td>
        <td>{row.dte}</td>
        <td>{num(row.credit)}</td>
        <Cell value={row.max_profit} formatted={money(row.max_profit)} />
        <Cell value={row.capital} formatted={money(row.capital)} />
        <Cell value={row.annualized_return} formatted={pct(row.annualized_return, 0)} />
        <Cell value={row.probability_of_profit} formatted={pct(row.probability_of_profit, 0)} />
        <Cell value={row.short_delta} formatted={num(row.short_delta)} />
        <Cell value={row.liquidity_score} formatted={num(row.liquidity_score)} />
        <td>{num(row.score, 3)}</td>
      </tr>
      {expanded && (
        <tr className="detail-row">
          <td colSpan={COLUMNS.length}>
            <div className="chip-row">
              {Object.entries(row.components).map(([name, value]) => (
                <span key={name} className="chip">
                  {name.replace(/_/g, " ")} {value === null ? "not scored" : num(value)}
                </span>
              ))}
            </div>
            <div className="chip-row">
              <span className="chip">max loss {money(row.max_loss)}</span>
              <span className="chip">commission {money(row.commission, 2)}</span>
              <span className="chip">return on capital {pct(row.return_on_capital, 1)}</span>
              <span className="chip">touch {pct(row.probability_of_touch, 0)}</span>
              <span className="chip">
                iv {row.iv === null ? "n/a" : `${(row.iv * 100).toFixed(1)}`} ({" "}
                {row.iv_confidence || "no history"} )
              </span>
            </div>
            {row.warnings.length > 0 && (
              <ul style={{ margin: "6px 0 8px", paddingLeft: 18 }}>
                {row.warnings.map((warning, index) => (
                  <li key={index} style={{ color: "var(--warn)" }}>
                    {warning}
                  </li>
                ))}
              </ul>
            )}
            {(row.has_earnings || row.early_assignment_risk) && (
              <div className="chip-row">
                {row.has_earnings && <span className="chip">earnings before expiry</span>}
                {row.early_assignment_risk && (
                  <span className="chip">early assignment risk around ex dividend</span>
                )}
              </div>
            )}
            <button
              type="button"
              className="btn primary"
              onClick={(event) => {
                event.stopPropagation();
                onOpenPayoff(row);
              }}
            >
              Show payoff
            </button>
            <span className="provenance"> id {row.id}</span>
          </td>
        </tr>
      )}
    </>
  );
}
