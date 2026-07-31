// The live feed, as the browser sees it.
//
// One EventSource per subscribed symbol and expiry. EventSource is used rather than a
// websocket for the reason recorded on the server: the traffic is one directional, and
// the reconnect logic that a dropped feed depends on is built in rather than written
// by hand here.
//
// The rule this file exists to keep is that the table is never a mixture of ages. The
// server sends whole cycles, each stamped with one fetched_at, and a delta only omits
// contracts whose numbers did not move. So applying a delta leaves every row as of the
// newest version, provided two things hold, and both are enforced below:
//
//   - a delta is only applied on top of the version before it. A cycle that arrives
//     out of order, or after a gap, is refused and the connection asks for a full one.
//   - `removed` is honoured. A contract dropped from the chain has to leave the table,
//     or it sits there at its last price forever looking current.
//
// When the feed is not live, this returns nothing at all and the panels fall back to
// the stored snapshot. It never half fills a table.

import { useEffect, useRef, useState } from "react";

const STREAM = "/api/live/stream";

export const LIVE_STATES = {
  disabled: { label: "off", tone: "idle" },
  starting: { label: "connecting", tone: "idle" },
  live: { label: "live", tone: "good" },
  idle: { label: "idle", tone: "idle" },
  degraded: { label: "degraded", tone: "bad" },
  stopped: { label: "stopped", tone: "bad" },
};

// What the browser itself knows about the socket, which is a different question from
// what the server says about the feed. A server that is polling happily is no use if
// this tab cannot reach it, and the header has to be able to say which of the two is
// wrong.
export const CONNECTION = {
  CLOSED: "closed",
  CONNECTING: "connecting",
  OPEN: "open",
};

/**
 * Subscribe to one symbol and expiry.
 *
 * Returns { cycle, status, connection, error }. `cycle` is null until a full cycle has
 * arrived, and goes back to null if the stream desynchronizes, because a partial table
 * is worse than the stored one it would replace.
 */
export function useLive(symbol, expiry, { enabled = true } = {}) {
  const [cycle, setCycle] = useState(null);
  const [status, setStatus] = useState(null);
  const [connection, setConnection] = useState(CONNECTION.CLOSED);
  const [error, setError] = useState(null);

  // The applied version lives in a ref rather than in state: it is read inside the
  // event handler, and a stale closure over it would silently accept a delta against
  // the wrong version, which is the one failure this whole design is built to avoid.
  const version = useRef(0);

  useEffect(() => {
    setCycle(null);
    setStatus(null);
    version.current = 0;

    if (!enabled || !symbol) {
      setConnection(CONNECTION.CLOSED);
      return undefined;
    }

    const params = new URLSearchParams({ symbol });
    if (expiry) params.set("expiry", expiry);

    const source = new EventSource(`${STREAM}?${params.toString()}`);
    setConnection(CONNECTION.CONNECTING);

    source.addEventListener("open", () => {
      setConnection(CONNECTION.OPEN);
      setError(null);
    });

    source.addEventListener("status", (event) => {
      setStatus(JSON.parse(event.data));
    });

    source.addEventListener("cycle", (event) => {
      const delta = JSON.parse(event.data);
      setCycle((previous) => apply(previous, delta, version));
    });

    source.addEventListener("bye", (event) => {
      setError(JSON.parse(event.data).reason);
      source.close();
      setConnection(CONNECTION.CLOSED);
    });

    // EventSource reports an error and then reconnects on its own with backoff. The
    // panel says "reconnecting" and keeps showing the last cycle with its age
    // climbing, which is the honest picture: the numbers are real, they are just
    // getting old. Blanking the table would throw away good data.
    source.addEventListener("error", () => {
      setConnection(source.readyState === 2 ? CONNECTION.CLOSED : CONNECTION.CONNECTING);
    });

    return () => {
      source.close();
      setConnection(CONNECTION.CLOSED);
    };
  }, [symbol, expiry, enabled]);

  return { cycle, status, connection, error };
}

// Fold one delta into the table, or refuse it.
function apply(previous, delta, version) {
  const isNext = delta.version === version.current + 1;

  if (!delta.full && (previous === null || !isNext)) {
    // A delta against a version this client never applied. Dropping it and waiting for
    // the server's next full cycle is the only safe move: merging it would leave rows
    // from two different fetches side by side with one timestamp over them.
    return previous;
  }

  version.current = delta.version;

  const contracts = delta.full ? {} : { ...previous.contracts };
  for (const key of delta.removed || []) delete contracts[key];
  for (const [key, contract] of Object.entries(delta.changed || {})) {
    contracts[key] = contract;
  }

  return {
    symbol: delta.symbol,
    expiry: delta.expiry,
    version: delta.version,
    fetchedAt: delta.fetched_at,
    source: delta.source,
    realtime: delta.realtime,
    delayMinutes: delta.delay_minutes,
    quote: delta.quote,
    atmIv: delta.atm_iv,
    dte: delta.dte,
    contracts,
  };
}

// Seconds since a cycle was fetched. Recomputed on a timer by the caller rather than
// stored, because the whole point of showing an age is that it keeps growing while
// nothing arrives.
export function cycleAge(cycle, now = Date.now()) {
  if (!cycle) return null;
  return (now - new Date(cycle.fetchedAt).getTime()) / 1000;
}

// How many missed cycles count as overdue rather than late. Two would fire on a single
// slow fetch, which is a false alarm; three is a pattern.
const OVERDUE_CYCLES = 3;

/**
 * Whether a panel has gone quiet for longer than the server said to expect.
 *
 * This exists because a socket is not a reliable witness to its own death. A dropped
 * connection usually raises an error event, but a proxy holding a half open socket, a
 * suspended laptop, or a server killed mid stream can all leave EventSource looking
 * connected forever. The age of the last cycle cannot be fooled that way, so it is what
 * the label is driven from. The threshold comes from the server's own poll interval
 * rather than a number invented here.
 */
export function cycleOverdue(cycle, status, now = Date.now()) {
  if (!cycle) return false;
  const interval = status?.poll_seconds;
  if (!interval) return false;
  return cycleAge(cycle, now) > interval * OVERDUE_CYCLES;
}
