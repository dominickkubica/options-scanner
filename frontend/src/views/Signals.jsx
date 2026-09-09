import { useState } from "react";
import { api } from "../api.js";
import { ErrorBox, Note, Notes, Panel, useAsync } from "../components/common.jsx";
import { num } from "../format.js";

// Market signals: what is holding right now, and what has already been delivered.
//
// ## Why this panel has two tabs rather than one list
//
// They are different claims and merging them would make the panel lie one way or the
// other. **Holding now** is evaluated on request from stored bars: it is the current
// state of the market and says nothing about whether anyone was told. **Delivered** is
// the record of what the scheduled scan actually sent. A single list would either
// present un-notified conditions as alerts, or hide conditions that hold because no
// scan has run since they started holding.
//
// ## Why severity is shown as a number and a colour, not a word
//
// The severities are an ordering for a digest, and they are not a claim about how
// likely anything is to be profitable, which is unknown and unmeasured. "Critical"
// would say something this tool has never validated. A 4 next to a 2 says only that
// one is rarer than the other, which is exactly what was measured.

const TABS = [
  { key: "now", label: "Holding now" },
  { key: "recent", label: "Delivered" },
];

//: Mirrors `analytics.signals.SEVERITY`. Kept here rather than fetched because it is
//: presentation: the server already sends the number, this only decides the colour.
const TONE = { 4: "sev-high", 3: "sev-mid", 2: "sev-low", 1: "sev-quiet" };

//: At or above this a signal is delivered by the scheduled scan. Below it a signal
//: waits here instead of chasing anyone, which the panel says rather than assumes.
const DELIVERED_AT = 3;

const KIND_LABEL = {
  level_approach: "approaching a level",
  level_break: "level break",
  squeeze: "squeeze",
  oversold_at_support: "oversold at support",
  overbought_at_resistance: "overbought at resistance",
  volume_surge: "volume surge",
};

function SignalRow({ signal, onSelect }) {
  return (
    <tr>
      <td>
        <span className={`sev ${TONE[signal.severity] || "sev-quiet"}`}>
          {signal.severity}
        </span>
      </td>
      <td className="left">
        <button type="button" className="link-btn" onClick={() => onSelect(signal.symbol)}>
          {signal.symbol}
        </button>
      </td>
      <td className="kind-cell left">{KIND_LABEL[signal.kind] || signal.kind}</td>
      <td className="signal-message left">{signal.message}</td>
      <td className="num">{signal.price === null ? "-" : num(signal.price)}</td>
      <td className="num muted">{signal.session}</td>
    </tr>
  );
}

