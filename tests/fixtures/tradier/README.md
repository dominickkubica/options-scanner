# Tradier response fixtures

**These are constructed from documentation, not captured from the wire.** No Tradier
token existed when they were written on 2026-07-31, so every file here was built from
the response schemas and examples published at docs.tradier.com, listed below. They
pin the shapes the adapter depends on and they are enough to test the parsing, the
one-or-many quirk, and the failure mapping. They are not evidence that the live API
behaves this way.

`tests/fixtures/spy_chain_snapshot.json` is the opposite: a real captured chain. Keep
the distinction visible. When a token exists, recapture these against the sandbox and
delete this paragraph.

Sources, all read 2026-07-31:

- https://docs.tradier.com/reference/brokerage-api-markets-get-quotes
- https://docs.tradier.com/reference/brokerage-api-markets-get-options-chains
- https://docs.tradier.com/reference/brokerage-api-markets-get-options-expirations
- https://docs.tradier.com/reference/brokerage-api-markets-get-history
- https://docs.tradier.com/docs/rate-limiting
- https://docs.tradier.com/docs/endpoints

Deliberate awkwardness in the data, because the adapter has to survive all of it:

- `quote_single.json` uses the object form of `quotes.quote`; `quote_multi.json` uses
  the array form. Tradier's schema is a `oneOf` and both really happen.
- `chain.json` includes a zero bid, a null greeks block, a row with no `option_type`,
  and a row whose `strike` is missing. Only the last two are unparseable.
- `history.json` includes one bar whose `close` sits outside its own high and low,
  which the domain model rejects and the adapter must drop rather than propagate.
