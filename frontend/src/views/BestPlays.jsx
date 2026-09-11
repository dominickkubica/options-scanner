import { useMemo } from "react";
import { api } from "../api.js";
import { ErrorBox, Notes, Panel, useAsync } from "../components/common.jsx";
import { count, money, num, pct, reasonLabel, strategyLabel } from "../format.js";

// The landing screen: what is worth looking at first, across the whole watchlist.
//
// Opportunities is the full ranked table and stays exactly as it is. This view exists
// because a 200 row table sorted by a composite score answers "what did the screen
// find" and not "what should I look at", and those are different questions. Here the
// top candidate is a card rather than the first row of a table, because the first row
// of a table does not look any more important than the fortieth.
//
// Three rules govern everything below, and none of them are styling:
//
//   1. This view scans the watchlist, not the selected symbol. A "best play available"
//      that silently meant "best play in NVDA" would be the most misleading screen in
//      the app. The sidebar selection deliberately does not reach this component.
//
//   2. A near miss is never drawn like a play. Near misses are scored with the same
//      function as passing candidates and routinely score higher, because the gate
//      that blocked them is not an input to the score. The top blocked candidate in
//      testing scored 0.976 against 0.956 for the best that actually passed. Anything
//      that let those two share a visual language would be actively dangerous, so
//      blocked cards are outlined and muted and always carry their blocker.
//
//   3. The runners are one per strategy, not the next six by score. Ranking the whole
//      list and taking the top slice showed the same shape six times: for months every
//      card here was a cash secured put, because the premium component was pinned at
//      1.000 on 89% of rows and what actually ordered the list was probability and
//      liquidity, both of which favour a single leg. Fixing that scale helped and did
//      not solve this: whichever shape happens to score best still sweeps the row.
//      A screen called "best plays" that only ever shows one structure is hiding the
//      choice it exists to present, so each strategy gets exactly one slot and wins it
//      against its own kind.
//
//   4. The score gets a bar, not a colour band. A green-amber-red ramp would assert
//      that some threshold is good, and no threshold here has earned that: the
//      validation study is still under its own cluster minimum. The bar shows the
//      number relative to the others on screen and claims nothing else.

const TOP_COUNT = 6;

// The one blocker that clears by waiting rather than by the market moving. Everything
// else in the near miss list needs a price or a spread to change.
const MATURES_ON_ITS_OWN = "dte_too_long";

function legText(row) {
  return row.legs
    .map((leg) => `${leg.action === "sell" ? "-" : "+"}${leg.strike}${leg.right}`)
    .join(" / ");
}

function ScoreBar({ score, muted }) {
  const width = `${Math.max(0, Math.min(1, score)) * 100}%`;
  return (
    <div className="score-bar" title={`score ${num(score, 3)}`}>
      <span className={`score-bar-fill ${muted ? "muted" : ""}`} style={{ width }} />
    </div>
  );
}

function Metric({ label, value, wide }) {
  return (
    <div className={`metric ${wide ? "wide" : ""}`}>
      <span className="metric-value">{value}</span>
      <span className="metric-label">{label}</span>
    </div>
  );
}

// The single best passing candidate, given the room to actually be read.
function HeroPlay({ play, onOpenPayoff }) {
  return (
    <div className="hero-card">
      <div className="hero-head">
        <div>
          <div className="hero-symbol">{play.symbol}</div>
          <div className="hero-strategy">{strategyLabel(play.strategy)}</div>
        </div>
        <div className="hero-score">
          <div className="hero-score-value">{num(play.score, 3)}</div>
          <div className="metric-label">score</div>
        </div>
      </div>

      <div className="hero-legs">{legText(play)}</div>
      <div className="hero-sub">
        {play.expiry} &middot; {play.dte}d to expiry &middot; spot {num(play.underlying_price)}
      </div>

      <ScoreBar score={play.score} />

      <div className="metric-row">
        <Metric label="credit" value={num(play.credit)} />
        <Metric label="max profit" value={money(play.max_profit)} />
        <Metric label="max loss" value={money(play.max_loss)} />
        <Metric label="capital" value={money(play.capital)} />
        <Metric label="annualized" value={pct(play.annualized_return, 0)} />
        <Metric label="prob of profit" value={pct(play.probability_of_profit, 0)} />
        {/* "tested delta", not "short delta". On a condor the two shorts nearly cancel
            and the old net figure read 0.01 beside a 69% probability of profit, which
            described no position that exists. This is the nearer wing. */}
        <Metric label="tested delta" value={num(play.short_delta)} />
        <Metric label="liquidity" value={num(play.liquidity_score)} />
      </div>

      {play.warnings.length > 0 && (
        <ul className="hero-warnings">
          {play.warnings.map((warning, index) => (
            <li key={index}>{warning}</li>
          ))}
        </ul>
      )}

      <button type="button" className="btn primary" onClick={() => onOpenPayoff(play)}>
        Show payoff
      </button>
    </div>
  );
}

