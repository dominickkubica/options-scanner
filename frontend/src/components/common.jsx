import { useCallback, useEffect, useState } from "react";
import { age } from "../format.js";

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
