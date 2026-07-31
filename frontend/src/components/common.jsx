import { useCallback, useEffect, useState } from "react";
import { age, duration } from "../format.js";
import { CONNECTION, LIVE_STATES, cycleAge, cycleOverdue } from "../live.js";

// One place for loading, error, and "the server said why". Every panel uses it, so
// every panel fails the same way: with the API's own sentence, not a spinner that
// never stops.
export function useAsync(loader, deps, { enabled = true } = {}) {
  const [state, setState] = useState({ data: null, error: null, loading: enabled });
  const [nonce, setNonce] = useState(0);

  const reload = useCallback(() => setNonce((value) => value + 1), []);

  useEffect(() => {
    if (!enabled) {
      setState({ data: null, error: null, loading: false });
      return undefined;
    }
    let live = true;
    setState((prev) => ({ ...prev, loading: true, error: null }));
    loader()
      .then((data) => live && setState({ data, error: null, loading: false }))
      .catch((error) => live && setState({ data: null, error: error.message, loading: false }));
    return () => {
      live = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, nonce, enabled]);

  return { ...state, reload };
}

export function Provenance({ provenance, label = "captured" }) {
  if (!provenance) return null;
  const when = new Date(provenance.fetched_at);
  return (
    <span className="provenance">
      <span className={`badge ${provenance.stale ? "stale" : "fresh"}`}>
        {provenance.stale ? "stale" : "fresh"}
      </span>{" "}
      {label} {when.toLocaleString()} ({age(provenance.age_seconds)}) via {provenance.source}
    </span>
  );
}

// Where a live panel's numbers came from and how old they are.
//
// Deliberately a different component from Provenance rather than a flag on it. A
// stored panel is captured once and ages in hours; a live panel is refreshed on a
// cycle and ages in seconds, and the two want different words. Sharing one component
// would mean one of them reading wrongly.
export function LiveProvenance({ cycle, connection, status }) {
  const [now, setNow] = useState(Date.now());

  // The age is the point. A feed that has stopped delivering looks identical to a
  // quiet market unless the number next to it keeps climbing.
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);

  if (!cycle) return null;

  const seconds = cycleAge(cycle, now);
  // Overdue is checked first and on its own. A socket is not a reliable witness to its
  // own death, so the label is driven by how long it has actually been since anything
  // arrived rather than by what the connection claims about itself.
  const overdue = cycleOverdue(cycle, status, now);
  const reconnecting = connection === CONNECTION.CONNECTING;
  const stalled = overdue || reconnecting;
  const delayed = !cycle.realtime;

  return (
    <span className="provenance">
      <span className={`badge ${stalled ? "stale" : "fresh"}`}>
        {overdue ? "not updating" : reconnecting ? "reconnecting" : "live"}
      </span>{" "}
      updated {age(seconds)} (v{cycle.version}) via {cycle.source}
      {delayed && (
        <>
          {" "}
          &middot;{" "}
          {cycle.delayMinutes
            ? `${cycle.delayMinutes} minutes delayed`
            : "delayed by an unstated amount"}
        </>
      )}
      {overdue && <> &middot; nothing has arrived for {duration(seconds)}</>}
      {status?.detail && reconnecting && <> &middot; {status.detail}</>}
    </span>
  );
}

// The one line in the chrome that says whether anything is arriving at all.
export function ConnectionBadge({ status, connection, cycle }) {
  const [now, setNow] = useState(Date.now());

  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);

  if (!status) return null;

  const state = LIVE_STATES[status.state] || { label: status.state, tone: "idle" };
  // Two ways to disagree with the server's own optimism, and both have to win.
  //
  // The first is the socket: a server that says "live" is no help if this tab cannot
  // reach it, and the tab is what the reader is looking at. The second is time: a
  // stream can stay open and silent, which no error event will ever report, so a cycle
  // that is overdue against the server's stated interval overrides the label too.
  const unreachable = connection === CONNECTION.CONNECTING && status.state !== "disabled";
  const stalled = cycleOverdue(cycle, status, now);
  const tone = unreachable || stalled ? "bad" : state.tone;
  const label = stalled ? "not updating" : unreachable ? "reconnecting" : state.label;

  return (
    <div className={`connection ${tone}`} title={status.detail || undefined}>
      <span className="dot" />
      <span className="label">{label}</span>
      <span className="session">market {status.session}</span>
    </div>
  );
}

export function Note({ children }) {
  if (!children) return null;
  return <div className="note">{children}</div>;
}

export function Notes({ items }) {
  if (!items || items.length === 0) return null;
  return (
    <>
      {items.map((text, index) => (
        <Note key={index}>{text}</Note>
      ))}
    </>
  );
}

export function ErrorBox({ error }) {
  if (!error) return null;
  return <div className="error">{error}</div>;
}

export function Panel({ title, right, children }) {
  return (
    <section className="panel">
      {(title || right) && (
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "baseline",
            gap: 12,
          }}
        >
          {title && <h2>{title}</h2>}
          {right}
        </div>
      )}
      {children}
    </section>
  );
}

export function Stat({ label, value, hint }) {
  return (
    <div className="stat">
      <div className="label">{label}</div>
      <div className="value">{value}</div>
      {hint && <div className="provenance">{hint}</div>}
    </div>
  );
}

// A number cell that renders "n/a" in a dimmed style rather than a plausible zero.
export function Cell({ value, formatted, className = "" }) {
  const missing = value === null || value === undefined;
  return <td className={`${className} ${missing ? "missing" : ""}`.trim()}>{formatted}</td>;
}