function PlayCard({ play, onOpenPayoff }) {
  return (
    <button type="button" className="play-card" onClick={() => onOpenPayoff(play)}>
      <div className="play-head">
        <span className="play-symbol">{play.symbol}</span>
        <span className="play-score">{num(play.score, 3)}</span>
      </div>
      <div className="play-strategy">{strategyLabel(play.strategy)}</div>
      <div className="play-legs">{legText(play)}</div>
      <ScoreBar score={play.score} />
      <div className="play-metrics">
        <Metric label="credit" value={num(play.credit)} />
        <Metric label="profit" value={money(play.max_profit)} />
        <Metric label="ann" value={pct(play.annualized_return, 0)} />
        <Metric label="pop" value={pct(play.probability_of_profit, 0)} />
      </div>
      <div className="play-foot">
        {play.expiry} &middot; {play.dte}d
      </div>
    </button>
  );
}

// Outlined, never filled. See rule 2 at the top of the file.
function BlockedCard({ item }) {
  const play = item.opportunity;
  const countdown = item.enters_screen_in_days;
  return (
    <div className="play-card blocked">
      <div className="play-head">
        <span className="play-symbol">{play.symbol}</span>
        <span className="play-score">{num(play.score, 3)}</span>
      </div>
      <div className="play-strategy">{strategyLabel(play.strategy)}</div>
      <div className="play-legs">{legText(play)}</div>
      <ScoreBar score={play.score} muted />
      <div className="blocker-line">
        {countdown === null ? "blocked by" : "waiting on"} {reasonLabel(item.blocker)}
      </div>
      {countdown !== null && (
        <div className="countdown">
          {countdown === 0 ? "eligible now" : `enters the screen in ${countdown}d`}
        </div>
      )}
      <div className="play-foot">
        {play.expiry} &middot; {play.dte}d
      </div>
    </div>
  );
}

