React + Vite + lightweight-charts.

```
npm install
npm run dev     # Vite on 5173, proxying /api to uvicorn on 8000
npm run build   # emits dist/, which `optscan serve` then hosts at :8000
```

`npm run dev` needs the API running: `venv\Scripts\python -m optscan serve` from the
repo root, or the `optscan-api` entry in `.claude/launch.json`.

Layout:

```
src/api.js          every endpoint, surfacing the server's own error text
src/format.js       formatting, and the rule that null renders "n/a" and never 0
src/App.jsx         the shell: pick a symbol, pick a view
src/views/          Opportunities, Chain, Underlying, Payoff
src/components/     common.jsx, charts.jsx (hand rolled SVG), Candles.jsx
```

Two conventions carry over from the Python side and matter as much here:

- **Null is not zero.** Nothing in this app falls back to `|| 0`. The API is careful to
  send null for a number it could not compute honestly, and undoing that in a table
  cell or a chart point would hide the refusal where nobody can see it.
- **Every panel shows where its numbers came from and how old they are.** The whole
  dashboard renders the last stored capture, not the market.
