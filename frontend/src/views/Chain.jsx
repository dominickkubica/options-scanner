import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api.js";
import { Cell, ErrorBox, Panel, Provenance, useAsync } from "../components/common.jsx";
import { count, heat, normalize, num, pct, vol } from "../format.js";

// The full grid, calls on the left, puts on the right, strikes down the middle.
//
// Heatmaps on implied vol and on volume, both normalized across the strikes actually
// visible rather than against an absolute scale, because what a reader wants from a
// chain heatmap is where the activity is relative to the rest of this chain.
//
// A strike that could not be solved keeps its row and shows the solver's own refusal
// on hover. Dropping those rows would leave gaps in the ladder that read as strikes
// which are not listed, and colouring them as zero volatility would be worse.

function byStrike(rows) {
  const map = new Map();
  for (const row of rows) map.set(row.strike, row);
  return map;
}

//: Strike bands the grid can be narrowed to, as a fraction either side of spot. A
//: full SPY ladder runs from 375 to 1000 against a 740 spot, and the deep wings are
//: mostly unquotable placeholders. The default is wide enough to hold every strike
//: anyone would sell and the count of what is hidden is always shown.
const BANDS = [
  { label: "near the money (10%)", value: 0.1 },
  { label: "wide (25%)", value: 0.25 },
  { label: "every listed strike", value: null },
];

export default function Chain({ symbol, expiries, expiry, onExpiry }) {
  const { data, error, loading } = useAsync(() => api.chain(symbol, expiry), [symbol, expiry]);
  const [band, setBand] = useState(0.25);
  const atmRow = useRef(null);

  const allRows = useMemo(() => {
    if (!data) return [];
    const calls = byStrike(data.calls);
    const puts = byStrike(data.puts);
    const strikes = [...new Set([...calls.keys(), ...puts.keys()])].sort((a, b) => a - b);
    return strikes.map((strike) => ({ strike, call: calls.get(strike), put: puts.get(strike) }));
  }, [data]);

  const rows = useMemo(() => {
    if (!data || band === null) return allRows;
    return allRows.filter((row) => Math.abs(row.strike / data.spot - 1) <= band);
  }, [allRows, band, data]);

  const ivScale = useMemo(
    () => normalize(rows.flatMap((row) => [row.call?.iv, row.put?.iv])),
    [rows],
  );
  const volumeScale = useMemo(
    () => normalize(rows.flatMap((row) => [row.call?.volume, row.put?.volume])),
    [rows],
  );

  // Open at the money rather than at the bottom of the ladder. A grid that lands on
  // the 375 strike of a 740 underlying looks empty and wrong.
  useEffect(() => {
    if (atmRow.current) atmRow.current.scrollIntoView({ block: "center" });
  }, [rows]);

  if (loading) return <div className="loading">Solving the chain...</div>;
  if (error) return <ErrorBox error={error} />;
  if (!data) return null;

  const atmStrike = rows.reduce(
    (best, row) =>
      best === null || Math.abs(row.strike - data.spot) < Math.abs(best - data.spot)
        ? row.strike
        : best,
    null,
  );

  const unsolved = rows.filter((row) => row.call?.reject_reason || row.put?.reject_reason).length;
  const solved = rows.reduce(
    (total, row) => total + (row.call?.iv !== null ? 1 : 0) + (row.put?.iv !== null ? 1 : 0),
    0,
  );

  return (
    <Panel
      title={`Chain ${data.expiry} (${data.dte} DTE)`}
      right={<Provenance provenance={data.provenance} />}
    >
      <div className="controls">
        <label>
          expiry{" "}
          <select
            value={data.expiry}
            onChange={(event) => onExpiry(event.target.value)}
          >
            {expiries.map((item) => (
              <option key={item.expiry} value={item.expiry}>
                {item.expiry} ({item.dte}d, {count(item.contracts)} contracts,{" "}
                {pct(item.solve_rate, 0)} solved)
              </option>
            ))}
          </select>
        </label>
        <label>
          strikes{" "}
          <select
            value={band === null ? "all" : String(band)}
            onChange={(event) =>
              setBand(event.target.value === "all" ? null : Number(event.target.value))
            }
          >
            {BANDS.map((item) => (
              <option key={item.label} value={item.value === null ? "all" : String(item.value)}>
                {item.label}
              </option>
            ))}
          </select>
        </label>
        <span className="provenance">
          spot {num(data.spot)}, at the money vol {vol(data.atm_iv)}, showing{" "}
          {count(rows.length)} of {count(allRows.length)} strikes
        </span>
      </div>

      <div className="legend">
        <span>
          <span className="swatch" />
          low to high, within the visible strikes
        </span>
        <span>
          {count(solved)} contracts solved, {count(unsolved)} strikes carry a stated refusal
          instead of a volatility
        </span>
      </div>

      <div className="table-wrap" style={{ maxHeight: "70vh", overflowY: "auto" }}>
        <table className="chain-table">
          <thead>
            <tr>
              <th>iv</th>
              <th>delta</th>
              <th>gamma</th>
              <th>theta</th>
              <th>vega</th>
              <th>vol</th>
              <th>oi</th>
              <th>bid</th>
              <th>ask</th>
              <th style={{ textAlign: "center" }}>strike</th>
              <th>bid</th>
              <th>ask</th>
              <th>oi</th>
              <th>vol</th>
              <th>vega</th>
              <th>theta</th>
              <th>gamma</th>
              <th>delta</th>
              <th>iv</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr
                key={row.strike}
                ref={row.strike === atmStrike ? atmRow : null}
                className={row.strike === atmStrike ? "atm" : ""}
              >
                <Side contract={row.call} ivScale={ivScale} volumeScale={volumeScale} side="call" />
                <td className="strike-col">{num(row.strike, 0)}</td>
                <Side contract={row.put} ivScale={ivScale} volumeScale={volumeScale} side="put" />
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Panel>
  );
}

