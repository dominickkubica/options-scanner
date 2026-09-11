import { useMemo, useState } from "react";
import { api } from "../api.js";
import { useMeasuredWidth } from "../components/chart/plot.jsx";
import { ErrorBox, Notes, Panel, useAsync } from "../components/common.jsx";
import { count, money, num, pct } from "../format.js";

// The journal: a trade log's report surface, over real fills.
//
// Every number here comes from broker statements taken in by `optscan import`, grouped
// one entry per underlying per closing day. These are trades that happened, at the
// prices they actually filled, with the fees the broker actually charged.
//
// It used to report settled screen candidates instead, which was a measurement of the
// screen held to expiry with no fill and no slippage. That produced a +$426,644 total
// across 2,060 positions that were never held at once and never traded at all, on a
// calendar keyed to settlement dates rather than to any day the account was open. Those
// rows still exist and are still the calibration sample for the score; `optscan
// validate` is their report. They are not a journal.
//
// Three things are deliberate:
//
//   1. The sample banner is above the numbers, not under them. The API returns
//      `reportable: false` until the sample clears the minimum, and putting the caveat
//      below the tiles would be printing the headline and whispering the correction.
//      It now qualifies how much can be concluded rather than whether it happened.
//
//   2. Calendar cells carry their dollar value as text. The house gain/loss pair is
//      #46b17b against #d9635f, which measures ΔE 4.5 under deuteranopia: a red/green
//      colourblind reader cannot separate a winning day from a losing one by hue.
//      Normal vision separates them fine (ΔE 25.8) so the colours stay, but the value
//      is written into every cell so colour is reinforcement and never the only channel.
//
//   3. Expectancy leads with its interval. It is the number the whole tool exists to
//      answer and the one most likely to be read as settled. When zero sits inside the
//      interval the tile says so in words rather than leaving it to be inferred from
//      two numbers in brackets.

const PAD = { top: 12, right: 14, bottom: 24, left: 58 };
const CURVE_HEIGHT = 220;
const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

function signClass(value) {
  if (value === null || value === undefined || value === 0) return "";
  return value > 0 ? "good" : "bad";
}

function signedMoney(value, digits = 0) {
  if (value === null || value === undefined) return "n/a";
  return `${value > 0 ? "+" : value < 0 ? "-" : ""}$${Math.abs(value).toFixed(digits)}`;
}

// A proportion with the interval it actually earned, never the bare number.
function IntervalText({ interval, digits = 0 }) {
  if (!interval) return <>n/a</>;
  return (
    <>
      {pct(interval.value, digits)}{" "}
      <span className="interval">
        ({pct(interval.low, digits)} to {pct(interval.high, digits)})
      </span>
    </>
  );
}

function Tile({ label, value, sub, tone }) {
  return (
    <div className="tile">
      <div className="metric-label">{label}</div>
      <div className={`tile-value ${tone || ""}`}>{value}</div>
      {sub && <div className="tile-sub">{sub}</div>}
    </div>
  );
}

