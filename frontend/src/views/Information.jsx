import { Note, Panel, Stat } from "../components/common.jsx";
import { num, pct, vol } from "../format.js";

// What is known about one symbol, and how much of it is trustworthy.
//
// ## Why the gaps are as prominent as the facts
//
// Every panel in this application is built on a stored capture, and the useful question
// about a symbol is usually not "what is its implied volatility" but "do we actually
// know". So an absent earnings date is rendered differently depending on *why* it is
// absent: no earnings scheduled is a fact, and a corporate calendar that could not be
// reached is not. Collapsing those two into a blank cell is how somebody sells a put
// through an earnings print.
//
// The same applies to IV rank, which carries its own note explaining what stopped it
// being computed, and to a partial capture, where some expiries are missing and every
// number derived from the surface is drawn from less than the whole board.

function Row({ label, value, tone, hint }) {
  return (
    <div className="info-row">
      <span className="info-label">{label}</span>
      <span className={`info-value ${tone || ""}`}>{value}</span>
      {hint && <span className="info-hint">{hint}</span>}
    </div>
  );
}

export default function Information({ symbol, summary }) {
  if (!summary) {
    return (
      <Panel title={`About ${symbol}`}>
        <div className="empty-state">
          No option chain has been captured for {symbol}, so there is nothing to
          describe beyond its price history. Pin it and the next snapshot run starts
          capturing.
        </div>
      </Panel>
    );
  }

  const rank = summary.iv_rank;
  const front = summary.expiries?.[0];
  const totalContracts = (summary.expiries || []).reduce(
    (sum, item) => sum + (item.contracts || 0),
    0,
  );

  return (
    <>
      <Panel title={`About ${symbol}`}>
        <div className="stats">
          <Stat label="spot" value={num(summary.spot)} />
          <Stat label="session" value={summary.session_date} />
          <Stat label="expiries captured" value={(summary.expiries || []).length} />
          <Stat label="contracts" value={totalContracts || "-"} />
          {front && <Stat label="front expiry" value={`${front.expiry} (${front.dte}d)`} />}
        </div>
        {summary.partial && (
          <Note>
            At least one expiry failed to capture in this snapshot, so anything derived
            from the surface — the term structure, the skew, the rank below — is drawn
            from less than the whole board.
          </Note>
        )}
      </Panel>

      <Panel title="Volatility">
        <div className="info-grid">
          <Row
            label="IV rank"
            value={rank ? pct(rank.rank) : "not available"}
            tone={rank ? "" : "muted"}
          />
          {rank && (
            <>
              <Row label="percentile" value={pct(rank.percentile)} />
              <Row
                label="observations"
                value={rank.observations}
                hint={rank.confidence ? `confidence ${rank.confidence}` : null}
              />
              <Row label="current" value={rank.current ? vol(rank.current) : "-"} />
            </>
          )}
          <Row
            label="term structure"
            value={
              summary.term_slope === null || summary.term_slope === undefined
                ? "not measurable"
                : `${(summary.term_slope * 100).toFixed(1)} vol points past 7 DTE`
            }
            tone={summary.backwardated ? "neg" : ""}
            hint={
              summary.backwardated
                ? "backwardated: something is expected soon"
                : "normal upward slope"
            }
          />
        </div>
        {/* The reason a rank is missing is more useful than the blank it leaves. */}
        {summary.iv_rank_note && <Note>{summary.iv_rank_note}</Note>}
      </Panel>

      <Panel title="Calendar">
        {summary.events_checked ? (
          <div className="info-grid">
            <Row
              label="earnings"
              value={summary.earnings_date || "none scheduled"}
              tone={summary.earnings_date ? "neg" : ""}
            />
            <Row
              label="ex-dividend"
              value={summary.ex_dividend_date || "none scheduled"}
            />
          </div>
        ) : (
          /* Not the same as "no earnings". An unchecked calendar is ignorance, and
             reading it as clear is how a short option gets held through a print. */
          <Note>
            The corporate calendar could not be reached
            {summary.events_note ? `: ${summary.events_note}` : "."} The absence of an
            earnings date here says nothing, and must not be read as clear.
          </Note>
        )}
      </Panel>

      <Panel title="Provenance">
        <div className="info-grid">
          <Row label="provider" value={summary.provenance?.source || "-"} />
          <Row label="captured" value={summary.provenance?.captured_at || "-"} />
          <Row
            label="quotes"
            value={summary.provenance?.realtime ? "real time" : "delayed"}
            tone={summary.provenance?.realtime ? "" : "muted"}
          />
        </div>
        <div className="chart-note">
          Everything above is read from the last stored capture, not from a live quote.
          The session and capture time are shown so a stale number cannot be mistaken
          for a current one.
        </div>
      </Panel>
    </>
  );
}
