import { useEffect, useRef } from "react";
import { createChart } from "lightweight-charts";

// The one chart that uses a library. Candles plus a volume histogram on its own scale
// is a lot of fiddly axis work to hand roll, and lightweight-charts is small and does
// exactly this.
//
// Bars arrive already shaped for it: `time` is an ISO date string, which is the format
// the library wants, and the API sorts them ascending because the library requires it
// and silently misbehaves otherwise.
export default function Candles({ bars, height = 280 }) {
  const container = useRef(null);

  useEffect(() => {
    if (!container.current || !bars || bars.length === 0) return undefined;

    const chart = createChart(container.current, {
      height,
      // autoSize watches the container with a ResizeObserver. Without it the chart
      // takes the library's 300 pixel default, because the container has no width at
      // the moment the effect runs and a one shot measurement reads zero.
      autoSize: true,
      layout: { background: { color: "transparent" }, textColor: "#8b97a6" },
      grid: {
        vertLines: { color: "rgba(38,46,57,0.6)" },
        horzLines: { color: "rgba(38,46,57,0.6)" },
      },
      rightPriceScale: { borderColor: "#262e39" },
      timeScale: { borderColor: "#262e39" },
      crosshair: { mode: 0 },
    });

    const candles = chart.addCandlestickSeries({
      upColor: "#46b17b",
      downColor: "#d9635f",
      borderUpColor: "#46b17b",
      borderDownColor: "#d9635f",
      wickUpColor: "#46b17b",
      wickDownColor: "#d9635f",
    });
    candles.setData(
      bars.map((bar) => ({
        time: bar.time,
        open: bar.open,
        high: bar.high,
        low: bar.low,
        close: bar.close,
      })),
    );

    // Volume goes on its own scale pinned to the bottom, so it reads as context under
    // the price rather than competing with it.
    const withVolume = bars.filter((bar) => bar.volume !== null && bar.volume !== undefined);
    if (withVolume.length > 0) {
      const volume = chart.addHistogramSeries({
        priceFormat: { type: "volume" },
        priceScaleId: "volume",
      });
      chart.priceScale("volume").applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
      volume.setData(
        withVolume.map((bar) => ({
          time: bar.time,
          value: bar.volume,
          color: bar.close >= bar.open ? "rgba(70,177,123,0.4)" : "rgba(217,99,95,0.4)",
        })),
      );
    }

    chart.timeScale().fitContent();

    return () => chart.remove();
  }, [bars, height]);

  if (!bars || bars.length === 0) return null;
  return <div ref={container} style={{ width: "100%" }} />;
}
