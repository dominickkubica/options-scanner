import { useState } from "react";
import { api } from "../api.js";
import PriceChart from "../components/chart/PriceChart.jsx";
import { IvRankGauge, SkewCurve, TermStructure } from "../components/charts.jsx";
import { ErrorBox, Note, Panel, Provenance, Stat, useAsync } from "../components/common.jsx";
import { count, num, pct, vol } from "../format.js";

// The volatility view of one underlying: where price has been, where implied vol sits
// against its own history, how vol varies across expiries, and how it varies across
// strikes inside one expiry.
//
// The IV rank panel is the one most likely to look broken and is not. Until months of
// daily captures exist there is no rank to publish, so the gauge shows the refusal and
// the reason instead of a bar at zero.

export default function Underlying({ summary }) {
  // Sessions of history, not calendar days: the endpoint returns one bar per unit. 126
  // is six months of trading, and is the chart's default window.
  const [days, setDays] = useState(126);
  // Null lets the server pick the first screenable expiry. The front week's smile is
  // dominated by gamma and is the least useful one to open on.
  const [skewExpiry, setSkewExpiry] = useState(null);

  const history = useAsync(() => api.history(summary.symbol, days), [summary.symbol, days]);
  const chain = useAsync(
    () => api.chain(summary.symbol, skewExpiry),
    [summary.symbol, skewExpiry],
  );

  return (
    <>
      {summary.partial && (
        <Note>
          This capture is partial: at least one expiry failed to fetch. What is here is
          real, and what is missing is missing rather than interpolated.
        </Note>
      )}
      {summary.events_note && <Note>{summary.events_note}</Note>}

      <Panel title="Session">
        <div className="stats">
          <Stat label="spot" value={num(summary.spot)} />
          <Stat label="session" value={summary.session_date} />
          <Stat label="expiries captured" value={summary.expiries.length} />
          <Stat
            label="earnings"
            value={summary.earnings_date || (summary.events_checked ? "none scheduled" : "not checked")}
          />
          <Stat
            label="ex dividend"
            value={summary.ex_dividend_date || (summary.events_checked ? "none scheduled" : "not checked")}
          />
        </div>
      </Panel>

      {/* The window control lives inside the chart now, alongside the interval and the
          indicators, because they are one decision made in one place. The panel title
          therefore names the series rather than the window. */}
      <Panel
        title="Price"
        right={<Provenance provenance={history.data?.provenance} label="fetched" />}
      >
        <ErrorBox error={history.error} />
        {history.data?.note && <Note>{history.data.note}</Note>}
        <PriceChart
          symbol={summary.symbol}
          bars={history.data?.bars}
          loading={history.loading}
          days={days}
          onDaysChange={setDays}
        />
        <div className="chart-note">
          Candles come from the vendor at request time, not from the stored snapshot.
          They are the one thing on this page that is not the last capture. Weekly and
          monthly candles are those same daily bars grouped, not a second series.
        </div>
      </Panel>

      <div className="grid-2">
        <Panel title="Implied volatility rank">
          <IvRankGauge ivRank={summary.iv_rank} note={summary.iv_rank_note} />
        </Panel>

        <Panel title="Term structure">
          <TermStructure
            points={summary.term_structure}
            backwardated={summary.backwardated}
            slope={summary.term_slope}
          />
        </Panel>
      </div>

      <Panel
        title="Skew"
        right={<Provenance provenance={chain.data?.provenance} />}
      >
        <div className="controls">
          <label>
            expiry{" "}
            <select
              value={chain.data?.expiry || ""}
              onChange={(event) => setSkewExpiry(event.target.value)}
            >
              {summary.expiries.map((item) => (
                <option key={item.expiry} value={item.expiry}>
                  {item.expiry} ({item.dte}d)
                </option>
              ))}
            </select>
          </label>
        </div>
        {chain.loading && <div className="loading">Solving...</div>}
        <ErrorBox error={chain.error} />
        {chain.data && (
          <SkewCurve
            putSkew={chain.data.put_skew}
            callSkew={chain.data.call_skew}
            spot={chain.data.spot}
            atmIv={chain.data.atm_iv}
          />
        )}
      </Panel>

      <Panel title="Expiries">
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th className="left">expiry</th>
                <th>dte</th>
                <th>atm iv</th>
                <th>expected move</th>
                <th>straddle</th>
                <th>contracts</th>
                <th>solved</th>
                <th>solve rate</th>
              </tr>
            </thead>
            <tbody>
              {summary.expiries.map((item) => (
                <tr key={item.expiry}>
                  <td className="left">{item.expiry}</td>
                  <td>{item.dte}</td>
                  <td className={item.atm_iv === null ? "missing" : ""}>{vol(item.atm_iv)}</td>
                  <td className={item.expected_move === null ? "missing" : ""}>
                    {num(item.expected_move)}
                  </td>
                  <td className={item.straddle === null ? "missing" : ""}>{num(item.straddle)}</td>
                  <td>{count(item.contracts)}</td>
                  <td>{count(item.solved)}</td>
                  <td>{pct(item.solve_rate, 0)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="chart-note">
          Expected move is one standard deviation in price terms, inverted from the at
          the money straddle where one could be solved and from the model otherwise.
        </div>
      </Panel>
    </>
  );
}
