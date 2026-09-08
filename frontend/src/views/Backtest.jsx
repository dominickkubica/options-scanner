import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../api.js";
import { ErrorBox, Note, Notes, Panel, useAsync } from "../components/common.jsx";
import { Plot, PlotReadout, linePath } from "../components/chart/plot.jsx";

// Backtesting: build a strategy, run it, and read what the answer is actually worth.
//
// ## Why the null gets equal billing with the result
//
// The mean return is the number everybody looks at and the least informative thing on
// the page. A long rule over the last decade made money because the market went up, so
// the only question worth asking is whether it beat entering at the same frequency on
// arbitrary days. That comparison is the `edge` row, and it is rendered as a band with
// the strategy's mark inside it rather than as a lone number, because seeing the mark
// sitting comfortably inside the null is worth more than reading a p-value.
//
// ## Why the sample size is shown next to the trade count and coloured
//
// A run reports thousands of trades and a hundred independent blocks, and the second
// number is the real one. Ninety-eight technology names entered in the same week are
// one observation of one market episode. Showing the trade count alone would be the
// most flattering true statement available, which is exactly the kind of number this
// project tries not to print without its companion.

const MODES = [
  { key: "underlying", label: "Stock" },
  { key: "short_put", label: "Short put" },
  { key: "short_call", label: "Short call" },
];

//: Below this many independent blocks the engine refuses to claim anything. Mirrors
//: MIN_BLOCKS_FOR_A_CLAIM; the server also sends `enough_to_claim` and that is what the
//: colour follows, so this is only used for the explanatory sentence.
const BLOCK_FLOOR = 20;

const pct = (value, digits = 2) =>
  value === null || value === undefined ? "-" : `${(value * 100).toFixed(digits)}%`;

function EdgeBand({ edge }) {
  // The null's 5th to 95th percentile as a bar, with the strategy's mean as a tick.
  // Position is clamped so a strategy far outside the band still renders on the edge
  // rather than overflowing the panel.
  const span = edge.null_high - edge.null_low || 1;
  const raw = (edge.strategy_mean - edge.null_low) / span;
  const position = Math.max(0, Math.min(1, raw));
  const beat = edge.p_value <= 0.05;

  return (
    <div className="edge-band">
      <div className="edge-track">
        <div className="edge-fill" />
        <div
          className={`edge-mark ${beat ? "beat" : ""}`}
          style={{ left: `${position * 100}%` }}
          title={`strategy ${pct(edge.strategy_mean)}`}
        />
      </div>
      <div className="edge-scale">
        <span>{pct(edge.null_low)}</span>
        <span className="edge-caption">
          random entry, 5th to 95th percentile
          {raw < 0 || raw > 1 ? " (strategy is outside this range)" : ""}
        </span>
        <span>{pct(edge.null_high)}</span>
      </div>
    </div>
  );
}

function EquityChart({ points }) {
  const [hover, setHover] = useState(null);
  const xs = useMemo(() => points.map((_, index) => index), [points]);
  const ys = useMemo(() => points.map((point) => point.value), [points]);

  const onHoverX = useCallback(
    (value) => {
      if (value === null || !points.length) return setHover(null);
      const index = Math.max(0, Math.min(points.length - 1, Math.round(value)));
      return setHover({ ...points[index], index });
    },
    [points],
  );

  return (
    <div>
      <PlotReadout
        items={[
          { label: "month", value: hover ? hover.date : "hover" },
          {
            label: "equity",
            value: hover ? hover.value.toFixed(3) : "-",
            tone: hover && hover.value < 1 ? "bad" : undefined,
          },
          {
            label: "final",
            value: points.length ? points[points.length - 1].value.toFixed(3) : "-",
          },
        ]}
      />
      <Plot
        height={220}
        xs={xs}
        ys={ys}
        formatY={(value) => value.toFixed(2)}
        onHoverX={onHoverX}
        empty="No trades, so there is no curve."
      >
        {({ scale }) => (
          <>
            {/* The starting line. Without it a curve that lost money still looks like a
                rising shape, because the axis pads to the data. */}
            <line
              x1={scale.left}
              x2={scale.right}
              y1={scale.toY(1)}
              y2={scale.toY(1)}
              stroke="var(--chart-axis)"
              strokeDasharray="2 4"
            />
            <path
              d={linePath(
                points.map((point, index) => ({
                  x: scale.toX(index),
                  y: scale.toY(point.value),
                })),
              )}
              fill="none"
              stroke="var(--accent)"
              strokeWidth="2"
            />
            {hover && (
              <circle
                cx={scale.toX(hover.index)}
                cy={scale.toY(hover.value)}
                r="4"
                fill="var(--accent)"
              />
            )}
          </>
        )}
      </Plot>
      <div className="chart-note">
        An equal weighted portfolio rebalanced monthly, starting at 1.0. One point per
        month, not per trade: a rule firing across many symbols opens many concurrent
        positions, and compounding those as though they ran one after another invents
        leverage the account never had.
      </div>
    </div>
  );
}