// Single series, so no legend: the panel title names it. 2px line, recessive grid,
// crosshair on hover.
function EquityCurve({ days }) {
  // Measured rather than a 720 pixel assumption. An equity curve is the most
  // persuasive object on this page and it was being scaled to fit whatever the
  // panel happened to be, which grew the axis type along with it.
  const [ref, width] = useMeasuredWidth(720);
  const [hover, setHover] = useState(null);

  const geom = useMemo(() => {
    if (days.length === 0) return null;
    const values = days.map((point) => point.cumulative);
    let low = Math.min(0, ...values);
    let high = Math.max(0, ...values);
    if (low === high) {
      low -= 1;
      high += 1;
    }
    const pad = (high - low) * 0.08;
    low -= pad;
    high += pad;

    const plotW = width - PAD.left - PAD.right;
    const plotH = CURVE_HEIGHT - PAD.top - PAD.bottom;
    const x = (index) =>
      PAD.left +
      (days.length === 1 ? plotW / 2 : (index / (days.length - 1)) * plotW);
    const y = (value) =>
      PAD.top + plotH - ((value - low) / (high - low)) * plotH;
    return { x, y, low, high, plotW, plotH };
  }, [days, width]);

  if (!geom) return <div className="empty-state">No closed trades yet. Import a broker statement with `optscan import`.</div>;

  const path = days
    .map(
      (point, i) =>
        `${i === 0 ? "M" : "L"}${geom.x(i)},${geom.y(point.cumulative)}`,
    )
    .join(" ");
  const zeroY = geom.y(0);
  const ticks = [geom.high, (geom.high + geom.low) / 2, geom.low];

  return (
    <div className="curve-wrap" ref={ref}>
      <svg
        viewBox={`0 0 ${width} ${CURVE_HEIGHT}`}
        className="curve"
        onMouseLeave={() => setHover(null)}
      >
        {ticks.map((value, i) => (
          <g key={i}>
            <line
              x1={PAD.left}
              x2={width - PAD.right}
              y1={geom.y(value)}
              y2={geom.y(value)}
              className="grid-line"
            />
            <text
              x={PAD.left - 8}
              y={geom.y(value) + 4}
              className="axis-label"
              textAnchor="end"
            >
              {money(value)}
            </text>
          </g>
        ))}

        {geom.low < 0 && geom.high > 0 && (
          <line
            x1={PAD.left}
            x2={width - PAD.right}
            y1={zeroY}
            y2={zeroY}
            className="zero-line"
          />
        )}

        <path d={path} className="curve-line" />

        {days.map((point, i) => (
          <circle
            key={point.day}
            cx={geom.x(i)}
            cy={geom.y(point.cumulative)}
            r={4}
            className="curve-dot"
          />
        ))}

        {hover !== null && (
          <line
            x1={geom.x(hover)}
            x2={geom.x(hover)}
            y1={PAD.top}
            y2={CURVE_HEIGHT - PAD.bottom}
            className="crosshair"
          />
        )}

        {/* Hit targets wider than the marks, so hovering does not require precision. */}
        {days.map((point, i) => (
          <rect
            key={`hit-${point.day}`}
            x={geom.x(i) - geom.plotW / Math.max(days.length * 2, 2)}
            y={PAD.top}
            width={Math.max(geom.plotW / Math.max(days.length, 1), 24)}
            height={geom.plotH}
            fill="transparent"
            onMouseEnter={() => setHover(i)}
          />
        ))}
      </svg>

      <div className="curve-readout">
        {hover === null ? (
          <span className="provenance">
            {days.length} trading {days.length === 1 ? "day" : "days"}, cumulative
          </span>
        ) : (
          <>
            <strong>{days[hover].day}</strong>{" "}
            <span className={signClass(days[hover].profit)}>
              {signedMoney(days[hover].profit)}
            </span>{" "}
            <span className="provenance">
              on the day, {signedMoney(days[hover].cumulative)} cumulative,{" "}
              {count(days[hover].trades)}{" "}
              {days[hover].trades === 1 ? "entry" : "entries"}
            </span>
          </>
        )}
      </div>
    </div>
  );
}

// Month grids, one per month that actually has a settlement. A full year of empty
// squares is not a calendar, it is a texture.
function CalendarPnl({ days }) {
  const months = useMemo(() => {
    const byMonth = new Map();
    for (const point of days) {
      const key = point.day.slice(0, 7);
      if (!byMonth.has(key)) byMonth.set(key, new Map());
      byMonth.get(key).set(point.day, point);
    }
    return [...byMonth.entries()].sort(([a], [b]) => a.localeCompare(b));
  }, [days]);

  if (months.length === 0)
    return <div className="empty-state">No closed trades yet. Import a broker statement with `optscan import`.</div>;

  return (
    <div className="calendars">
      {months.map(([month, points]) => {
        const [year, monthIndex] = month.split("-").map(Number);
        const first = new Date(Date.UTC(year, monthIndex - 1, 1));
        const daysInMonth = new Date(
          Date.UTC(year, monthIndex, 0),
        ).getUTCDate();
        // Monday-first, matching how a trading week is read.
        const lead = (first.getUTCDay() + 6) % 7;
        const total = sumMonth(points);

        return (
          <div className="calendar" key={month}>
            <div className="calendar-head">
              <span>
                {first.toLocaleString("en-US", {
                  month: "long",
                  year: "numeric",
                  timeZone: "UTC",
                })}
              </span>
              <span className={signClass(total)}>{signedMoney(total)}</span>
            </div>
            <div className="calendar-grid">
              {WEEKDAYS.map((name) => (
                <div key={name} className="calendar-weekday">
                  {name}
                </div>
              ))}
              {Array.from({ length: lead }, (_, i) => (
                <div key={`lead-${i}`} className="calendar-cell empty" />
              ))}
              {Array.from({ length: daysInMonth }, (_, i) => {
                const iso = `${month}-${String(i + 1).padStart(2, "0")}`;
                const point = points.get(iso);
                return (
                  <div
                    key={iso}
                    className={`calendar-cell ${point ? `filled ${signClass(point.profit)}` : ""}`}
                    title={
                      point
                        ? `${iso}: ${signedMoney(point.profit)} over ${point.trades} ${point.trades === 1 ? "entry" : "entries"}`
                        : iso
                    }
                  >
                    <span className="calendar-day">{i + 1}</span>
                    {/* The value, not just the colour. See note 2 at the top. */}
                    {point && (
                      <span className="calendar-value">
                        {signedMoney(point.profit)}
                      </span>
                    )}
                  </div>
                );
              })}
            </div>
          </div>
        );
      })}
    </div>
  );
}

