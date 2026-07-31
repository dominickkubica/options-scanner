import { useEffect, useMemo, useState } from "react";
import { api } from "../api.js";
import { PayoffChart } from "../components/charts.jsx";
import { ErrorBox, Note, Panel, Provenance, Stat } from "../components/common.jsx";
import { money, num, vol } from "../format.js";

// Build a position and see what it pays.
//
// The legs the browser sends carry no prices. It says sell this strike, buy that one,
// and the server prices both from the same snapshot everything else on the page came
// from. That is why this view has no notion of a mid: it has nothing to price with and
// should not pretend to.
//
// A position arriving from the opportunities table lands here through the same path,
// so a candidate and a hand built spread produce the same diagram from the same code.

const UNBOUNDED = "unbounded";

//: Expiries inside a week are excluded from the default. This view has to post a
//: concrete expiry, so unlike the chain it cannot leave the choice to the server, and
//: opening on a zero day chain would draw a payoff with no T plus zero curve to speak
//: of. A week is the same boundary the term structure code uses.
const FRONT_WEEK_DTE = 7;

function defaultExpiry(expiries) {
  if (expiries.length === 0) return "";
  const past = expiries.find((item) => item.dte >= FRONT_WEEK_DTE);
  return (past || expiries[expiries.length - 1]).expiry;
}