function SignalTable({ signals, onSelect, empty }) {
  if (!signals.length) return <div className="empty-state">{empty}</div>;
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th title="Rarity, not confidence. See the note below.">sev</th>
            <th className="left">symbol</th>
            <th className="left">what</th>
            <th className="left">detail</th>
            <th className="num">price</th>
            <th className="num">session</th>
          </tr>
        </thead>
        <tbody>
          {signals.map((signal) => (
            <SignalRow
              key={`${signal.symbol}:${signal.kind}:${signal.session}`}
              signal={signal}
              onSelect={onSelect}
            />
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function Signals({ onSelect, symbol = null }) {
  const [tab, setTab] = useState("now");
  const [days, setDays] = useState(14);

  // `symbol` scopes both halves to one ticker, which is how this renders inside the
  // per-symbol Key levels tab. Unscoped it spans the whole watchlist, which is the
  // question Trade ideas asks instead.
  const now = useAsync(() => api.signals(symbol), [symbol], { enabled: tab === "now" });
  const recent = useAsync(() => api.recentSignals({ days, symbol }), [days, symbol], {
    enabled: tab === "recent",
  });

  const active = tab === "now" ? now : recent;
  const signals = active.data?.signals || [];
  const quiet = signals.filter((signal) => signal.severity < DELIVERED_AT).length;

  return (
    <>
      <Panel
        title="Signals"
        right={
          <div className="seg">
            {TABS.map((item) => (
              <button
                key={item.key}
                type="button"
                className={`seg-btn ${tab === item.key ? "active" : ""}`}
                onClick={() => setTab(item.key)}
              >
                {item.label}
              </button>
            ))}
          </div>
        }
      >
        <ErrorBox error={active.error} onRetry={active.reload} />

        {tab === "recent" && (
          <div className="controls">
            <label>
              days
              <select value={days} onChange={(event) => setDays(Number(event.target.value))}>
                {[7, 14, 30, 90].map((value) => (
                  <option key={value} value={value}>
                    {value}
                  </option>
                ))}
              </select>
            </label>
          </div>
        )}

        {active.loading && <div className="loading">Evaluating...</div>}

        {!active.loading && (
          <SignalTable
            signals={signals}
            onSelect={onSelect}
            empty={
              tab === "now"
                ? "Nothing is holding on any pinned symbol right now. That is the normal state."
                : `Nothing has been delivered in the last ${days} days.`
            }
          />
        )}

        <Notes items={active.data?.notes || []} />

        {tab === "now" && active.data && (
          <div className="chart-note">
            Evaluated live from stored daily bars across {active.data.scanned} pinned
            symbols. Nothing here has been delivered or recorded: opening this panel
            cannot consume the once-per-session suppression the scheduled scan relies
            on. The scan covers the whole universe; this covers what you pinned.
            {quiet > 0 && (
              <>
                {" "}
                {quiet} of these sit below severity {DELIVERED_AT} and would not have
                chased you. They are context rather than events.
              </>
            )}
          </div>
        )}

        {tab === "recent" && (
          <div className="chart-note">
            What the scheduled scan actually sent, once per symbol, kind and session. A
            break in March and another in July are two rows, not a repeat.
          </div>
        )}
      </Panel>

      <Panel title="What these mean">
        <Note>
          Severity orders a digest by <strong>how rare a condition is</strong>, measured
          across 44,113 symbol-days of stored history. It is not a confidence, a
          probability, or a view on direction, and none of these signals has been
          validated against outcomes.
        </Note>
        <div className="table-wrap">
          <table className="signal-legend">
            <thead>
              <tr>
                <th className="left">signal</th>
                <th>sev</th>
                <th>fires on</th>
                <th className="left">what it says</th>
              </tr>
            </thead>
            <tbody>
              <tr>
                <td className="left">oversold / overbought at a level</td>
                <td>
                  <span className="sev sev-high">4</span>
                </td>
                <td className="num">0.01-0.06%</td>
                <td>
                  RSI at an extreme with price at a swing level that beat chance. Two
                  different measurements agreeing, which is what makes it rare.
                </td>
              </tr>
              <tr>
                <td className="left">level break</td>
                <td>
                  <span className="sev sev-mid">3</span>
                </td>
                <td className="num">1.7%</td>
                <td>
                  A <em>close</em> through a tested level. A wick through and back does
                  not count.
                </td>
              </tr>
              <tr>
                <td className="left">squeeze</td>
                <td>
                  <span className="sev sev-low">2</span>
                </td>
                <td className="num">1.8%</td>
                <td>
                  The day the Bollinger bands close inside the Keltner channels, not
                  every day they stay there. No direction implied.
                </td>
              </tr>
              <tr>
                <td className="left">volume surge</td>
                <td>
                  <span className="sev sev-low">2</span>
                </td>
                <td className="num">4.0%</td>
                <td>Volume at twice its own twenty day average.</td>
              </tr>
              <tr>
                <td className="left">approaching a level</td>
                <td>
                  <span className="sev sev-quiet">1</span>
                </td>
                <td className="num">0.7%</td>
                <td>
                  Within 0.25% of a tested level, excluding one it has just broken
                  through.
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </Panel>
    </>
  );
}
