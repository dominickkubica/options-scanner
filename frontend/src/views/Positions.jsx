import { api } from "../api.js";
import { ErrorBox, Note, Notes, Panel, Stat, useAsync } from "../components/common.jsx";
import { money, num, pct, vol } from "../format.js";

// What is already sold, and whether anything wants attention.
//
// Read only, and the reason is worth stating where somebody will look for the missing
// "add position" button: a fill price is the one number in this tool nothing can check,
// and a mistyped one silently poisons every figure on this page. It is entered through
// `optscan position add`, where it is obvious a person typed it.
//
// Alerts are not sent from this page either. A GET that delivered notifications would
// fire on every refresh, and a notifier that repeats itself gets muted.

// Severity to a class. Only three levels are worth distinguishing visually: something
// that must be handled, something to look at, and something merely worth knowing.
function toneFor(severity) {
  if (severity >= 4) return "bad";
  if (severity >= 3) return "warn";
  return "idle";
}

export default function Positions() {
  const book = useAsync(() => api.positions(), []);
  const data = book.data;

  if (book.loading) return <div className="loading">Marking positions...</div>;
  if (book.error) return <ErrorBox error={book.error} />;
  if (!data) return null;

  if (data.positions.length === 0) {
    return (
      <Panel title="Positions">
        <Note>
          Nothing open. Record a position with{" "}
          <code>optscan position add SPY --expiry 2026-09-18 --leg sell:P:700:5.20</code>.
          The fill price is required: it is what profit and loss is measured against, and
          it is the one number this tool cannot reconstruct.
        </Note>
      </Panel>
    );
  }

  return (
    <>
      <Panel title={`Portfolio, as of ${data.asof}`}>
        <div className="stats">
          <Stat label="open positions" value={data.positions.length} />
          <Stat label="unrealized" value={money(data.unrealized, 2)} />
          <Stat
            label="delta"
            value={num(data.delta, 1)}
            hint="shares, only meaningful within one symbol"
          />
          <Stat label="theta" value={money(data.theta, 2)} hint="per calendar day" />
          <Stat label="vega" value={money(data.vega, 2)} hint="per volatility point" />
          <Stat
            label={`beta delta vs ${data.reference}`}
            value={money(data.beta_weighted_delta, 0)}
            hint="dollars of reference exposure"
          />
        </div>

        {data.unmarked > 0 && (
          <Note>
            {data.unmarked} of {data.positions.length} positions could not be marked, so
            the portfolio greeks are withheld. The profit total covers only what priced.
          </Note>
        )}
        {data.beta_weighted_delta === null && (
          <Note>
            Beta weighted delta is not published. It needs enough overlapping price
            history to estimate a slope for every symbol held, and a default of 1.0 would
            be a measured looking number for an unmeasured thing.
          </Note>
        )}
        <Notes items={data.notes} />
        <div className="chart-note">{data.disclaimer}</div>
      </Panel>

      {data.positions.map((position) => (
        <PositionPanel key={position.id} position={position} />
      ))}
    </>
  );
}

function PositionPanel({ position }) {
  const worst = position.triggers[0];

  return (
    <Panel
      title={`${position.symbol} ${position.expiry} (${position.dte} DTE)`}
      right={
        worst ? (
          <span className={`connection ${toneFor(worst.severity)}`}>
            <span className="dot" />
            <span className="label">{worst.kind.replace(/_/g, " ")}</span>
          </span>
        ) : (
          <span className="provenance">nothing triggered</span>
        )
      }
    >
      <div className="stats">
        <Stat label="entry credit" value={money(position.entry_credit, 2)} />
        <Stat
          label="unrealized"
          value={money(position.unrealized, 2)}
          hint={position.marks_complete ? undefined : "one or more legs could not be marked"}
        />
        <Stat
          label="of max profit"
          value={pct(position.profit_fraction, 0)}
          hint={position.profit_fraction === null ? "opened for a debit" : undefined}
        />
        <Stat label="delta" value={num(position.delta, 1)} />
        <Stat label="theta" value={money(position.theta, 2)} hint="per day" />
        <Stat label="vega" value={money(position.vega, 2)} />
      </div>

      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th className="left">leg</th>
              <th>qty</th>
              <th>fill</th>
              <th>mark</th>
              <th>iv</th>
              <th>delta</th>
              <th>theta</th>
              <th>vega</th>
            </tr>
          </thead>
          <tbody>
            {position.legs.map((leg, index) => (
              <tr key={index}>
                <td className="left">
                  {leg.action === "sell" ? "-" : "+"}
                  {num(leg.strike, 0)}
                  {leg.right}
                </td>
                <td>{leg.quantity}</td>
                <td>{num(leg.fill_price)}</td>
                <td className={leg.mark === null ? "missing" : ""}>{num(leg.mark)}</td>
                <td className={leg.iv === null ? "missing" : ""}>{vol(leg.iv)}</td>
                <td>{num(leg.delta, 3)}</td>
                <td>{num(leg.theta, 3)}</td>
                <td>{num(leg.vega, 3)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {position.triggers.length > 0 && (
        <div style={{ marginTop: 10 }}>
          {position.triggers.map((trigger) => (
            <Note key={trigger.kind}>
              <strong>{trigger.kind.replace(/_/g, " ")}</strong> {trigger.message}
            </Note>
          ))}
        </div>
      )}

      <Notes items={position.notes} />

      <div className="chart-note">
        Marked at what it would cost to close, not at the mid. A short book marked at the
        mid is ahead by half the spread on every leg, which is most of the last of the
        credit and exactly the range where a profit target fires.
      </div>
    </Panel>
  );
}
