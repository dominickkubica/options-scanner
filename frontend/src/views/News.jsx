import { api } from "../api.js";
import { ErrorBox, Note, Panel, useAsync } from "../components/common.jsx";

// News for one symbol.
//
// ## Why the market wraps are dimmed rather than dropped
//
// A story is tagged with whatever symbols the wire attached, and a daily market wrap
// carries thirty of them. Those are not news about any one ticker, and mixing them in
// undimmed makes the panel look busy while saying nothing. Dropping them is worse
// though: occasionally the wrap is the thing that moved the price, and a panel that
// silently hid the only story of the day would be actively misleading. The server marks
// them and they render quietly, under a heading that says what they are.
//
// ## Why nothing here is cached
//
// News ages out of relevance in hours. A cached copy would be worse than useless: it
// would look current and be a day old.

function when(iso) {
  const at = new Date(iso);
  const minutes = Math.round((Date.now() - at.getTime()) / 60000);
  if (minutes < 60) return `${Math.max(minutes, 0)}m ago`;
  if (minutes < 60 * 24) return `${Math.round(minutes / 60)}h ago`;
  return at.toLocaleDateString();
}

function Story({ item }) {
  return (
    <article className={`story ${item.primary ? "" : "broad"}`}>
      {item.image && <img className="story-image" src={item.image} alt="" loading="lazy" />}
      <div className="story-body">
        <a className="story-headline" href={item.url} target="_blank" rel="noreferrer">
          {item.headline}
        </a>
        {item.summary && <p className="story-summary">{item.summary}</p>}
        <div className="story-meta">
          <span>{item.wire || "wire"}</span>
          <span>{when(item.published_at)}</span>
          {/* How many tickers the wire tagged. The number is the reason this story is
              in the quiet half of the panel, so it is shown rather than implied. */}
          {!item.primary && <span>{item.symbols.length} symbols tagged</span>}
        </div>
      </div>
    </article>
  );
}

export default function News({ symbol }) {
  const news = useAsync(() => api.news(symbol), [symbol], { enabled: Boolean(symbol) });

  const items = news.data?.items || [];
  const primary = items.filter((item) => item.primary);
  const broad = items.filter((item) => !item.primary);

  return (
    <>
      <Panel title={`News for ${symbol}`}>
        <ErrorBox error={news.error} onRetry={news.reload} />
        {news.loading && <div className="loading">Fetching...</div>}
        {news.data?.note && <Note>{news.data.note}</Note>}

        {!news.loading && primary.length === 0 && !news.data?.note && (
          <div className="empty-state">
            Nothing recent is tagged specifically to {symbol}.
          </div>
        )}

        <div className="story-list">
          {primary.map((item) => (
            <Story key={item.id} item={item} />
          ))}
        </div>
      </Panel>

      {broad.length > 0 && (
        <Panel title="Mentioned in broader coverage">
          <div className="story-list">
            {broad.map((item) => (
              <Story key={item.id} item={item} />
            ))}
          </div>
          <div className="chart-note">
            These are tagged to {symbol} along with many other tickers, so they are
            market coverage rather than news about this company. Kept because
            occasionally the wrap is the thing that moved the price.
          </div>
        </Panel>
      )}
    </>
  );
}