export default function BestPlays({ onOpenPayoff, symbols = null }) {
  // Null symbols means the server falls back to the whole watchlist, which is the only
  // thing that makes "best available" a true statement. Passed a list, this becomes the
  // ranked plays for one ticker, which is what the per-symbol tab wants and is why the
  // separate Opportunities view is gone: it was the same question asked twice.
  const { data, error, loading } = useAsync(
    () => api.scan(symbols, 200, { nearMiss: true }),
    [symbols],
  );

  const { hero, runners, maturing, almost } = useMemo(() => {
    if (!data) return { hero: null, runners: [], maturing: [], almost: [] };
    const plays = data.opportunities;
    const near = data.near_misses || [];

    // One card per strategy: the best of each, in score order, minus whichever one is
    // already the hero. `plays` arrives ranked, so the first time a strategy is seen is
    // its best instance and no sorting is needed here.
    const best = new Map();
    for (const play of plays) {
      if (!best.has(play.strategy)) best.set(play.strategy, play);
    }
    const champion = plays[0] || null;
    const perStrategy = [...best.values()].filter((play) => play.id !== champion?.id);

    return {
      hero: champion,
      runners: perStrategy.slice(0, TOP_COUNT),
      maturing: near
        .filter((item) => item.blocker === MATURES_ON_ITS_OWN)
        .sort((a, b) => a.enters_screen_in_days - b.enters_screen_in_days),
      almost: near.filter((item) => item.blocker !== MATURES_ON_ITS_OWN).slice(0, TOP_COUNT),
    };
  }, [data]);

  // Older than this and the chain is not from the session being traded. A live chain
  // refreshes every twenty seconds, so anything past an hour means the fetch is falling
  // back to a stored capture.
  const STALE_AFTER_SECONDS = 3600;
  const oldest = Math.max(0, ...Object.values(data?.stale || {}));
  const staleHours =
    oldest > STALE_AFTER_SECONDS
      ? oldest > 7200
        ? `${Math.round(oldest / 3600)} hour old`
        : "1 hour old"
      : null;

  if (loading) return <div className="loading">Ranking the watchlist...</div>;
  if (error) return <ErrorBox error={error} />;
  if (!data) return null;

  return (
    <>
      {/* The chain's age, on the panel rather than only in the header badge.
          `stale` has always been sent and this view never rendered it, so a card
          scored on a 22 hour old chain looked exactly like one scored on a live one:
          the numbers were presented with total confidence and the only contradiction
          was a small badge at the top of the page saying the chain was from yesterday.
          Everything on a card is derived from that chain -- the credit, the greeks,
          the probability, the DTE -- so its age is a property of every number here. */}
      {staleHours !== null && (
        <div className="sample-banner">
          <strong>Scored on a {staleHours} chain.</strong> Every number below comes from
          that capture, including the credit and the days to expiry. Press refresh in
          the header, or check the fills against your broker before trading them.
        </div>
      )}

      <Notes items={data.notes} />

      <Panel
        title="Best play available"
        right={
          <span className="provenance">
            {count(data.passed)} passed of {count(data.considered)} across{" "}
            {data.symbols_scanned.join(", ") || "no symbols"}
          </span>
        }
      >
        {hero ? (
          <>
            <HeroPlay play={hero} onOpenPayoff={onOpenPayoff} />
            <div className="caveat">{data.disclaimer}</div>
          </>
        ) : (
          <div className="empty-state">
            Nothing passed the screen on the last capture. The Opportunities view has
            the rejection tally, which usually points at a threshold in screen.yaml
            rather than an empty market.
          </div>
        )}
      </Panel>

      {runners.length > 0 && (
        /* The "see all" button pointed at the Opportunities view, which was the same
           ranking rendered a second way. The count says how many there are; the list
           below is the part worth looking at. */
        <Panel
          title="Best of each structure"
          right={
            <span className="muted">
              one per strategy &middot; {count(data.opportunities.length)} in all
            </span>
          }
        >
          <div className="card-grid">
            {runners.map((play) => (
              <PlayCard key={play.id} play={play} onOpenPayoff={onOpenPayoff} />
            ))}
          </div>
        </Panel>
      )}

      {maturing.length > 0 && (
        <Panel
          title="Coming onto the screen"
          right={
            <span className="provenance">
              blocked only by the {"<="} {data.max_dte ?? "DTE"} day ceiling
            </span>
          }
        >
          <div className="card-grid">
            {maturing.map((item) => (
              <BlockedCard key={item.opportunity.id} item={item} />
            ))}
          </div>
          <div className="caveat">
            These clear by the calendar alone: nothing has to happen for them to become
            eligible, only time. Their credit and liquidity will have moved by then, so
            the numbers shown are today's, not a forecast of the day they qualify.
          </div>
        </Panel>
      )}

      {almost.length > 0 && (
        <Panel title="One gate away">
          <div className="card-grid">
            {almost.map((item) => (
              <BlockedCard key={item.opportunity.id} item={item} />
            ))}
          </div>
          <div className="caveat">
            Each of these failed exactly one check group and needs the market to move to
            pass. They are shown outlined rather than filled because they can and do
            outscore everything that passed: the gate blocking them is not an input to
            the score, so a high number here is not a better trade, it is a trade the
            screen rejected. A check group can also hide a second problem behind the
            reason it names.
          </div>
        </Panel>
      )}
    </>
  );
}