export default function Payoff({ symbol, expiries, initialPosition, onConsumed }) {
  const [expiry, setExpiry] = useState(initialPosition?.expiry || defaultExpiry(expiries));
  const [legs, setLegs] = useState(initialPosition ? toLegs(initialPosition) : []);
  const [chain, setChain] = useState(null);
  const [chainError, setChainError] = useState(null);
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  // How far either side of spot to draw. A five point wide spread inside a 20 percent
  // band is three pixels of interesting shape and a lot of flat, so the width is a
  // control rather than a constant.
  const [range, setRange] = useState(0.2);

  // A position handed over from the opportunities table replaces whatever was being
  // built, once, and then the handover is cleared so editing is not fought over. It
  // also draws itself: the user clicked "show payoff", which is already the request,
  // and making them click again in a second place would be a pointless step.
  useEffect(() => {
    if (!initialPosition) return;
    const handed = toLegs(initialPosition);
    setExpiry(initialPosition.expiry);
    setLegs(handed);
    onConsumed();
    draw(initialPosition.expiry, handed);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialPosition]);

  useEffect(() => {
    if (!expiry) return;
    setChain(null);
    setChainError(null);
    api
      .chain(symbol, expiry)
      .then(setChain)
      .catch((err) => setChainError(err.message));
  }, [symbol, expiry]);

  const strikes = useMemo(() => {
    if (!chain) return { C: [], P: [] };
    const tradeable = (rows) =>
      rows.filter((row) => row.mid !== null).map((row) => row.strike);
    return { C: tradeable(chain.calls), P: tradeable(chain.puts) };
  }, [chain]);

  function draw(expiryValue, legsValue, rangeValue = range) {
    if (!expiryValue || legsValue.length === 0) return;
    setBusy(true);
    setError(null);
    api
      .payoff({ symbol, expiry: expiryValue, legs: legsValue, price_range: rangeValue })
      .then((data) => {
        setResult(data);
        setBusy(false);
      })
      .catch((err) => {
        setError(err.message);
        setResult(null);
        setBusy(false);
      });
  }

  const submit = () => draw(expiry, legs);

  const addLeg = (action, right) => {
    const available = strikes[right];
    if (available.length === 0) return;
    const nearSpot = available.reduce((best, value) =>
      Math.abs(value - (chain?.spot ?? 0)) < Math.abs(best - (chain?.spot ?? 0)) ? value : best,
    );
    setLegs((prev) => [...prev, { action, right, strike: nearSpot, quantity: 1 }]);
  };

  return (
    <>
      <Panel title="Position">
        <div className="controls">
          <label>
            expiry{" "}
            <select
              value={expiry}
              onChange={(event) => {
                setExpiry(event.target.value);
                setLegs([]);
                setResult(null);
              }}
            >
              {expiries.map((item) => (
                <option key={item.expiry} value={item.expiry}>
                  {item.expiry} ({item.dte}d)
                </option>
              ))}
            </select>
          </label>
          <button type="button" className="btn" onClick={() => addLeg("sell", "P")}>
            sell put
          </button>
          <button type="button" className="btn" onClick={() => addLeg("buy", "P")}>
            buy put
          </button>
          <button type="button" className="btn" onClick={() => addLeg("sell", "C")}>
            sell call
          </button>
          <button type="button" className="btn" onClick={() => addLeg("buy", "C")}>
            buy call
          </button>
          <label>
            width{" "}
            <select
              value={range}
              onChange={(event) => {
                const next = Number(event.target.value);
                setRange(next);
                if (result) draw(expiry, legs, next);
              }}
            >
              {[0.05, 0.1, 0.2, 0.35].map((value) => (
                <option key={value} value={value}>
                  {Math.round(value * 100)}% either side of spot
                </option>
              ))}
            </select>
          </label>
          <button
            type="button"
            className="btn primary"
            disabled={legs.length === 0 || busy}
            onClick={submit}
          >
            {busy ? "pricing..." : "draw payoff"}
          </button>
        </div>

        <ErrorBox error={chainError} />

        {legs.length === 0 ? (
          <div className="empty-state">
            Add a leg, or open one from a row in the candidates table. Only strikes with
            a two sided market in this snapshot are offered, because a leg with no mid
            has no price and therefore no payoff.
          </div>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th className="left">action</th>
                  <th className="left">right</th>
                  <th>strike</th>
                  <th>qty</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {legs.map((leg, index) => (
                  <tr key={index}>
                    <td className="left">{leg.action}</td>
                    <td className="left">{leg.right === "C" ? "call" : "put"}</td>
                    <td>
                      <select
                        value={leg.strike}
                        onChange={(event) =>
                          setLegs((prev) =>
                            prev.map((item, position) =>
                              position === index
                                ? { ...item, strike: Number(event.target.value) }
                                : item,
                            ),
                          )
                        }
                      >
                        {strikes[leg.right].map((value) => (
                          <option key={value} value={value}>
                            {value}
                          </option>
                        ))}
                      </select>
                    </td>
                    <td>
                      <input
                        type="number"
                        min="1"
                        value={leg.quantity}
                        style={{ width: 56 }}
                        onChange={(event) =>
                          setLegs((prev) =>
                            prev.map((item, position) =>
                              position === index
                                ? { ...item, quantity: Math.max(1, Number(event.target.value)) }
                                : item,
                            ),
                          )
                        }
                      />
                    </td>
                    <td>
                      <button
                        type="button"
                        className="btn danger"
                        onClick={() =>
                          setLegs((prev) => prev.filter((_, position) => position !== index))
                        }
                      >
                        remove
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>

      <ErrorBox error={error} />

      {result && (
        <Panel
          title={`Payoff at expiry and at T plus zero (${result.dte} DTE)`}
          right={<Provenance provenance={result.provenance} />}
        >
          {result.note && <Note>{result.note}</Note>}
          <div className="stats" style={{ marginBottom: 10 }}>
            <Stat label="net credit" value={money(result.net_credit, 2)} />
            <Stat
              label="max profit"
              value={result.max_profit === null ? UNBOUNDED : money(result.max_profit, 2)}
            />
            <Stat
              label="max loss"
              value={result.max_loss === null ? UNBOUNDED : money(result.max_loss, 2)}
              hint={
                result.max_loss === null
                  ? "A short call has no maximum loss. A short put does, because a stock cannot fall below zero."
                  : undefined
              }
            />
            <Stat
              label="breakevens"
              value={
                result.breakevens.length === 0
                  ? "none"
                  : result.breakevens.map((value) => num(value)).join(", ")
              }
            />
          </div>

          <PayoffChart payoff={result} />

          <div className="table-wrap" style={{ marginTop: 12 }}>
            <table>
              <thead>
                <tr>
                  <th className="left">leg</th>
                  <th>strike</th>
                  <th>qty</th>
                  <th>mid</th>
                  <th>iv</th>
                  <th>delta</th>
                </tr>
              </thead>
              <tbody>
                {result.legs.map((leg, index) => (
                  <tr key={index}>
                    <td className="left">
                      {leg.action} {leg.right === "C" ? "call" : "put"}
                    </td>
                    <td>{num(leg.strike, 0)}</td>
                    <td>{leg.quantity}</td>
                    <td className={leg.mid === null ? "missing" : ""}>{num(leg.mid)}</td>
                    <td className={leg.iv === null ? "missing" : ""}>{vol(leg.iv)}</td>
                    <td className={leg.delta === null ? "missing" : ""}>{num(leg.delta, 3)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="chart-note">
            Prices are the stored snapshot&apos;s mids, not fills. The T plus zero curve
            holds each leg&apos;s volatility constant, which flatters a short premium
            position: a real move down usually comes with a rise in volatility.
          </div>
        </Panel>
      )}
    </>
  );
}

function toLegs(opportunity) {
  return opportunity.legs.map((leg) => ({
    action: leg.action,
    right: leg.right,
    strike: leg.strike,
    quantity: leg.quantity,
  }));
}
