import { useMemo, useState } from "react";
import { INDICATORS } from "./indicators.js";
import { colourRows, toHexInput } from "./colours.js";

// The colour picker for every line on the chart.
//
// ## Why it lists everything, not just what is switched on
//
// Setting a colour and then turning the indicator on is the order people work in when
// they are laying out a chart, and a picker that only offers what is already visible
// forces the opposite. Rows are grouped by indicator so the list stays navigable at
// thirty-odd entries.
//
// ## Why a row can be reset on its own
//
// An override is only stored once it is changed, so the stylesheet remains the source of
// the palette for everything untouched. Reverting one line has to put it back under that
// rule rather than freezing today's default as a new override, or the next palette
// change would silently skip it.

export default function ColourPicker({ theme, colours, onChange, onReset, onClose }) {
  const [query, setQuery] = useState("");

  const groups = useMemo(() => {
    const rows = colourRows(INDICATORS, theme, colours);
    const needle = query.trim().toLowerCase();
    const matching = needle
      ? rows.filter(
          (row) =>
            row.label.toLowerCase().includes(needle) ||
            row.group.toLowerCase().includes(needle),
        )
      : rows;

    const out = new Map();
    for (const row of matching) {
      if (!out.has(row.group)) out.set(row.group, []);
      out.get(row.group).push(row);
    }
    return [...out.entries()];
  }, [theme, colours, query]);

  const overrides = Object.keys(colours).length;

  return (
    <div className="colour-picker">
      <div className="colour-picker-head">
        <strong>Chart colours</strong>
        <input
          type="search"
          placeholder="filter"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
        />
        <button
          type="button"
          className="btn"
          onClick={onReset}
          disabled={overrides === 0}
          title={
            overrides === 0
              ? "Nothing has been changed"
              : `Put all ${overrides} back to the stylesheet's palette`
          }
        >
          Reset all
        </button>
        <button type="button" className="pane-close" aria-label="Close" onClick={onClose}>
          ×
        </button>
      </div>

      <div className="colour-picker-body">
        {groups.length === 0 && <div className="empty-state">Nothing matches.</div>}
        {groups.map(([group, rows]) => (
          <div key={group} className="colour-group">
            <div className="section-label">{group}</div>
            {rows.map((row) => {
              const hex = toHexInput(row.value);
              return (
                <div key={row.key} className="colour-row">
                  <span className="colour-swatch" style={{ background: row.value }} />
                  <span className="colour-label">{row.label}</span>
                  {hex ? (
                    <input
                      type="color"
                      value={hex}
                      onChange={(event) => onChange(row.key, event.target.value)}
                      aria-label={`${group} ${row.label} colour`}
                    />
                  ) : (
                    /* A translucent palette entry has no hex the input can hold. Saying
                       so beats showing a picker that silently turns it black. */
                    <span className="colour-note">not adjustable</span>
                  )}
                  <button
                    type="button"
                    className="link-btn"
                    onClick={() => onChange(row.key, null)}
                    disabled={!row.overridden}
                  >
                    {row.overridden ? "revert" : "default"}
                  </button>
                </div>
              );
            })}
          </div>
        ))}
      </div>

      <div className="chart-note">
        Saved in this browser only. Anything left alone follows the stylesheet, so a
        change to the app&apos;s palette still reaches it.
      </div>
    </div>
  );
}
