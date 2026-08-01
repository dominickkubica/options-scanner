import { useState } from "react";
import { api } from "../api.js";
import LevelsChart from "../components/LevelsChart.jsx";
import { ErrorBox, Note, Notes, Panel, Provenance, Stat, useAsync } from "../components/common.jsx";
import { num, pct, vol } from "../format.js";

// The context layer for choosing a strike: where price has been, where the options
// market thinks it might go, and which strikes the screener surfaced, together.
//
// The panel header carries two provenances rather than one, and that is not clutter.
// This is the only screen in the tool that draws a number fetched seconds ago on the
// same axes as one from the last stored capture, and a single "as of" over the top of
// both would be wrong about one of them.

const WINDOWS = [90, 180, 365, 730];

export default function Levels({ summary }) {
  const [days, setDays] = useState(365);
  // Null lets the server pick the first screenable expiry, the same rule the chain
  // uses. The browser has no business knowing a screen threshold.
  const [expiry, setExpiry] = useState(null);

  const levels = useAsync(
    () => api.levels(summary.symbol, days, expiry),
    [summary.symbol, days, expiry],
  );

  const data = levels.data;
  const swing = (data?.levels || []).filter((item) => item.kind.startsWith("swing"));

  return (
    <>
      <Panel
        title="Price, levels, and the expected move"
        right={
          <span className="provenance">
            <Provenance provenance={data?.bars_provenance} label="candles fetched" />
            {" | "}
            <Provenance provenance={data?.chain_provenance} label="chain captured" />
          </span>
        }
      >
        <div className="controls">
          <label>
            history{" "}
            <select value={days} onChange={(event) => setDays(Number(event.target.value))}>
              {WINDOWS.map((value) => (
                <option key={value} value={value}>
                  {value} days
                </option>
              ))}
            </select>
          </label>
          <label>
            project to{" "}
            <select
              value={expiry || data?.distribution?.expiry || ""}
              onChange={(event) => setExpiry(event.target.value || null)}
            >
              {summary.expiries.map((item) => (
                <option key={item.expiry} value={item.expiry}>
                  {item.expiry} ({item.dte}d)
                </option>
              ))}
            </select>
          </label>
        </div>

        {levels.loading && <div className="loading">Building levels...</div>}
        <ErrorBox error={levels.error} />
        {data && <LevelsChart data={data} />}
      </Panel>

      {data && (
        <div className="grid-2">
          <Panel title="What the window measured">
            <div className="stats">
              <Stat label="sessions" value={data.sessions} />
              <Stat
                label="atr"
                value={num(data.atr)}
                hint={data.atr ? `${pct(data.atr / data.spot, 2)} of spot` : undefined}
              />
              <Stat label="realized vol" value={vol(data.realized_vol)} hint="30 day, annualized" />
              <Stat
                label="implied vol"
                value={vol(data.implied_vol)}
                hint={data.implied_vol === null ? "no expiry near the window" : "nearest tenor"}
              />
              <Stat
                label="variance risk premium"
                value={vol(data.variance_risk_premium)}
                hint="implied minus realized, vol points"
              />
            </div>

            {data.implied_vol === null && (
              <Note>
                No captured expiry sits near the realized volatility window, so the
                premium is not published. Comparing a 30 day realized vol against a 4 day
                implied would be the term structure talking, not the premium.
              </Note>
            )}
          </Panel>

          <Panel title="Levels that beat chance">
            <div className="stats">
              <Stat label="published" value={swing.length} />
              <Stat label="tested" value={data.swing_candidates} />
            </div>
            {swing.length > 0 ? (
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th className="left">price</th>
                      <th>kind</th>
                      <th>touches</th>
                      <th>expected</th>
                      <th>p</th>
                      <th>last</th>
                    </tr>
                  </thead>
                  <tbody>
                    {swing.map((level) => (
                      <tr key={`${level.kind}-${level.price}`}>
                        <td className="left">{num(level.price)}</td>
                        <td>{level.kind.replace("_", " ")}</td>
                        <td>{level.touches}</td>
                        <td>{num(level.expected_touches, 1)}</td>
                        <td>{num(level.p_value, 3)}</td>
                        <td>{level.last_touch}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <Note>
                Nothing here turned out to be a level. Every candidate was a price where
                the number of times price turned is what the time it spent there already
                predicts.
              </Note>
            )}
            <div className="chart-note">
              A touch count on its own measures how densely pivots fall, not whether a
              price mattered. Each candidate is compared against how many touches its own
              band would have collected by chance.
            </div>
          </Panel>
        </div>
      )}

      {data?.distribution && (
        <Panel title={`Where price could finish on ${data.distribution.expiry}`}>
          <div className="stats">
            <Stat label="paths" value={data.distribution.paths.toLocaleString()} />
            <Stat label="median" value={num(data.distribution.median)} />
            <Stat label="most likely" value={num(data.distribution.mode)} />
            {Object.entries(data.distribution.quantiles).map(([q, value]) => (
              <Stat key={q} label={`p${Math.round(Number(q) * 100)}`} value={num(value)} />
            ))}
          </div>
          <Histogram distribution={data.distribution} spot={data.spot} />
          <div className="chart-note">
            Simulated under the same lognormal model the probabilities use, at the
            expiry&apos;s own implied volatility and the configured rate. The peak sits
            below the mean because the distribution is right skewed, which is a property
            of the model rather than a view on direction.
          </div>
        </Panel>
      )}

      {data && <Notes items={data.notes} />}
    </>
  );
}

// A plain probability histogram. Horizontal, so it shares a price axis orientation
// with nothing else on the page and cannot be misread as a price chart.
function Histogram({ distribution, spot }) {
  const peak = Math.max(...distribution.bins.map((bin) => bin.probability), 0);
  if (peak <= 0) return null;

  return (
    <svg viewBox="0 0 900 160" style={{ width: "100%", height: "auto" }}>
      {distribution.bins.map((bin, index) => {
        const width = 900 / distribution.bins.length;
        const height = (bin.probability / peak) * 130;
        const straddles = spot >= bin.low && spot < bin.high;
        return (
          <rect
            key={index}
            x={index * width}
            y={140 - height}
            width={Math.max(width - 1, 1)}
            height={height}
            fill={straddles ? "var(--accent, #4f7fbf)" : "var(--line)"}
            opacity={straddles ? 0.9 : 0.7}
          />
        );
      })}
      <text x="2" y="155" fontSize="9">
        {num(distribution.bins[0].low)}
      </text>
      <text x="860" y="155" fontSize="9">
        {num(distribution.bins[distribution.bins.length - 1].high)}
      </text>
    </svg>
  );
}