export default function Backtest() {
  const rules = useAsync(() => api.backtestRules(), []);

  const [entry, setEntry] = useState("rsi_below");
  const [params, setParams] = useState({});
  const [symbols, setSymbols] = useState("");
  const [group, setGroup] = useState("");
  const [mode, setMode] = useState("underlying");
  const [horizon, setHorizon] = useState(21);
  const [slippage, setSlippage] = useState(0.05);
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);

  const selected = useMemo(
    () => (rules.data || []).find((rule) => rule.name === entry),
    [rules.data, entry],
  );

  // Reset the parameters to the chosen rule's defaults. Carrying the previous rule's
  // parameters over would send `threshold` to a rule that has no threshold, which the
  // server refuses rather than ignores.
  useEffect(() => {
    if (selected) setParams({ ...selected.params });
  }, [selected]);

  const submit = async () => {
    setRunning(true);
    setError(null);
    try {
      const payload = {
        name: entry,
        entry,
        entry_params: params,
        symbols: symbols
          .split(/[\s,]+/)
          .map((s) => s.trim().toUpperCase())
          .filter(Boolean),
        group: group || null,
        mode,
        horizon: Number(horizon),
        slippage: Number(slippage),
        draws: 500,
      };
      setResult(await api.runBacktest(payload));
    } catch (caught) {
      setError(caught);
    } finally {
      setRunning(false);
    }
  };

  const stats = result?.stats;
  const edge = result?.edge;

  return (
    <>
      <Panel title="Strategy">
        <ErrorBox error={rules.error} onRetry={rules.reload} />
        <div className="controls backtest-form">
          <label>
            entry
            <select value={entry} onChange={(event) => setEntry(event.target.value)}>
              {(rules.data || []).map((rule) => (
                <option key={rule.name} value={rule.name}>
                  {rule.label}
                </option>
              ))}
            </select>
          </label>

          {Object.entries(params).map(([key, value]) => (
            <label key={key}>
              {key}
              <input
                type="number"
                step="any"
                value={value}
                onChange={(event) =>
                  setParams((current) => ({
                    ...current,
                    [key]: Number(event.target.value),
                  }))
                }
              />
            </label>
          ))}

          <label>
            mode
            <select value={mode} onChange={(event) => setMode(event.target.value)}>
              {MODES.map((item) => (
                <option key={item.key} value={item.key}>
                  {item.label}
                </option>
              ))}
            </select>
          </label>

          {mode === "underlying" ? (
            <label>
              hold (bars)
              <input
                type="number"
                value={horizon}
                onChange={(event) => setHorizon(event.target.value)}
              />
            </label>
          ) : (
            <label>
              slippage
              <input
                type="number"
                step="0.01"
                value={slippage}
                onChange={(event) => setSlippage(event.target.value)}
              />
            </label>
          )}

          <label>
            symbols
            <input
              type="text"
              placeholder="AAPL MSFT NVDA"
              value={symbols}
              onChange={(event) => setSymbols(event.target.value)}
            />
          </label>

          <label>
            or group
            <input
              type="text"
              placeholder="tech"
              value={group}
              onChange={(event) => setGroup(event.target.value)}
            />
          </label>

          <button type="button" className="btn" onClick={submit} disabled={running}>
            {running ? "Running..." : "Run"}
          </button>
        </div>

        {selected && <div className="chart-note">{selected.about}</div>}
        {mode !== "underlying" && (
          <Note>
            Option modes price the entry credit from stored vendor implied volatility and
            settle exactly against the real close, so only the fill is modelled. That
            history exists for two symbols, and any other symbol is refused rather than
            priced off realized volatility: the gap between implied and realized is the
            variance risk premium, which is the edge being measured.
          </Note>
        )}
        <ErrorBox error={error} onRetry={submit} />
      </Panel>

      {running && <div className="loading">Running the strategy and its null...</div>}

      {result && stats && (
        <>
          <Panel title="Result" right={<span className="muted">{result.seconds}s</span>}>
            <div className="stats">
              <div className="stat">
                <div className="label">mean per trade</div>
                <div className={`value ${stats.mean_return < 0 ? "neg" : "pos"}`}>
                  {pct(stats.mean_return)}
                </div>
              </div>
              <div className="stat">
                <div className="label">win rate</div>
                <div className="value">{pct(stats.win_rate, 0)}</div>
              </div>
              <div className="stat">
                <div className="label">trades</div>
                <div className="value">{stats.trades}</div>
              </div>
              <div className="stat">
                <div className="label">independent blocks</div>
                <div className={`value ${stats.enough_to_claim ? "" : "neg"}`}>
                  {stats.effective_sample}
                </div>
              </div>
              <div className="stat">
                <div className="label">best / worst</div>
                <div className="value">
                  {pct(stats.best, 0)} / {pct(stats.worst, 0)}
                </div>
              </div>
            </div>

            {!stats.enough_to_claim && (
              <Note>
                {stats.trades} trades fall into only {stats.effective_sample} independent
                blocks, below the floor of {BLOCK_FLOOR}. Trades that overlap in one
                market episode are one observation, so nothing here is a claim.
              </Note>
            )}

            {edge && (
              <>
                <div className="section-label" style={{ padding: "16px 0 4px" }}>
                  Against entering at the same frequency on arbitrary days
                </div>
                <EdgeBand edge={edge} />
                <div className="stats">
                  <div className="stat">
                    <div className="label">random entry returned</div>
                    <div className="value">{pct(edge.null_mean)}</div>
                  </div>
                  <div className="stat">
                    <div className="label">edge over that</div>
                    <div className={`value ${edge.edge < 0 ? "neg" : "pos"}`}>
                      {pct(edge.edge)}
                    </div>
                  </div>
                  <div className="stat">
                    <div className="label">p</div>
                    <div className={`value ${edge.p_value <= 0.05 ? "pos" : "neg"}`}>
                      {edge.p_value.toFixed(3)}
                    </div>
                  </div>
                </div>
              </>
            )}
          </Panel>

          <Panel title="Equity">
            <EquityChart points={result.equity} />
          </Panel>

          <Panel title="By year">
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th className="left">year</th>
                    <th>trades</th>
                    <th>mean</th>
                    <th>win</th>
                    <th>portfolio</th>
                  </tr>
                </thead>
                <tbody>
                  {result.by_year.map((row) => (
                    <tr key={row.year}>
                      <td className="left">{row.year}</td>
                      <td>{row.trades}</td>
                      <td className={row.mean_return < 0 ? "neg" : "pos"}>
                        {pct(row.mean_return)}
                      </td>
                      <td>{pct(row.win_rate, 0)}</td>
                      <td className={row.total_return < 0 ? "neg" : "pos"}>
                        {pct(row.total_return, 1)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="chart-note">
              A strategy that worked until 2021 and died after would average out to
              something respectable. This is where that shows.
            </div>
          </Panel>

          <Panel title="What this does and does not say">
            <Notes items={result.notes} />
            {Object.keys(result.skipped).length > 0 && (
              <div className="chart-note">
                {Object.keys(result.skipped).length} symbols were left out:{" "}
                {Object.entries(result.skipped)
                  .slice(0, 6)
                  .map(([symbol, why]) => `${symbol} (${why.split(".")[0]})`)
                  .join(", ")}
                .
              </div>
            )}
          </Panel>
        </>
      )}
    </>
  );
}
