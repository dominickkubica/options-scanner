import { useEffect, useState } from "react";
import { api } from "../api.js";
import PriceChart, { INDICATOR_WARMUP } from "../components/chart/PriceChart.jsx";
import { IvRankGauge, SkewCurve, TermStructure } from "../components/charts.jsx";
import { ErrorBox, Note, Panel, Provenance, Stat, useAsync } from "../components/common.jsx";
import { count, num, pct, vol } from "../format.js";

//: How often the chart re-asks the server while it is open. Matched to the server's own
//: intraday cache: asking faster returns the identical payload and spends a request to
//: learn nothing.
const CHART_REFRESH_SECONDS = 15;

// The volatility view of one underlying: where price has been, where implied vol sits
// against its own history, how vol varies across expiries, and how it varies across
// strikes inside one expiry.
//
// The IV rank panel is the one most likely to look broken and is not. Until months of
// daily captures exist there is no rank to publish, so the gauge shows the refusal and
// the reason instead of a bar at zero.

// `summary` is optional and usually absent. After a bulk price sync there are a few
// hundred symbols with a decade of daily bars and a handful with captured option
// chains, and the chart needs only the first. Gating the whole view on a snapshot made
// every unpinned ticker a dead page reading "No stored snapshot for AA", while 2,492
// sessions of its bars sat in the database.
//
// So the price panel renders from `symbol` alone and every volatility panel below it
// is conditional. The absence is stated once, at the top, with what to do about it.
export default function Underlying({ symbol, summary }) {
  const ticker = summary?.symbol || symbol;
  // Sessions of history, not calendar days: the endpoint returns one bar per unit. 126
  // is six months of trading, and is the chart's default window.
  const [days, setDays] = useState(126);
  // Null lets the server pick the first screenable expiry. The front week's smile is
  // dominated by gamma and is the least useful one to open on.
  const [skewExpiry, setSkewExpiry] = useState(null);
  // Intraday drill-in. `session` set means the chart is showing one named day
  // rather than a rolling window, which is a different question and gets its own
  // header rather than being folded into the interval control.
  const [interval, setInterval] = useState("1Day");
  const [session, setSession] = useState(null);

  // Extra leading bars so the long indicators have a window to fill. A 200 day moving
  // average on a six month chart is not a contradiction: the average needs 200 sessions
  // of history, not 200 sessions on screen, and refusing it because the *window* is
  // short confuses what is being drawn with what is being looked at. The chart shows the
  // requested window and keeps the warmup off to the left, one scroll away.
  const history = useAsync(
    () => api.history(ticker, { days: days + INDICATOR_WARMUP, interval, session }),
    [ticker, days, interval, session],
    { keepOnError: true },
  );

  // Redraw the chart on a clock, not only when the symbol changes.
  //
  // Everything else on the page moved and the chart did not: the pills repriced every
  // ten seconds while the candles sat exactly as first drawn, which on a two minute
  // chart means the bar you are watching form never forms. The daily chart is included
  // because its last candle is now today's session and moves for the same reason.
  //
  // Not while drilled into a past session: that day is finished and re-fetching it
  // would spend a request to redraw an identical chart.
  const reloadHistory = history.reload;
  useEffect(() => {
    if (!ticker || session) return undefined;
    const timer = window.setInterval(() => {
      // A hidden tab is not being looked at, and its requests come out of the same
      // vendor budget as the capture jobs.
      if (!document.hidden) reloadHistory();
    }, CHART_REFRESH_SECONDS * 1000);
    return () => window.clearInterval(timer);
  }, [ticker, interval, days, session, reloadHistory]);
  const chain = useAsync(() => api.chain(ticker, skewExpiry), [ticker, skewExpiry], {
    enabled: Boolean(summary),
  });

  const openSession = (day, chosen) => {
    setSession(day);
    setInterval(chosen || "5Min");
  };

  const backToDaily = () => {
    setSession(null);
    setInterval("1Day");
  };

  return (
    <>
      {!summary && (
        <Note>
          No option chains have been captured for {ticker}, so the volatility panels
          below are not available. The price history is real and is shown. Pin it on
          Home and the next snapshot run starts capturing chains.
        </Note>
      )}

      {summary?.partial && (
        <Note>
          This capture is partial: at least one expiry failed to fetch. What is here is
          real, and what is missing is missing rather than interpolated.
        </Note>
      )}
      {summary?.events_note && <Note>{summary.events_note}</Note>}

      {summary && (
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
      )}

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
          symbol={ticker}
          bars={history.data?.bars}
          loading={history.loading}
          days={days}
          onDaysChange={setDays}
          interval={interval}
          onIntervalChange={setInterval}
          session={session}
          onOpenSession={openSession}
          onBackToDaily={backToDaily}
        />
        <div className="chart-note">
          {session
            ? "One session, intraday. Bounded to the calendar day rather than to market hours, so the pre and post market bars are included: on a quiet name those are often the whole reason to open a day."
            : "Daily candles are read from stored history, so they work for any synced symbol. Weekly and monthly are those same bars grouped, not a second series. Click a candle to open that day."}
        </div>
      </Panel>

      {summary && (
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
      )}

      {summary && (
        <>
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
      )}
    </>
  );
}
