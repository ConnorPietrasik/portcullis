# Demo: UI with mock data

Load the real panel UI against a mock API and screenshot every tab — for docs,
design review, or checking a UI change without the server, Docker, or GPUs.

```
tools/demo/
├── mock-api.py   # mock of panel-api on 127.0.0.1:8010 (stdlib only, no deps)
├── shot.mjs      # headless-Chromium screenshots (Node ≥ 22) — a plain tab tour or a scenario
├── scenarios/    # scripted walks for `shot.mjs --scenario` (the shipped ones: gen, actions, aspect)
└── screens/      # the captured PNGs
```

## Run

```sh
# 1. mock API — serves the /api read shapes (metrics, containers, gpus, gen,
#    settings/llm, update, …) as bounded random walks around a snapshot of the
#    live box: Threadripper, 7× AI PRO R9700 (four saturated chat cards, two
#    idle gen slots, one reserved x8 card), 13 containers, the real action
#    registry, the gen slot layout. The gen section is a real (tiny) job
#    store: queued jobs walk the state machine on the poll clock, finished
#    ones own generated artifacts under /gen/, and create/delete/archive
#    behave like the real endpoints.
python3 tools/demo/mock-api.py          # http://127.0.0.1:8010

# 2. UI — the existing dev loop; Vite proxies /api → localhost:8010
cd compose/panel-frontend && npm run dev   # http://localhost:5173

# 3. screenshots (optional; 35 s warm-up so the traces fill)
#    Plain tour: clicks each tab; <tab>.png is the 1920x1080 viewport (the
#    second-monitor view), <tab>-full.png the full page, only when it is taller.
node tools/demo/shot.mjs                  # → tools/demo/screens/*.png

# Scripted walks instead of the tour — a scenario is a JSON array of steps
# {name, tab?, then?, wait?, full?}: `tab` clicks a nav tab, `then` runs a page
# script first (click a card, open a modal…), `wait` is the settle in ms, `full`
# captures the whole page. The shipped ones:
node tools/demo/shot.mjs --scenario=tools/demo/scenarios/gen.json
#    gen — the composer → gallery (select all, a job modal, settings + prompts tabs)
#    actions — the workbench (a run, the confirm dialog, an arg picker)
#    aspect — the composer's frame retune across image/video

# Options: --tabs=Gen,Actions (which tabs, default all six) · --warm=35 (warm-up
# seconds, 0 = skip) · --pre=file.js (one page script after warm-up, before the
# walk) · --scenario=file.json (above; --tabs is then ignored).
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
