import { useState } from "react";
import { api } from "../api.js";
import OiGexChart from "../components/OiGexChart.jsx";
import { ErrorBox, Note, Notes, Panel, Provenance, Stat, useAsync } from "../components/common.jsx";
import { compact, count, num, pct } from "../format.js";

// Where the open interest sits, and what the dealer hedging implied by it would do.
//
// This panel is the one place in the app where the headline number is a model output
// rather than a measurement, so it is deliberate about which is which. The walls, the
// pin and max pain are counts of contracts and are stated plainly. The gamma regime is
// an inference resting on a convention nobody can verify from public data -- that
// dealers are long calls and short puts -- and it is labelled as a reading rather than
// a fact, with the server's caveats printed underneath rather than folded away.
//
// The other thing this panel has to keep saying is its own age. Open interest is
// settled overnight, so every level here describes the prior close no matter how fresh
// the fetch was, while the spot line they are measured against is seconds old. That is
// a wider gap between two numbers on one chart than anywhere else in the tool.

//: How much of the ladder either side of spot the chart draws. Matches the band the
//: zero-gamma search sweeps, so a level that exists is a level that can be drawn.
const BAND = 0.03;

//: The aggregation windows the toggle offers. Null is the default and the honest one:
//: a single expiry's gamma is a statement about that expiry, and summing a month of
//: them produces a number dominated by the monthly that says nothing about today.
const WINDOWS = [
  { label: "this expiry only", value: null },
  { label: "everything inside 7 days", value: 7 },
  { label: "everything inside 30 days", value: 30 },
];

const REGIMES = {
  dampened: {
    label: "dampened, pin-prone",
    tone: "up",
    blurb:
      "Spot is above the zero-gamma level, so hedging the convention implies leans " +
      "against the move: rallies are sold and dips are bought, which compresses range.",
  },
  volatile: {
    label: "volatile",
    tone: "down",
    blurb:
      "Spot is below the zero-gamma level, so hedging the convention implies follows " +
      "the move: selling into weakness and buying into strength, which extends range.",
  },
  unknown: {
    label: "not known",
    tone: "",
    blurb:
      "No strike in this expiry could be priced, so there is no gamma reading at all. " +
      "The open interest levels beside it are counts and still stand.",
  },
};

export default function Gex({ symbol }) {
  const [expiry, setExpiry] = useState(null);
  const [withinDays, setWithinDays] = useState(null);
  const [view, setView] = useState("oi");

  const gex = useAsync(
    () => api.gex(symbol, { expiry, withinDays }),
    [symbol, expiry, withinDays],
  );
  const data = gex.data;
  const regime = REGIMES[data?.regime] || REGIMES.unknown;

  return (
    <>
      <Panel
        title="Open interest and gamma"
        right={
          <span className="provenance">
            <Provenance provenance={data?.chain_provenance} label="chain fetched" />
            {data?.open_interest_as_of ? ` | open interest settled ${data.open_interest_as_of}` : ""}
          </span>
        }
      >
        <div className="controls">
          <label>
            expiry{" "}
            <select
              value={expiry || data?.expiry || ""}
              onChange={(event) => setExpiry(event.target.value || null)}
              disabled={!data}
            >
              {(data?.expiries || []).map((item) => (
                <option key={item.expiry} value={item.expiry}>
                  {item.expiry} ({item.dte}d)
                </option>
              ))}
            </select>
          </label>
          <label>
            scope{" "}
            <select
              value={withinDays === null ? "" : String(withinDays)}
              onChange={(event) =>
                setWithinDays(event.target.value === "" ? null : Number(event.target.value))
              }
            >
              {WINDOWS.map((item) => (
                <option key={item.label} value={item.value === null ? "" : String(item.value)}>
                  {item.label}
                </option>
              ))}
            </select>
          </label>
          <label>
            show{" "}
            <select value={view} onChange={(event) => setView(event.target.value)}>
              <option value="oi">open interest by strike</option>
              <option value="gex">net gamma by strike</option>
            </select>
          </label>
        </div>

        {gex.loading && <div className="loading">Reading the chain...</div>}
        <ErrorBox error={gex.error} />

        {data && (
          <>
            <div className="stats">
              <Stat
                label="regime"
                value={regime.label}
                hint={data.zero_gamma ? `flip at ${num(data.zero_gamma.price)}` : "no flip in band"}
              />
              <Stat
                label="net gamma"
                value={data.total_gex === null ? "n/a" : signedDollars(data.total_gex)}
                hint="per 1% move, dealer convention"
              />
              <Stat label="spot" value={num(data.spot)} hint={data.spot_source} />
              <Stat
                label="open interest"
                value={count(data.total_oi)}
                hint={
                  data.priced_oi === data.total_oi
                    ? "all of it priced"
                    : `${pct(data.priced_oi / (data.total_oi || 1), 0)} of it priced`
                }
              />
            </div>

            <OiGexChart data={data} view={view} band={BAND} />

            {view === "gex" && (
              <div className="chart-note">
                Calls count positive and puts negative, the convention that assumes dealers
                are long calls and short puts. It is a convention, not a measurement: real
                positioning is not published, and one large overwriting programme can flip
                the true sign of a strike without changing anything visible here.
              </div>
            )}
          </>
        )}
      </Panel>

      {data && (
        <div className="grid-2">
          <Panel title="Levels, and how far price is from them">
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th className="left">level</th>
                    <th>price</th>
                    <th>distance</th>
                    <th>%</th>
                    <th>open interest</th>
                  </tr>
                </thead>
                <tbody>
                  {[
                    ["call wall", data.call_wall],
                    ["put wall", data.put_wall],
                    ["pin candidate", data.pin],
                    ["max pain", data.max_pain],
                    ["zero gamma", data.zero_gamma],
                  ].map(([label, level]) => (
                    <tr key={label}>
                      <td className="left">{label}</td>
                      <td>{level ? num(level.price) : "n/a"}</td>
                      <td>{level ? signed(level.distance) : "n/a"}</td>
                      <td>{level ? signedPct(level.distance_pct) : "n/a"}</td>
                      <td>{level?.open_interest ? compact(level.open_interest) : ""}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="chart-note">
              A level with no price was not found rather than found at zero: no strike above
              spot carries open interest, or the gamma total never changed sign inside the{" "}
              {pct(BAND, 0)} band.
            </div>
          </Panel>

          <Panel title="What this reads as">
            <div className="stats">
              <Stat label="expiry" value={data.expiry} hint={`${data.dte}d`} />
              <Stat
                label="expiries folded in"
                value={data.expiries_used.length}
                hint={data.within_days ? `inside ${data.within_days} days` : "the selected one"}
              />
              <Stat label="strikes" value={data.strikes.length} />
              <Stat label="market" value={data.market_state} />
            </div>
            <Note>{regime.blurb}</Note>
            <div className="chart-note">
              Spot comes from the {data.spot_source}; the open interest behind every level
              was settled overnight. The two are hours apart by construction, which is why
              a wall can sit exactly where price already traded through today.
            </div>
          </Panel>
        </div>
      )}

      {data && <Notes items={data.notes} />}
    </>
  );
}

function signed(value) {
  if (value === null || value === undefined) return "n/a";
  return `${value >= 0 ? "+" : "-"}${num(Math.abs(value))}`;
}

function signedPct(value) {
  if (value === null || value === undefined) return "n/a";
  return `${value >= 0 ? "+" : "-"}${pct(Math.abs(value))}`;
}

function signedDollars(value) {
  if (value === null || value === undefined) return "n/a";
  return `${value < 0 ? "-" : ""}$${compact(Math.abs(value))}`;
}
