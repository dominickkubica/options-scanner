# Tradier response fixtures

Two sets, and the distinction is the point.

## `*.json` here: constructed from documentation

Built on 2026-07-31 from the response schemas published at docs.tradier.com, because no
Tradier token existed yet. They pin the shapes the adapter depends on and the failures
it has to survive.

Deliberate awkwardness in the data, because the adapter has to survive all of it:

- `quote_single.json` uses the object form of `quotes.quote`; `quote_multi.json` uses
  the array form. Tradier's schema is a `oneOf` and both really happen, which the live
  capture has now confirmed.
- `chain.json` includes a zero bid, a null greeks block, a row with no `option_type`,
  and a row whose `strike` is missing. Only the last two are unparseable.
- `history.json` includes one bar whose `close` sits outside its own high and low,
  which the domain model rejects and the adapter must drop rather than propagate.

**These are not overwritten now that a token exists**, which is the opposite of what an
earlier version of this file instructed. The damage above is the reason they exist: a
healthy response contains none of it, so a real capture cannot test any of it. Overwriting
them would have quietly deleted the adapter's entire refusal test suite.

## `live/`: captured from the sandbox

Real responses from `sandbox.tradier.com`, captured 2026-08-02 against SPY. Rows are
unmodified. `chain.json` is trimmed to 44 rows, a near the money window plus both wings,
because the full response is 464 KB; rows were kept or dropped, never edited, and the
kept set still contains six real zero bids and seven contracts that have never traded.

`test_provider_tradier_live_capture.py` drives the adapter against these and, more
usefully, asserts that every field the constructed fixtures assume is really on the wire.
A vendor renaming a field would otherwise break the adapter silently while the
constructed fixtures kept passing forever, because they encode the old name too.

What the first capture established:

- The one-or-many quirk is real. One symbol returns an object, two return an array.
- `unmatched_symbols` is the real shape for an unknown ticker.
- The greeks block and the history day match the documentation exactly.
- Every chain field the adapter reads is present, and the wire carries eight more it
  ignores (`change_percentage`, `week_52_high`, exchange codes and volumes).
- One documented quote field, `lot_size`, is **not** returned. Nothing reads it: contract
  size comes off the chain row.

Sources for the constructed set, all read 2026-07-31:

- https://docs.tradier.com/reference/brokerage-api-markets-get-quotes
- https://docs.tradier.com/reference/brokerage-api-markets-get-options-chains
- https://docs.tradier.com/reference/brokerage-api-markets-get-options-expirations
- https://docs.tradier.com/reference/brokerage-api-markets-get-history
- https://docs.tradier.com/docs/rate-limiting
- https://docs.tradier.com/docs/endpoints
