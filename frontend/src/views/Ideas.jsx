import { useState } from "react";
import { api } from "../api.js";
import { ErrorBox, Note, Notes, Panel, useAsync } from "../components/common.jsx";
import { num } from "../format.js";

// Trade ideas: symbols triggering a strategy that survived validation.
//
// ## Why the evidence is on the page and not behind a click
//
// A list of tickers is the easiest thing in this application to build and the most
// dangerous thing to show, because it reads as a recommendation whatever the caption
// says. So the strategy's record sits directly under the symbols: the population it was
// tested on, the number of independent blocks, the p-value, and what it was charged to
// trade. Putting that behind a details panel means it is not read, and a list nobody
// reads the numbers on is a tip sheet.
//
// ## Why retired strategies are shown
//
// `gap_down_continuation` scored a z of 7.70 in sample against a multiplicity bar of
// 3.23 and then lost money out of sample. It is the most instructive entry in the
// registry, and a panel that showed only the survivors would look like a list of things
// that work rather than the result of a search that mostly found nothing.
//
// ## Why cost is a column
//
// The edge is a fixed number and the spread is not. The same trigger is worth roughly
// seventy basis points on a large cap and nothing at all on a uranium microcap quoting
// two hundred, so the cost belongs next to the symbol rather than in a footnote.

const STATUS_TONE = {
  supported: "sev sev-high",
  provisional: "sev sev-low",
  retired: "sev sev-quiet",
};

const pct = (value, digits = 2) =>
  value === null || value === undefined ? "-" : `${(value * 100).toFixed(digits)}%`;

function StrategyCard({ strategy }) {
  const [open, setOpen] = useState(false);
  const evidence = strategy.evidence;
  const retired = strategy.status === "retired";

  return (
    <div className={`idea-strategy ${retired ? "retired" : ""}`}>
      <div className="idea-strategy-head">
        <span className={STATUS_TONE[strategy.status] || "sev sev-quiet"}>
          {strategy.status}
        </span>
        <strong>{strategy.label}</strong>
        <button type="button" className="link-btn" onClick={() => setOpen(!open)}>
          {open ? "less" : "caveats"}
        </button>
      </div>

      <div className="idea-rationale">{strategy.rationale}</div>

      {evidence && (
        <>
          <div className="idea-evidence">
            <span>
              <em>edge</em> {pct(evidence.edge)}
            </span>
            <span>
              <em>p</em> {evidence.p_value.toFixed(3)}
            </span>
            <span>
              <em>blocks</em> {evidence.blocks}
            </span>
            <span>
              <em>net/trade</em> {pct(evidence.net_return)}
            </span>
          </div>
          <div className="chart-note">
            Tested on {evidence.tested_on}. Costs: {evidence.cost_basis}.
          </div>
          {open && (
            <ul className="idea-caveats">
              {evidence.caveats.map((caveat) => (
                <li key={caveat}>{caveat}</li>
              ))}
            </ul>
          )}
        </>
      )}
    </div>
  );
}

export default function Ideas({ onSelect }) {
  const [freshness, setFreshness] = useState(3);
  const ideas = useAsync(() => api.ideas({ freshness }), [freshness]);

  const rows = ideas.data?.ideas || [];
  const strategies = ideas.data?.strategies || [];
  const supported = strategies.filter((item) => item.status === "supported");

  return (
    <>
      <Panel
        title="Triggering now"
        right={
          <label className="inline-control">
            within
            <select
              value={freshness}
              onChange={(event) => setFreshness(Number(event.target.value))}
            >
              {[0, 1, 3, 5, 10].map((value) => (
                <option key={value} value={value}>
                  {value === 0 ? "today" : `${value} sessions`}
                </option>
              ))}
            </select>
          </label>
        }
      >
        <ErrorBox error={ideas.error} onRetry={ideas.reload} />
        {ideas.loading && <div className="loading">Evaluating...</div>}

        {!ideas.loading && rows.length === 0 && (
          <div className="empty-state">
            Nothing has triggered. That is the normal state: the supported rule fires on a
            few percent of symbol-days.
          </div>
        )}

        {rows.length > 0 && (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th className="left">symbol</th>
                  <th className="left">triggered</th>
                  <th>price</th>
                  <th title="This symbol's own estimated round trip spread.">cost</th>
                  <th>hold</th>
                  <th className="left">strategy</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr key={`${row.symbol}:${row.strategy}`}>
                    <td className="left">
                      <button
                        type="button"
                        className="link-btn"
                        onClick={() => onSelect(row.symbol)}
                      >
                        {row.symbol}
                      </button>
                    </td>
                    <td className="left muted">
                      {row.age === 0 ? "today" : `${row.age} sessions ago`}
                    </td>
                    <td>{num(row.price)}</td>
                    {/* Coloured against the edge it has to be paid out of: a spread wider
                        than the measured edge means this trigger is not worth taking on
                        this symbol, whatever the strategy's average says. */}
                    <td className={row.cost_bp > 71 ? "neg" : ""}>
                      {row.cost_bp.toFixed(0)} bp
                    </td>
                    <td>{row.horizon}d</td>
                    <td className="left muted">{row.strategy}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        <Notes items={ideas.data?.notes || []} />

        {supported.length > 0 && rows.length > 0 && (
          <div className="chart-note">
            Entry is the next open after the trigger, which is how these were tested. A
            cost above the strategy&apos;s measured edge is marked red: on that symbol the
            spread eats the whole thing.
          </div>
        )}
      </Panel>

      <Panel title="Why these are listed">
        <Note>
          Nothing appears above that has not survived a documented out-of-sample test, and
          no strategy is listed without the numbers that got it there. None of this is
          validated in the sense that matters, which is forward performance on money.
        </Note>
        {strategies.map((strategy) => (
          <StrategyCard key={strategy.key} strategy={strategy} />
        ))}
      </Panel>
    </>
  );
}