function sumMonth(points) {
  let total = 0;
  for (const point of points.values()) total += point.profit;
  return total;
}

function BreakdownTable({ title, rows }) {
  if (rows.length === 0) return null;
  return (
    <div className="breakdown">
      <div className="section-label">{title}</div>
      <div className="breakdown-scroll">
        <table>
          <thead>
            <tr>
              <th className="left">{title}</th>
              <th>n</th>
              <th>clusters</th>
              <th>win rate</th>
              <th>mean</th>
              <th>total</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr
                key={row.key}
                className={row.reportable ? "" : "under-sampled"}
              >
                <td className="left">{row.key.replace(/_/g, " ")}</td>
                <td>{count(row.trades)}</td>
                {/* The number that governs, so it is never the small print. */}
                <td>{count(row.clusters)}</td>
                <td>{row.win_rate ? pct(row.win_rate.value, 0) : "n/a"}</td>
                <td className={signClass(row.mean_profit)}>
                  {signedMoney(row.mean_profit, 2)}
                </td>
                <td className={signClass(row.total_profit)}>
                  {signedMoney(row.total_profit)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// Uploading a Robinhood statement.
//
// The ledger keys rows on a digest of their own contents, so re-uploading an
// overlapping export adds only what is new. That makes this safe to press repeatedly,
// which matters because the broker exports date ranges rather than deltas: every
// download after the first overlaps the last, and the honest report is "457 rows, 25
// new" rather than a silent success that could equally mean nothing happened.
function ImportStatement({ onImported }) {
  const [state, setState] = useState({ status: "idle" });

  const choose = (event) => {
    const file = event.target.files?.[0];
    // Clear the input so choosing the same file twice fires change twice. Without this
    // a failed import cannot be retried by picking the same file again.
    event.target.value = "";
    if (!file) return;

    setState({ status: "working" });
    file
      .text()
      .then((text) => api.importStatement(text))
      .then((result) => {
        setState({ status: "done", result });
        onImported?.();
      })
      .catch((error) => setState({ status: "failed", error: error.message }));
  };

  return (
    <div className="import-row">
      <label className="import-btn">
        <input type="file" accept=".csv,text/csv" onChange={choose} hidden />
        {state.status === "working" ? "Reading..." : "Import Robinhood CSV"}
      </label>
      {state.status === "done" && (
        <span className="import-note good">
          {state.result.detail}
          {state.result.first_date && (
            <> {state.result.first_date} to {state.result.last_date}.</>
          )}
        </span>
      )}
      {state.status === "failed" && <span className="import-note bad">{state.error}</span>}
      {state.status === "idle" && (
        <span className="import-note">
          Account &rarr; Statements &amp; History &rarr; Export. Safe to re-upload:
          rows already held are skipped.
        </span>
      )}
    </div>
  );
}

export default function Journal() {
  const [symbol, setSymbol] = useState("");
  const [strategy, setStrategy] = useState("");
  const [imported, setImported] = useState(0);
  const { data, error, loading } = useAsync(
    () => api.journal({ symbol, strategy }),
    [symbol, strategy, imported],
  );

  if (loading)
    return <div className="loading">Reading the ledger...</div>;
  if (error) return <ErrorBox error={error} />;
  if (!data) return null;

  const expectancy = data.expectancy;
  const symbols = data.by_symbol.map((row) => row.key);
  const strategies = data.by_strategy.map((row) => row.key);

  return (
    <>
      <Panel title="Import trades">
        <ImportStatement onImported={() => setImported((value) => value + 1)} />
      </Panel>

      {/* Above the numbers, deliberately. The banner now qualifies the sample rather
          than disowning the source: these are real fills from imported statements, so
          the caveat is about how much can be concluded, not about whether it happened. */}
      {!data.reportable && (
        <div className="sample-banner">
          <strong>Real fills, small sample.</strong> {count(data.trades)} closed{" "}
          {data.trades === 1 ? "day" : "days"} of trading across{" "}
          {count(data.settlement_dates)}{" "}
          {data.settlement_dates === 1 ? "session" : "sessions"}, below the{" "}
          minimum this tool will draw a conclusion from. Everything here happened,
          and none of it is yet enough to tell an edge from noise.
        </div>
      )}

      <Panel
        title="Performance"
        right={
          <div className="controls">
            <label>
              symbol{" "}
              <select
                value={symbol}
                onChange={(event) => setSymbol(event.target.value)}
              >
                <option value="">all</option>
                {symbols.map((name) => (
                  <option key={name} value={name}>
                    {name}
                  </option>
                ))}
              </select>
            </label>
            <label>
              strategy{" "}
              <select
                value={strategy}
                onChange={(event) => setStrategy(event.target.value)}
              >
                <option value="">all</option>
                {strategies.map((name) => (
                  <option key={name} value={name}>
                    {name.replace(/_/g, " ")}
                  </option>
                ))}
              </select>
            </label>
          </div>
        }
      >
        <div className="tile-row">
          {/* "closed trades", not "trading days". This tile showed the cluster count
              under a label that means something else: the grain here is
              (symbol, closing day), so 43 is forty-three symbol-days, while trading
              actually happened on 26 sessions. The two were within a plausible
              distance of each other, which is exactly what made the wrong label
              survive -- 43 is also roughly the number of market sessions in the
              imported range, so the number looked right for the label it had. */}
          <Tile
            label="closed trades"
            value={count(data.trades)}
            sub={`${count(data.settlement_dates)} sessions traded`}
          />
          <Tile
            label="win rate"
            value={<IntervalText interval={data.win_rate} />}
          />
          {/* Per trade, not per day. The figure is total profit over the cluster
              count, so calling it "per day" overstated the denominator and therefore
              understated the number: $139.26 over 43 clusters is $3.24 a trade, while
              over the 26 sessions actually traded it would be $5.36 a day. Both are
              true of different questions; the one computed here is per trade. */}
          <Tile
            label="expectancy / trade"
            value={expectancy ? signedMoney(expectancy.value, 2) : "n/a"}
            tone={expectancy ? signClass(expectancy.value) : ""}
            sub={
              expectancy
                ? expectancy.low === null
                  ? "one cluster, no interval"
                  : expectancy.indistinguishable_from_zero
                    ? `${signedMoney(expectancy.low, 0)} to ${signedMoney(expectancy.high, 0)} — includes zero`
                    : `${signedMoney(expectancy.low, 0)} to ${signedMoney(expectancy.high, 0)}`
                : null
            }
          />
          <Tile
            label="avg win"
            value={signedMoney(data.avg_win, 2)}
            tone="good"
          />
          <Tile
            label="avg loss"
            value={signedMoney(data.avg_loss, 2)}
            tone="bad"
          />
          <Tile
            label="profit factor"
            value={
              data.profit_factor === null
                ? "undefined"
                : num(data.profit_factor, 2)
            }
            sub={data.profit_factor === null ? "nothing lost yet" : null}
          />
          <Tile
            label="total"
            value={signedMoney(data.total_profit)}
            tone={signClass(data.total_profit)}
          />
          <Tile label="max drawdown" value={money(data.max_drawdown)} />
        </div>
      </Panel>

      <Panel title="Cumulative profit by closing date">
        <EquityCurve days={data.days} />
      </Panel>

      <Panel title="Calendar">
        <CalendarPnl days={data.days} />
      </Panel>

      <Panel title="Breakdowns">
        <div className="breakdown-grid">
          <BreakdownTable title="instrument" rows={data.by_strategy} />
          <BreakdownTable title="symbol" rows={data.by_symbol} />
          <BreakdownTable title="days held" rows={data.by_dte} />
          {/* The score breakdown is absent on purpose and the API returns it empty:
              a trade taken at a broker was never scored by this tool, so a score
              column over real fills would be invented numbers. Whether the score
              predicts anything is `optscan validate`, over the candidates it
              actually scored. */}
        </div>
        <div className="caveat">
          Rows are dimmed where the group has too few independent days to support a
          reading. Days held is 0 for a position opened and closed in one session,
          which is most of this account.
        </div>
      </Panel>

      <Notes items={data.notes} />
    </>
  );
}
