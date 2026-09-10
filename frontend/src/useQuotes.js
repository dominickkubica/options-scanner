import { useEffect, useRef, useState } from "react";
import { api } from "./api.js";

// Polling prices, with the four behaviours that separate a live number from a
// flickering one.
//
// ## It keeps the last good answer
//
// `useAsync` clears `data` before every reload, which is right for a panel you
// navigated to and wrong for one that refetches every ten seconds: the pills would
// blank on each tick. A failed poll here leaves the previous prices on screen and
// sets `error`, because a price from forty seconds ago is worth vastly more than an
// empty pill, and `as_of` on each quote already says how old it is.
//
// ## The server decides the cadence
//
// `poll_seconds` comes back on the response rather than being a constant here. The
// market calendar lives on the server -- it knows about holidays and half days, and
// this file must not grow a second copy of it. A closed market polls slowly instead
// of not at all, so a tab left open overnight comes alive at the bell.
//
// ## It stops when nobody is looking
//
// A backgrounded tab polling all night is requests spent on pixels nobody sees, and
// the budget it spends is shared with the capture jobs. Hiding the tab stops the loop.
//
// ## Becoming visible does not mean refetching
//
// The obvious version of the line above fetches immediately on every `visibilitychange`,
// and it was measured firing **two requests on every page load**: the page mounts while
// the tab is still hidden, ticks once, then becomes visible and ticks again 200ms later.
// Focus a window forty times and that is forty needless requests against a budget shared
// with the capture jobs.
//
// So returning to the tab asks how old the data actually is. Older than the interval,
// fetch now; younger, just resume the timer for whatever is left of it. The visible
// behaviour is the same -- you never look at a stale number while a timer runs down --
// without the duplicate.

const FALLBACK_POLL_SECONDS = 60;

export function useQuotes(symbols) {
  const [state, setState] = useState({
    quotes: {},
    note: null,
    delayMinutes: null,
    error: null,
  });

  const timer = useRef(null);
  const fetchedAt = useRef(0);
  const intervalSeconds = useRef(FALLBACK_POLL_SECONDS);
  const inFlight = useRef(false);

  // The symbol list is an array rebuilt on every render, so it cannot be a dependency
  // directly without restarting the poll on each one.
  const key = symbols.join(",");

  useEffect(() => {
    if (!key) {
      setState({ quotes: {}, note: null, delayMinutes: null, error: null });
      return undefined;
    }

    let live = true;
    const wanted = key.split(",");

    const clear = () => {
      if (timer.current !== null) {
        window.clearTimeout(timer.current);
        timer.current = null;
      }
    };

    const schedule = (seconds) => {
      clear();
      if (!live || document.hidden) return;
      timer.current = window.setTimeout(tick, Math.max(seconds, 1) * 1000);
    };

    function tick() {
      // A slow response must not stack a second request on top of the first. Without
      // this, a vendor taking longer than the interval turns one poll into a queue.
      if (inFlight.current) return;
      inFlight.current = true;

      api
        .quotes(wanted)
        .then((data) => {
          fetchedAt.current = Date.now();
          intervalSeconds.current = data.poll_seconds || FALLBACK_POLL_SECONDS;
          if (!live) return;
          setState({
            quotes: data.quotes || {},
            note: data.note || null,
            delayMinutes: data.delay_minutes ?? null,
            error: null,
          });
          schedule(intervalSeconds.current);
        })
        .catch((error) => {
          fetchedAt.current = Date.now();
          if (!live) return;
          // Prices are kept, not cleared. See the note above.
          setState((prev) => ({ ...prev, error: error.message }));
          schedule(FALLBACK_POLL_SECONDS);
        })
        .finally(() => {
          inFlight.current = false;
        });
    }

    const onVisibility = () => {
      if (document.hidden) {
        clear();
        return;
      }
      const elapsed = (Date.now() - fetchedAt.current) / 1000;
      if (elapsed >= intervalSeconds.current) tick();
      else schedule(intervalSeconds.current - elapsed);
    };

    // Mounting hidden is a real case -- a background tab, a restored session -- and
    // fetching into a page nobody is looking at spends a request for nothing. The
    // visibility handler picks it up the moment the tab is actually shown.
    if (document.hidden) clear();
    else tick();

    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      live = false;
      clear();
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [key]);

  return state;
}
