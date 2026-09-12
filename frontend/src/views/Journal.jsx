import { useMemo, useState } from "react";
import { api } from "../api.js";
import { useMeasuredWidth } from "../components/chart/plot.jsx";
import { ErrorBox, Notes, Panel, useAsync } from "../components/common.jsx";
import { count, money, num, pct } from "../format.js";

// The journal: a trade log's report surface, over real fills.
//
// Every number here comes from broker statements taken in by `optscan import` or the
// Import button. Two grains sit on one page, on purpose:
//
//   - The headline tiles, the curve and the calendar are one entry per underlying per
//     closing day. That grain was audited to the cent against the ledger, and it is
//     also the unit of independence, so it carries the intervals.
//   - The trade table and everything below it are one row per position as it was
//     placed: a condor is one row of four legs. Filters apply to positions first and
//     the headline is rebuilt from the chosen positions, so the two always agree.
//
// Three things are deliberate:
//
//   1. The sample banner is above the numbers, not under them. Below them it would be
//      printing the headline and whispering the correction.
//
//   2. Calendar cells carry their dollar value as text. The house gain/loss pair
//      measures ΔE 4.5 under deuteranopia, so colour is reinforcement and never the
//      only channel.
//
//   3. Expectancy leads with its interval, and says in words when zero is inside it.
//
// Nothing time-of-day based is ever estimated. The Robinhood CSV carries dates only;
// times come from the order history import or from what the trader types.

const PAD = { top: 12, right: 14, bottom: 24, left: 58 };
const CURVE_HEIGHT = 220;
const SIZING_HEIGHT = 120;
const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

function signClass(value) {
  if (value === null || value === undefined || value === 0) return "";
  return value > 0 ? "good" : "bad";
}

function signedMoney(value, digits = 0) {
  if (value === null || value === undefined) return "n/a";
  return `${value > 0 ? "+" : value < 0 ? "-" : ""}$${Math.abs(value).toFixed(digits)}`;
}

function signedR(value) {
  if (value === null || value === undefined) return "n/a";
  return `${value > 0 ? "+" : ""}${value.toFixed(2)}R`;
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
      PAD.left + (days.length === 1 ? plotW / 2 : (index / (days.length - 1)) * plotW);
    const y = (value) => PAD.top + plotH - ((value - low) / (high - low)) * plotH;
    return { x, y, low, high, plotW, plotH };
  }, [days, width]);

  if (!geom)
    return <div className="empty-state">No closed trades yet. Import a Robinhood statement above.</div>;

  const path = days
    .map((point, i) => `${i === 0 ? "M" : "L"}${geom.x(i)},${geom.y(point.cumulative)}`)
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
            <text x={PAD.left - 8} y={geom.y(value) + 4} className="axis-label" textAnchor="end">
              {money(value)}
            </text>
          </g>
        ))}

        {geom.low < 0 && geom.high > 0 && (
          <line x1={PAD.left} x2={width - PAD.right} y1={zeroY} y2={zeroY} className="zero-line" />
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
            <span className={signClass(days[hover].profit)}>{signedMoney(days[hover].profit)}</span>{" "}
            <span className="provenance">
              on the day, {signedMoney(days[hover].cumulative)} cumulative,{" "}
              {count(days[hover].trades)} {days[hover].trades === 1 ? "entry" : "entries"}
            </span>
          </>
        )}
      </div>
    </div>
  );
}