// Calls read outward from the strike column and puts read outward the other way,
// which is the layout every options screen uses. The two orders are written out in
// full rather than derived by reversing one of them: reversing also flips bid and ask
// past each other, which renders a normal 0.00 / 0.01 put as a crossed market.
const CALL_ORDER = ["iv", "delta", "gamma", "theta", "vega", "volume", "oi", "bid", "ask"];
const PUT_ORDER = ["bid", "ask", "oi", "volume", "vega", "theta", "gamma", "delta", "iv"];

function Side({ contract, ivScale, volumeScale, side }) {
  const order = side === "call" ? CALL_ORDER : PUT_ORDER;

  if (!contract) {
    return (
      <>
        {order.map((key) => (
          <td key={key} className="missing">
            n/a
          </td>
        ))}
      </>
    );
  }

  const cells = {
    iv: (
      <td
        key="iv"
        className={contract.iv === null ? "missing" : ""}
        style={{ background: contract.iv === null ? "transparent" : heat(ivScale(contract.iv)) }}
        title={contract.reject_reason ? `no implied vol: ${contract.reject_reason}` : undefined}
      >
        {contract.iv === null ? contract.reject_reason || "n/a" : vol(contract.iv)}
      </td>
    ),
    volume: (
      <td
        key="volume"
        className={contract.volume === null ? "missing" : ""}
        style={{
          background: contract.volume === null ? "transparent" : heat(volumeScale(contract.volume)),
        }}
      >
        {count(contract.volume)}
      </td>
    ),
    delta: <Cell key="delta" value={contract.delta} formatted={num(contract.delta, 3)} />,
    gamma: <Cell key="gamma" value={contract.gamma} formatted={num(contract.gamma, 4)} />,
    theta: <Cell key="theta" value={contract.theta} formatted={num(contract.theta, 3)} />,
    vega: <Cell key="vega" value={contract.vega} formatted={num(contract.vega, 3)} />,
    oi: (
      <Cell key="oi" value={contract.open_interest} formatted={count(contract.open_interest)} />
    ),
    bid: <Cell key="bid" value={contract.bid} formatted={num(contract.bid)} />,
    ask: <Cell key="ask" value={contract.ask} formatted={num(contract.ask)} />,
  };

  return <>{order.map((key) => cells[key])}</>;
}
