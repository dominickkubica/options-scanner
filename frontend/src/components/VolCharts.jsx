import { useCallback, useMemo, useState } from "react";
import { Plot, PlotReadout, linePath } from "./chart/plot.jsx";
import { num, vol } from "../format.js";

// The two volatility surface charts: vol across expiries, and vol across strikes.
//
// Both were fixed 460 pixel viewBoxes with no hover, which on a wide panel meant
// scaled-up axis text and a curve you could read the shape of but never a value from.
// Reading a value is most of the point: "what is the 30 day vol" and "what is the 25
// delta put trading at" are the questions these panels exist to answer.

//: Moneyness band the smile is drawn over.
//:
//: Not cosmetic. A far out of the money strike quoted 0.00 by 0.05 solves to a real
//: implied volatility of 195 percent, and plotting it against a 14 percent at the
//: money vol compresses the entire meaningful part of the curve into the bottom two
//: pixels. The same band, and the same reason, as the smile fit in the gaps module.
const SKEW_BAND = 0.2;

export function TermStructure({ points, backwardated, slope }) {
  const [hover, setHover] = useState(null);
  const usable = points || [];

  const xs = useMemo(() => usable.map((point) => point.dte), [usable]);
  const ys = useMemo(() => usable.map((point) => point.iv), [usable]);

  const onHoverX = useCallback(
    (dte) => {
      if (dte === null || usable.length === 0) return setHover(null);
      let best = usable[0];
      for (const point of usable) {
        if (Math.abs(point.dte - dte) < Math.abs(best.dte - dte)) best = point;
      }
      return setHover(best);
    },
    [usable],
  );

  return (
    <div>
      <PlotReadout
        items={[
          { label: "dte", value: hover ? `${hover.dte}d` : "hover" },
          { label: "atm iv", value: hover ? vol(hover.iv) : "-" },
          { label: "expiry", value: hover ? hover.expiry : "-" },
        ]}
      />
      <Plot
        height={200}
        xs={xs}
        ys={ys}
        formatY={(value) => vol(value)}
        onHoverX={onHoverX}
        empty="No expiry has a solvable at the money volatility."
      >
        {({ scale }) => (
          <>
            <path
              d={linePath(
                usable.map((point) => ({ x: scale.toX(point.dte), y: scale.toY(point.iv) })),
              )}
              fill="none"
              stroke="var(--accent)"
              strokeWidth="2"
            />
            {usable.map((point) => (
              <circle
                key={point.expiry}
                cx={scale.toX(point.dte)}
                cy={scale.toY(point.iv)}
                r={hover && hover.expiry === point.expiry ? 4.5 : 2.5}
                fill="var(--accent)"
              />
            ))}
          </>
        )}
      </Plot>
      <div className="chart-note">
        {slope === null || slope === undefined
          ? "Not enough expiries past the front week to measure a slope."
          : `Slope past 7 DTE: ${(slope * 100).toFixed(1)} vol points. ${
              backwardated
                ? "Backwardated, so something is expected soon. Look for the event before selling the front."
                : "Normal upward term structure."
            }`}
      </div>
    </div>
  );
}

export function SkewCurve({ putSkew, callSkew, spot, atmIv }) {
  const [hover, setHover] = useState(null);

  const inBand = useCallback(
    (points) =>
      (points || []).filter((point) => Math.abs(point.strike / spot - 1) <= SKEW_BAND),
    [spot],
  );

  const puts = useMemo(() => inBand(putSkew), [inBand, putSkew]);
  const calls = useMemo(() => inBand(callSkew), [inBand, callSkew]);

  const xs = useMemo(
    () => [...puts, ...calls].map((point) => point.strike),
    [puts, calls],
  );
  const ys = useMemo(() => [...puts, ...calls].map((point) => point.iv), [puts, calls]);

  const onHoverX = useCallback(
    (strike) => {
      if (strike === null) return setHover(null);
      const all = [
        ...puts.map((p) => ({ ...p, side: "put" })),
        ...calls.map((p) => ({ ...p, side: "call" })),
      ];
      if (all.length === 0) return setHover(null);
      let best = all[0];
      for (const point of all) {
        if (Math.abs(point.strike - strike) < Math.abs(best.strike - strike)) best = point;
      }
      return setHover(best);
    },
    [puts, calls],
  );

  return (
    <div>
      <PlotReadout
        items={[
          { label: "strike", value: hover ? num(hover.strike) : "hover" },
          { label: "iv", value: hover ? vol(hover.iv) : "-" },
          { label: "side", value: hover ? hover.side : "-" },
          { label: "atm", value: atmIv ? vol(atmIv) : "-" },
        ]}
      />
      <Plot
        height={200}
        xs={xs}
        ys={ys}
        formatY={(value) => vol(value)}
        onHoverX={onHoverX}
        empty={`No strike within ${Math.round(SKEW_BAND * 100)}% of spot solved for a volatility.`}
      >
        {({ scale }) => (
          <>
            {spot && (
              <line
                x1={scale.toX(spot)}
                x2={scale.toX(spot)}
                y1={scale.top}
                y2={scale.bottom}
                stroke="var(--accent)"
                strokeDasharray="3 3"
              />
            )}
            <path
              d={linePath(
                puts.map((point) => ({ x: scale.toX(point.strike), y: scale.toY(point.iv) })),
              )}
              fill="none"
              stroke="var(--chart-down)"
              strokeWidth="1.8"
            />
            <path
              d={linePath(
                calls.map((point) => ({ x: scale.toX(point.strike), y: scale.toY(point.iv) })),
              )}
              fill="none"
              stroke="var(--chart-up)"
              strokeWidth="1.8"
            />
            {hover && (
              <circle
                cx={scale.toX(hover.strike)}
                cy={scale.toY(hover.iv)}
                r="4"
                fill={hover.side === "put" ? "var(--chart-down)" : "var(--chart-up)"}
              />
            )}
          </>
        )}
      </Plot>
      <div className="chart-note">
        Red: puts. Green: calls. Each point is that strike&apos;s own solved volatility,
        in vol points. Drawn over strikes within {Math.round(SKEW_BAND * 100)}% of spot:
        a 0.00 by 0.05 wing solves to a real 195 percent vol and would flatten the rest
        of the curve into the bottom of the panel.
      </div>
    </div>
  );
}