// Month grids, one per month that actually has a close. Clicking a day lists every
// position closed on it in the trade table below.
function CalendarPnl({ days, selected, onSelect }) {
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
    return <div className="empty-state">No closed trades yet. Import a Robinhood statement above.</div>;

  return (
    <div className="calendars">
      {months.map(([month, points]) => {
        const [year, monthIndex] = month.split("-").map(Number);
        const first = new Date(Date.UTC(year, monthIndex - 1, 1));
        const daysInMonth = new Date(Date.UTC(year, monthIndex, 0)).getUTCDate();
        // Monday-first, matching how a trading week is read.
        const lead = (first.getUTCDay() + 6) % 7;
        const total = sumMonth(points);

        return (
          <div className="calendar" key={month}>
            <div className="calendar-head">
              <span>
                {first.toLocaleString("en-US", { month: "long", year: "numeric", timeZone: "UTC" })}
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
                const classes = [
                  "calendar-cell",
                  point ? `filled clickable ${signClass(point.profit)}` : "",
                  selected === iso ? "selected" : "",
                ].join(" ");
                return (
                  <div
                    key={iso}
                    className={classes}
                    onClick={point ? () => onSelect(selected === iso ? null : iso) : undefined}
                    title={
                      point
                        ? `${iso}: ${signedMoney(point.profit)} over ${point.trades} ${point.trades === 1 ? "entry" : "entries"}. Click to list the trades.`
                        : iso
                    }
                  >
                    <span className="calendar-day">{i + 1}</span>
                    {/* The value, not just the colour. See note 2 at the top. */}
                    {point && (
                      <span className="calendar-value">{signedMoney(point.profit)}</span>
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

function BreakdownTable({ title, rows, extra }) {
  if (!rows || rows.length === 0) return null;
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
              {extra && <th>{extra.label}</th>}
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.key} className={row.reportable ? "" : "under-sampled"}>
                <td className="left">{row.key.replace(/_/g, " ")}</td>
                <td>{count(row.trades)}</td>
                {/* The number that governs, so it is never the small print. */}
                <td>{count(row.clusters)}</td>
                <td>{row.win_rate ? pct(row.win_rate.value, 0) : "n/a"}</td>
                <td className={signClass(row.mean_profit)}>{signedMoney(row.mean_profit, 2)}</td>
                <td className={signClass(row.total_profit)}>{signedMoney(row.total_profit)}</td>
                {extra && <td>{extra.render(row)}</td>}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// Uploading a Robinhood statement, and taking the journal back out as CSV.
//
// The ledger keys rows on a digest of their own contents, so re-uploading an
// overlapping export adds only what is new, and the result says how much was new.
function ImportStatement({ onImported }) {
  const [state, setState] = useState({ status: "idle" });

  const choose = (event) => {
    const file = event.target.files?.[0];
    // Clear the input so choosing the same file twice fires change twice.
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
      <a className="import-btn secondary" href={api.journalExportUrl("positions")} download>
        Export trades CSV
      </a>
      <a className="import-btn secondary" href={api.journalExportUrl("fills")} download>
        Export fills CSV
      </a>
      {state.status === "done" && (
        <span className="import-note good">
          {state.result.detail}
          {state.result.first_date && (
            <>
              {" "}
              {state.result.first_date} to {state.result.last_date}.
            </>
          )}
        </span>
      )}
      {state.status === "failed" && <span className="import-note bad">{state.error}</span>}
      {state.status === "idle" && (
        <span className="import-note">
          Account &rarr; Statements &amp; History &rarr; Export. Safe to re-upload: rows already
          held are skipped.
        </span>
      )}
    </div>
  );
}

// -- The trade table ---------------------------------------------------------------

function strike(value) {
  if (value === null || value === undefined) return "";
  return Number.isInteger(value) ? String(value) : value.toFixed(1);
}

// "P -500/+495 · C -520/+525": the shape at a glance, short legs marked minus.
function legSummary(position) {
  if (position.legs.every((leg) => leg.right === null)) return `${num(position.units, 0)} sh`;
  const part = (right, letter) => {
    const legs = position.legs.filter((leg) => leg.right === right);
    if (legs.length === 0) return null;
    return `${letter} ${legs.map((leg) => `${leg.side === "short" ? "-" : "+"}${strike(leg.strike)}`).join("/")}`;
  };
  return [part("put", "P"), part("call", "C")].filter(Boolean).join(" · ");
}

function price(value, credit) {
  if (value === null || value === undefined) return "";
  const kind = value >= 0 === credit ? "cr" : "db";
  return `${Math.abs(value).toFixed(2)} ${kind}`;
}

function TradeTable({ positions, selected, onSelect, book, onSaved }) {
  // The editor sits in a row spanning thirteen columns, so on its own it is as wide as
  // the table and scrolls out of view on a narrow window. Pinned to the visible width of
  // the scroll area, it stays readable however wide the table gets.
  const [ref, width] = useMeasuredWidth(900);
  if (positions.length === 0)
    return <div className="empty-state">No trades match these filters.</div>;

  const ordered = [...positions].sort((a, b) =>
    (b.closed_at || b.opened_at).localeCompare(a.closed_at || a.opened_at),
  );

  return (
    <div className="breakdown-scroll" ref={ref}>
      <table className="trade-table">
        <thead>
          <tr>
            <th className="left">opened</th>
            <th className="left">closed</th>
            <th className="left">symbol</th>
            <th className="left">strategy</th>
            <th>DTE</th>
            <th className="left">legs</th>
            <th>qty</th>
            <th>entry</th>
            <th>exit</th>
            <th>P&amp;L</th>
            <th>R</th>
            <th>in / out (PT)</th>
            <th className="left">tags</th>
          </tr>
        </thead>
        <tbody>
          {ordered.map((p) => {
            const open = selected === p.key;
            const overridden = p.strategy !== p.strategy_guess;
            return [
              <tr
                key={p.key}
                className={`clickable ${open ? "selected" : ""}`}
                onClick={() => onSelect(open ? null : p.key)}
              >
                <td className="left" title={p.opened_at}>
                  {p.opened_at.slice(5)} {p.weekday}
                </td>
                <td className="left">{p.is_open ? "open" : p.closed_at.slice(5)}</td>
                <td className="left">{p.symbol}</td>
                <td className="left">
                  {!overridden && !p.guess_confident ? (
                    <span className="guess" title="Guessed from the legs. Open it to tag it.">
                      {p.strategy}?
                    </span>
                  ) : (
                    p.strategy
                  )}
                </td>
                <td>{p.dte_at_entry ?? ""}</td>
                <td className="left mono">{legSummary(p)}</td>
                <td>{num(p.units, 0)}</td>
                <td>{price(p.entry_price, true)}</td>
                <td>{price(p.exit_price, false)}</td>
                <td className={signClass(p.realized)}>
                  {p.is_open ? "open" : signedMoney(p.realized, 2)}
                </td>
                <td className={signClass(p.r_multiple)}>
                  {p.r_multiple === null ? "" : signedR(p.r_multiple)}
                </td>
                <td>
                  {p.entry_time || p.exit_time ? `${p.entry_time || "?"} / ${p.exit_time || "?"}` : ""}
                </td>
                <td className="left">
                  {p.flags.map((flag) => (
                    <span key={`f-${flag}`} className="chip flag" title="Raised by the journal">
                      {flag}
                    </span>
                  ))}
                  {p.tags.map((tag) => (
                    <span key={`t-${tag}`} className="chip tag">
                      {tag}
                    </span>
                  ))}
                  {p.notes && <span className="chip">note</span>}
                  {p.screenshots.length > 0 && (
                    <span className="chip">
                      {p.screenshots.length} img
                    </span>
                  )}
                </td>
              </tr>,
              open && (
                <tr key={`${p.key}-editor`} className="editor-row">
                  <td colSpan={13}>
                    <div className="editor-sticky" style={{ width: Math.max(width - 20, 320) }}>
                      <PositionEditor
                        position={p}
                        strategies={book.filters.strategies || []}
                        mistakeTags={book.mistake_tags}
                        stopMultiple={book.stop_multiple}
                        onSaved={onSaved}
                      />
                    </div>
                  </td>
                </tr>
              ),
            ];
          })}
        </tbody>
      </table>
    </div>
  );
}

// Everything the trader adds to one position. Nothing here is written to the ledger:
// the broker's rows stay exactly as exported, and this sits beside them.
function PositionEditor({ position, strategies, mistakeTags, stopMultiple, onSaved }) {
  const overridden = position.strategy !== position.strategy_guess;
  const [strategy, setStrategy] = useState(overridden ? position.strategy : "");
  const [notes, setNotes] = useState(position.notes || "");
  const [tags, setTags] = useState(position.tags);
  const [custom, setCustom] = useState("");
  const [planned, setPlanned] = useState(
    position.planned_exit === null ? "" : String(position.planned_exit),
  );
  const [entryTime, setEntryTime] = useState(
    position.time_source === "typed" ? position.entry_time || "" : "",
  );
  const [exitTime, setExitTime] = useState(
    position.time_source === "typed" ? position.exit_time || "" : "",
  );
  const [state, setState] = useState({ status: "idle" });

  const toggle = (tag) =>
    setTags((current) =>
      current.includes(tag) ? current.filter((t) => t !== tag) : [...current, tag],
    );

  const save = () => {
    setState({ status: "saving" });
    api
      .saveAnnotation({
        key: position.key,
        strategy: strategy.trim() || null,
        notes,
        tags: custom.trim() ? [...tags, custom.trim()] : tags,
        planned_exit: planned === "" ? null : Number(planned),
        entry_time: entryTime || null,
        exit_time: exitTime || null,
      })
      .then(() => {
        setCustom("");
        setState({ status: "saved" });
        onSaved();
      })
      .catch((error) => setState({ status: "failed", error: error.message }));
  };

  const upload = (event) => {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) return;
    setState({ status: "saving" });
    api
      .uploadScreenshot(position.key, file)
      .then(() => {
        setState({ status: "saved" });
        onSaved();
      })
      .catch((error) => setState({ status: "failed", error: error.message }));
  };

  const remove = (id) =>
    api
      .deleteScreenshot(id)
      .then(onSaved)
      .catch((error) => setState({ status: "failed", error: error.message }));

  const allTags = [...new Set([...mistakeTags, ...tags])];

  return (
    <div className="editor">
      <div className="editor-facts">
        <span>
          {position.symbol} {position.expiry ? `exp ${position.expiry}` : "shares"} ·{" "}
          {position.strategy_guess}
          {position.guess_confident ? "" : " (unconfident guess)"}
        </span>
        {position.max_loss !== null && <span>risk at entry {money(position.max_loss)}</span>}
        {position.close_multiple !== null && (
          <span className={position.close_multiple > stopMultiple ? "bad" : ""}>
            closed at {position.close_multiple.toFixed(2)}x credit (stop {stopMultiple}x)
          </span>
        )}
        {position.vix !== null && <span>VIX {position.vix.toFixed(1)}</span>}
        {position.trend && <span>{position.trend}</span>}
        {position.time_source === "order history" && (
          <span>
            times from order history: {position.entry_time || "?"} / {position.exit_time || "?"}
          </span>
        )}
      </div>

      <table className="legs-table">
        <thead>
          <tr>
            <th className="left">leg</th>
            <th>qty</th>
            <th>open</th>
            <th>close</th>
            <th>cash</th>
          </tr>
        </thead>
        <tbody>
          {position.legs.map((leg, i) => (
            <tr key={i}>
              <td className="left">
                {leg.side} {leg.right ?? "shares"} {strike(leg.strike)}
                {leg.expired ? " (expired)" : ""}
              </td>
              <td>{num(leg.size, 0)}</td>
              <td>{leg.open_price === null ? "" : leg.open_price.toFixed(2)}</td>
              <td>{leg.close_price === null ? "" : leg.close_price.toFixed(2)}</td>
              <td className={signClass(leg.cash)}>{signedMoney(leg.cash, 2)}</td>
            </tr>
          ))}
        </tbody>
      </table>

      <div className="editor-grid">
        <label>
          strategy
          <input
            list="journal-strategies"
            value={strategy}
            placeholder={position.strategy_guess}
            onChange={(event) => setStrategy(event.target.value)}
          />
          <datalist id="journal-strategies">
            {strategies.map((name) => (
              <option key={name} value={name} />
            ))}
          </datalist>
        </label>
        <label>
          entry time (PT)
          <input type="time" value={entryTime} onChange={(e) => setEntryTime(e.target.value)} />
        </label>
        <label>
          exit time (PT)
          <input type="time" value={exitTime} onChange={(e) => setExitTime(e.target.value)} />
        </label>
        <label>
          planned exit (per unit)
          <input
            type="number"
            step="0.01"
            min="0"
            value={planned}
            placeholder={
              position.entry_price > 0
                ? (position.entry_price * stopMultiple).toFixed(2) + " at the stop"
                : ""
            }
            onChange={(event) => setPlanned(event.target.value)}
          />
        </label>
      </div>

      <div>
        <div className="section-label">mistakes and tags</div>
        <div className="chips">
          {allTags.map((tag) => (
            <button
              type="button"
              key={tag}
              className={`chip ${tags.includes(tag) ? "on" : ""} ${position.flags.includes(tag) && !tags.includes(tag) ? "flag" : ""}`}
              title={position.flags.includes(tag) ? "The journal flagged this" : undefined}
              onClick={() => toggle(tag)}
            >
              {tag}
            </button>
          ))}
          <input
            className="chip-input"
            placeholder="add a tag"
            value={custom}
            onChange={(event) => setCustom(event.target.value)}
          />
        </div>
      </div>

      <label className="notes-label">
        notes
        <textarea
          value={notes}
          placeholder="Why you took it, what you saw, what you would do differently."
          onChange={(event) => setNotes(event.target.value)}
        />
      </label>

      <div className="thumbs">
        {position.screenshots.map((id) => (
          <div className="thumb" key={id}>
            <a href={api.screenshotUrl(id)} target="_blank" rel="noreferrer">
              <img src={api.screenshotUrl(id)} alt={`screenshot ${id}`} />
            </a>
            <button type="button" className="chip" onClick={() => remove(id)}>
              remove
            </button>
          </div>
        ))}
        <label className="import-btn secondary">
          <input type="file" accept="image/png,image/jpeg,image/webp,image/gif" onChange={upload} hidden />
          Add screenshot
        </label>
      </div>

      <div className="editor-actions">
        <button type="button" className="btn" onClick={save} disabled={state.status === "saving"}>
          {state.status === "saving" ? "Saving..." : "Save"}
        </button>
        {state.status === "saved" && <span className="import-note good">Saved.</span>}
        {state.status === "failed" && <span className="import-note bad">{state.error}</span>}
      </div>
    </div>
  );
}

// -- Position-level summaries ------------------------------------------------------

function streakText(streak) {
  if (!streak || streak.current === 0) return "none";
  const n = Math.abs(streak.current);
  return streak.current > 0 ? `${n} ${n === 1 ? "win" : "wins"}` : `${n} ${n === 1 ? "loss" : "losses"}`;
}

// Plain-English findings under a panel, written by the server from the numbers above
// them. Each one is generated with its sample size in mind: a comparison needs enough
// trades on both sides, and a small one says so in the sentence.
function Findings({ items }) {
  if (!items || items.length === 0) return null;
  return (
    <div className="findings">
      <div className="findings-label">What this says</div>
      <ul>
        {items.map((text) => (
          <li key={text}>{text}</li>
        ))}
      </ul>
    </div>
  );
}

function BookTiles({ book }) {
  const r = book.r_expectancy;
  const stops = book.stops;
  return (
    <div className="tile-row">
      <Tile
        label="day streak"
        value={streakText(book.day_streaks)}
        tone={signClass(book.day_streaks.current)}
        sub={`longest ${book.day_streaks.longest_win}W / ${book.day_streaks.longest_loss}L days`}
      />
      <Tile
        label="trade streak"
        value={streakText(book.position_streaks)}
        tone={signClass(book.position_streaks.current)}
        sub={`longest ${book.position_streaks.longest_win}W / ${book.position_streaks.longest_loss}L trades`}
      />
      <Tile
        label={`expectancy in R (1R = ${book.stop_multiple - 1}x credit)`}
        value={r ? signedR(r.value) : "n/a"}
        tone={r ? signClass(r.value) : ""}
        sub={
          r
            ? r.low === null
              ? "one cluster, no interval"
              : `${signedR(r.low)} to ${signedR(r.high)}${r.indistinguishable_from_zero ? " — includes zero" : ""}`
            : null
        }
      />
      <Tile
        label="stop-outs"
        value={count(stops.stopped)}
        tone={stops.beyond_stop > 0 ? "bad" : ""}
        sub={
          stops.mean_multiple === null
            ? `none closed at a loss against a ${stops.planned_multiple}x stop`
            : `avg ${stops.mean_multiple.toFixed(2)}x credit, worst ${stops.worst_multiple.toFixed(2)}x; ${stops.beyond_stop} past the ${stops.planned_multiple}x stop`
        }
      />
      <Tile
        label="planned vs actual exit"
        value={
          stops.mean_slippage === null
            ? "n/a"
            : `${stops.mean_slippage >= 0 ? "+" : ""}${stops.mean_slippage.toFixed(2)} / unit`
        }
        tone={stops.mean_slippage > 0 ? "bad" : stops.mean_slippage < 0 ? "good" : ""}
        sub={
          stops.planned_exits
            ? `over ${stops.planned_exits} trades with a planned exit; positive is worse`
            : "type a planned exit into a trade to measure this"
        }
      />
    </div>
  );
}

function WeekdayTable({ rows }) {
  return (
    <BreakdownTable
      title="day opened"
      rows={rows}
      extra={{
        label: "avg credit",
        render: (row) => (row.avg_credit === null ? "" : row.avg_credit.toFixed(2)),
      }}
    />
  );
}

// Risk at entry, one bar per trade in date order: a share of the account once the
// starting balance is set, dollars until then. Flagged bars are more than twice the
// median: sizing that crept.
function SizingChart({ points, median, basis }) {
  const [ref, width] = useMeasuredWidth(720);
  const value = (point) => (basis === "share" ? point.share : point.risk);
  const label = (v) => (basis === "share" ? pct(v, 0) : money(v));
  const shown = points.filter((point) => value(point) !== null);
  if (shown.length === 0) return <div className="empty-state">No sized trades yet.</div>;
  const top = Math.max(...shown.map(value), median || 0) * 1.1 || 1;
  const plotW = width - PAD.left - PAD.right;
  const plotH = SIZING_HEIGHT - PAD.top - PAD.bottom;
  const band = plotW / shown.length;
  const y = (value) => PAD.top + plotH - (value / top) * plotH;
  return (
    <div className="curve-wrap" ref={ref}>
      <svg viewBox={`0 0 ${width} ${SIZING_HEIGHT}`} className="curve sizing-bars">
        {[top, top / 2].map((tick) => (
          <g key={tick}>
            <line x1={PAD.left} x2={width - PAD.right} y1={y(tick)} y2={y(tick)} className="grid-line" />
            <text x={PAD.left - 8} y={y(tick) + 4} className="axis-label" textAnchor="end">
              {label(tick)}
            </text>
          </g>
        ))}
        {median ? (
          <line x1={PAD.left} x2={width - PAD.right} y1={y(median)} y2={y(median)} className="zero-line" />
        ) : null}
        {shown.map((point, i) => (
          <rect
            key={point.key}
            x={PAD.left + i * band + band * 0.15}
            y={y(value(point))}
            width={Math.max(band * 0.7, 1)}
            height={PAD.top + plotH - y(value(point))}
            className={median && value(point) > 2 * median ? "flagged" : ""}
          >
            <title>
              {`${point.day} ${point.key.split("|")[0]}: risk ${money(point.risk)}` +
                (point.share === null ? "" : ` of ${money(point.account)} (${pct(point.share, 1)})`)}
            </title>
          </rect>
        ))}
      </svg>
    </div>
  );
}

function SizingPanel({ sizing, onSaved }) {
  const [balance, setBalance] = useState(
    sizing.starting_balance === null ? "" : String(sizing.starting_balance),
  );
  const [state, setState] = useState("idle");
  const save = () => {
    setState("saving");
    api
      .setBalance(balance === "" ? null : Number(balance))
      .then(() => {
        setState("saved");
        onSaved();
      })
      .catch(() => setState("failed"));
  };
  const share = sizing.basis === "share";
  const show = (v) => (v === null ? "n/a" : share ? pct(v, 1) : money(v));
  return (
    <>
      <div className="tile-row">
        <Tile
          label="median risk per option trade"
          value={show(sizing.median_risk)}
          sub={
            share
              ? "1R, what your stop puts at risk, as a share of the account"
              : "1R in dollars, what your stop puts at risk; set the balance to see it as a share of the account"
          }
        />
        <Tile
          label="after a winning day"
          value={show(sizing.after_win_risk)}
          sub={`average over ${sizing.after_win_n} trades`}
        />
        <Tile
          label="after a losing day"
          value={show(sizing.after_loss_risk)}
          sub={`average over ${sizing.after_loss_n} trades`}
        />
        <div className="tile">
          <div className="metric-label">balance before first statement</div>
          <div className="balance-row">
            <input
              type="number"
              step="1"
              min="0"
              value={balance}
              placeholder="e.g. 5000"
              onChange={(event) => setBalance(event.target.value)}
            />
            <button type="button" className="btn" onClick={save} disabled={state === "saving"}>
              Set
            </button>
          </div>
          <div className="tile-sub">
            deposits in statements {signedMoney(sizing.net_deposits)}
            {state === "failed" ? " · could not save" : ""}
          </div>
        </div>
      </div>
      <SizingChart points={sizing.points} median={sizing.median_risk} basis={sizing.basis} />
    </>
  );
}

function RegimeButton({ onFetched }) {
  const [state, setState] = useState({ status: "idle" });
  const fetch = () => {
    setState({ status: "working" });
    api
      .fetchRegime()
      .then((result) => {
        setState({ status: "done", detail: result.detail });
        onFetched();
      })
      .catch((error) => setState({ status: "failed", detail: error.message }));
  };
  return (
    <span className="import-row">
      <button type="button" className="btn" onClick={fetch} disabled={state.status === "working"}>
        {state.status === "working" ? "Fetching..." : "Fetch VIX history"}
      </button>
      {state.detail && (
        <span className={`import-note ${state.status === "failed" ? "bad" : "good"}`}>
          {state.detail}
        </span>
      )}
    </span>
  );
}

export default function Journal() {
  const [filters, setFilters] = useState({ symbol: "", strategy: "", tag: "", dte: "" });
  const [imported, setImported] = useState(0);
  const [selected, setSelected] = useState(null);
  const [day, setDay] = useState(null);
  // Capped view: any trade that risked more than the account's median 1R is counted as
  // if it had risked the median, and smaller trades keep their real result. It shows
  // what oversizing cost without inventing larger trades. Kept in the page only; a
  // reload shows the real record again.
  const [cap, setCap] = useState(false);
  const { data, error, loading, reload } = useAsync(
    () => api.journal({ ...filters, cap }),
    [filters, imported, cap],
  );

  // Only the first load shows the placeholder. A save reloads in place, so an editor
  // that is open stays open and the page does not jump back to the top.
  if (loading && !data) return <div className="loading">Reading the ledger...</div>;
  if (error && !data) return <ErrorBox error={error} />;
  if (!data) return null;

  const expectancy = data.expectancy;
  const book = data.book;
  const setFilter = (name) => (event) => setFilters({ ...filters, [name]: event.target.value });
  const shown = day
    ? book.positions.filter((p) => p.closed_at === day)
    : book.positions;
  const hasVix = book.positions.some((p) => p.vix !== null);
  const view = data.view;

  const select = (name, options, label = name) => (
    <label>
      {label}{" "}
      <select value={filters[name]} onChange={setFilter(name)}>
        <option value="">all</option>
        {options.map((option) => (
          <option key={option} value={option}>
            {option}
          </option>
        ))}
      </select>
    </label>
  );

  return (
    <>
      <Panel title="Import trades">
        <ImportStatement onImported={() => setImported((value) => value + 1)} />
      </Panel>

      {/* Above the numbers, deliberately. See note 1 at the top. */}
      {!data.reportable && (
        <div className="sample-banner">
          <strong>Real fills, small sample.</strong> {count(data.trades)} closed{" "}
          {data.trades === 1 ? "day" : "days"} of trading across {count(data.settlement_dates)}{" "}
          {data.settlement_dates === 1 ? "session" : "sessions"}, below the minimum this tool will
          draw a conclusion from. Everything here happened, and none of it is yet enough to tell an
          edge from noise.
        </div>
      )}

      <Panel
        title="Performance"
        right={
          <div className="controls">
            {select("symbol", book.filters.symbols || [])}
            {select("strategy", book.filters.strategies || [])}
            {select("tag", [...(book.filters.tags || []), "untagged"])}
            {select("dte", book.filters.dte || [], "DTE")}
            <button
              type="button"
              className={`chip ${cap ? "on" : ""}`}
              aria-pressed={cap}
              title="Count any trade that risked more than your median as if it risked the median"
              onClick={() => setCap(!cap)}
            >
              cap size at median
            </button>
          </div>
        }
      >
        {view?.capped && (
          <div className="view-banner">
            <strong>Capped at your median size.</strong> {count(view.capped_trades)}{" "}
            {view.capped_trades === 1 ? "trade" : "trades"} that risked more than{" "}
            {money(view.cap_risk)} {view.capped_trades === 1 ? "is" : "are"} counted as if
            they had risked {money(view.cap_risk)}; everything smaller keeps its real result.
            The trade table still shows real results. Actual total{" "}
            {signedMoney(view.actual_total)}.{" "}
            <button type="button" className="chip" onClick={() => setCap(false)}>
              show real
            </button>
          </div>
        )}
        <div className="tile-row">
          {/* "closed trades" counts (symbol, closing day) entries, the audited grain. */}
          <Tile
            label="closed trades"
            value={count(data.trades)}
            sub={`${count(data.settlement_dates)} sessions traded · ${count(book.positions.filter((p) => !p.is_open).length)} positions`}
          />
          <Tile label="win rate" value={<IntervalText interval={data.win_rate} />} />
          {/* Per trade, not per day: total profit over the (symbol, day) count. */}
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
          <Tile label="avg win" value={signedMoney(data.avg_win, 2)} tone="good" />
          <Tile label="avg loss" value={signedMoney(data.avg_loss, 2)} tone="bad" />
          <Tile
            label="profit factor"
            value={data.profit_factor === null ? "undefined" : num(data.profit_factor, 2)}
            sub={data.profit_factor === null ? "nothing lost yet" : null}
          />
          <Tile label="total" value={signedMoney(data.total_profit)} tone={signClass(data.total_profit)} />
          <Tile label="max drawdown" value={money(data.max_drawdown)} />
        </div>
        <div style={{ height: 10 }} />
        <BookTiles book={book} />
        <Findings items={book.insights?.performance} />
      </Panel>

      <Panel title="Cumulative profit by closing date">
        <EquityCurve days={data.days} />
        <Findings items={book.insights?.curve} />
      </Panel>

      <Panel title="Calendar" right={<span className="muted">click a day to list its trades</span>}>
        <CalendarPnl days={data.days} selected={day} onSelect={setDay} />
      </Panel>

      <Panel
        title={day ? `Trades closed ${day}` : "Trades"}
        right={
          day ? (
            <button type="button" className="chip" onClick={() => setDay(null)}>
              show all
            </button>
          ) : (
            <span className="muted">click a trade to tag it, add notes or a screenshot</span>
          )
        }
      >
        <TradeTable
          positions={shown}
          selected={selected}
          onSelect={setSelected}
          book={book}
          onSaved={reload}
        />
        <Findings items={book.insights?.trades} />
      </Panel>

      <Panel title="Breakdowns">
        <div className="breakdown-grid">
          <BreakdownTable title="strategy" rows={book.by_strategy} />
          <BreakdownTable title="symbol" rows={data.by_symbol} />
          <BreakdownTable title="DTE at entry" rows={book.by_dte_class} />
          <WeekdayTable rows={book.by_weekday} />
          <BreakdownTable title="tag" rows={book.by_tag} />
          <BreakdownTable title="instrument" rows={data.by_strategy} />
        </div>
        <div className="caveat">
          Rows are dimmed where the group has too few independent (symbol, closing day)
          clusters to support a reading. Day opened splits 0DTE from everything longer.
        </div>
        <Findings items={book.insights?.breakdowns} />
      </Panel>

      <Panel title="Time of day">
        {book.timed === 0 ? (
          <div className="empty-state">
            No trade has a time yet. The Robinhood CSV has dates only. Run{" "}
            <code>optscan import-fill-times</code> in a terminal to pull them from Robinhood's
            order history (you log in there), or type them into a trade.
          </div>
        ) : (
          <div className="breakdown-grid">
            <BreakdownTable title="entry time" rows={book.by_entry_time} />
            <BreakdownTable title="exit" rows={book.by_exit_time} />
          </div>
        )}
        {book.timed === 0 && book.by_exit_time.length > 0 && (
          <div className="breakdown-grid">
            <BreakdownTable title="exit" rows={book.by_exit_time} />
          </div>
        )}
        <Findings items={book.insights?.time} />
      </Panel>

      <Panel title="Market regime" right={hasVix ? null : <RegimeButton onFetched={reload} />}>
        <div className="breakdown-grid">
          <BreakdownTable title="VIX on the day opened" rows={book.by_vix} />
          <BreakdownTable title="SPY 20-session trend" rows={book.by_trend} />
        </div>
        {!hasVix && <div className="caveat">VIX needs one fetch; SPY trend is read from stored prices.</div>}
        <Findings items={book.insights?.regime} />
      </Panel>

      <Panel title="Position sizing">
        <SizingPanel sizing={book.sizing} onSaved={reload} />
        <Findings items={book.insights?.sizing} />
      </Panel>

      <Notes items={[...book.notes, ...data.notes]} />
    </>
  );
}
