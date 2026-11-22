# Demo: UI with mock data

Load the real panel UI against a mock API and screenshot every tab — for docs,
design review, or checking a UI change without the server, Docker, or GPUs.

```
tools/demo/
├── mock-api.py   # mock of panel-api on 127.0.0.1:8010 (stdlib only, no deps)
├── shot.mjs      # headless-Chromium screenshot of all six tabs (Node ≥ 22)
└── screens/      # the captured PNGs
```

## Run

```sh
# 1. mock API — serves the /api read shapes (metrics, containers, gpus, gen,
#    settings/llm, update, …) as bounded random walks around a snapshot of the
#    live box: Threadripper, 7× AI PRO R9700 (four saturated chat cards, two
#    idle gen slots, one reserved x8 card), 13 containers, the real action
#    registry, the gen slot layout. Mutation endpoints are no-op stubs.
python3 tools/demo/mock-api.py          # http://127.0.0.1:8010

# 2. UI — the existing dev loop; Vite proxies /api → localhost:8010
cd compose/panel-frontend && npm run dev   # http://localhost:5173

# 3. screenshots (optional; clicks each tab, 35 s warm-up so the traces fill)
#    <tab>.png is the 1920x1080 viewport (the second-monitor view);
#    <tab>-full.png is the full page, only for tabs taller than 1080 px
node tools/demo/shot.mjs                  # → tools/demo/screens/*.png
```

## What it is (and isn't)

- **Stands in for `panel-api` only.** No Docker, no `/sys`, no `panel-do`:
  nothing privileged is reachable and nothing here is a second privileged
  path. It has no auth because the dev server in front of it is local-only.
- **Data is a snapshot, not live:** every reading is a small random walk, so
  the charts move and the "5m peak" picks up a realistic net burst.
- The action list in `mock-api.py` is copied verbatim from
  `panel-do/main.go`'s registry — if the registry changes there, refresh it
  here (the Actions tab renders the names/summaries directly).
- The demo values (fleet, cards, containers) mirror the compose file and the
  gpu-pool ledger in this repo, so screenshots match what the real box shows.
