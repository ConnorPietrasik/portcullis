# Portcullis

A home-server admin panel: dashboard and whitelisted privileged actions —
behind Traefik + Authelia 2FA, on a CachyOS (Arch) box at `pietrasik.top`.
Also manages a Radiance GPU fleet (AMD AI PRO R9700 GPUs — chat LLMs today,
image/video generators in the same pool) and a NameSilo
DDNS timer for the dynamic public IP.

One gated entrance to the host (Traefik), one privileged path through
it (`panel-do`), everything else default-deny.

**`docs/RUNBOOK.md` and `docs/ARCHITECTURE.md` are the reference docs.**
RUNBOOK = phase-by-phase deploy, the UFW commands, updates, day-2 ops,
troubleshooting. ARCHITECTURE = *why* each trust boundary exists.

## Environment

- **This container** is a dev environment. No sudo, no GPU, no target
  host: `setup/*.sh` scripts assume the real server. You can edit, read, and
  run `make verify` here; the deploy targets can't run.
- The **repo is the single source of truth.** Everything deploys to the server
  one-way.

## Repo layout

| path | what |
|---|---|
| `setup/` | idempotent bash, numeric run order; `11-autoupdate.sh` installs the auto-update timer (disabled by default)
| `compose/` | edge stack → `/opt/panel`: `docker-compose.yml` (traefik, authelia, panel-web, portfolio, `gpu-*` fleet via the `x-gpu-base` anchor), `gpu-configs/` (per-instance radiance 2.x settings YAML — the server runs on `--config` only, no flags), `traefik/dynamic/*.yml` (routes — file provider, hot-reloaded), `authelia/configuration.yml` (+ `users_database.yml.example`), `panel-frontend/` (React+TS+Vite UI, multi-stage Dockerfile), `portfolio/` |
| `panel-api/` | FastAPI host service → `/opt/panel-api`, user `panel`, binds `172.17.0.1:8010` (docker0 gateway): `main.py` (metrics, containers, actions, LLM fallback router, gen job engine + `/api/gen/*`), `gpu_pool.py` (GPU pool: ledger/override/allocation + card reservations — the dedicated-card fence, e.g. the x8 card), `gen_jobs.py` (gen queue/settings logic: the gen-phase cap (per slot) + the enhance cap (on the target), slot discovery from radiance configs, the off-slot enhance target — default `gpu-writer`'s `writer` entry — artifact GC), `gpu_autostart.py` (boot oneshot), `panel-api.service` + `gpu-autostart.service` |
| `panel-do/` | Go whitelist dispatcher → `/usr/local/libexec/panel-do` — the ONLY privileged path (`sudo -n panel-do`) |
| `ddns/` | stdlib-only Python + systemd timer/service → NameSilo apex A record |
| `tools/panel-hosts.sh` | client-side /etc/hosts pin of routed names (no-hairpin workaround) |
| `docs/` | RUNBOOK (deploy/ops), ARCHITECTURE (trust boundaries) |
| `ref/` | gitignored external reference clones (e.g. Radiance source) — local scratch, never deployed |

## Security invariants — do not weaken

These are enforced by code and by review; changing any of them is a security
change, not a refactor. The "why" for each is in `docs/ARCHITECTURE.md`.

1. **panel-do** (`panel-do/main.go`): the whitelist is in the file itself —
   no host-side config to edit or weaken
2. **No docker.sock anywhere — not even `:ro`.** A ro-mounted socket still
   grants full Docker API access. Traefik (the only internet-facing process)
   runs the **file provider only**; routes live in `traefik/dynamic/*.yml`.
3. **API auth is double-gated and re-checked**: Traefik injects
   `X-Panel-Token` (overwriting anything a client sends) + Authelia
   forward-auth replaces `Remote-*` headers. `panel-api` re-verifies both
   (`secrets.compare_digest`, `admins` group). The LLM `/v1/*` router is
   gated by the bearer `LLM_API_KEY`. The gen endpoints (`/api/gen/*`) ride
   the same admin double gate — no new route/host/DNS/firewall line — and
   gen artifacts are served only from the panel host (`/gen/*`, ro bind into
   panel-web, nginx allowlisted to the `job-*` names); the V2 machine
   surface (`/machine/gen/*`, bearer-gated) is the sanctioned public shape
   for machine consumers — V2.
4. **Secrets**: one `.env` is the source of truth, feeding compose and
   the derived reduced env files (`/opt/panel/.panel-api.env`,
   `.panel-ddns.env` — root-owned 0600, regenerated every deploy, never
   hand-edit). Reduced because a panel-uid process's environ is readable via
   `/proc/<pid>/environ` — the full secret set must not enter it. The repo
   copy holds placeholders (`__PANEL_TOKEN__` etc.), never real values.
   `.env` is **never `source`d** as shell — `envload.sh` parses it literally
   and refuses anything docker compose/systemd would read differently.
5. **`PANEL_DEV=1` is local development only.** The systemd unit never sets
   it; nothing else should.
6. **Fleet naming contract**: `gpu-<name>` is 1:1 across traefik router,
   compose service, and container — any model class the pool serves (chat
   LLMs, image/video gen): the prefix names the POOL, not the workload.
   **Exception: a service carrying `gpu.private=true` is a fleet instance with
   NO public host** (gen slots): no router pair, no fleet-open host; reachable
   only via the edge network (panel-api talks to it by container IP, like the
   LLM router). The label asserts no-host both ways: `make doctor` fails a
   private service that HAS a router (drift) and exempts it from the
   router/fleet-open checks otherwise; gpu.cards/config/tp checks still apply.
   The exemption is the LABEL, not the workload — any future internal
   GPU-holding service rides the same class. Shared/non-instance routers are
   `fleet-*`, never `gpu-*` (`make doctor`'s orphan check assumes that
   prefix). `gpuServiceRe` (panel-do) and `GPU_NAME_RE` (panel-api) must stay
   identical. (The LLM fallback router, `LLM_API_KEY` and the `panel.category=LLM`
   label are LLM-scoped and keep their names.)
7. **Every compose render that can create/recreate a `gpu-*` container must
   include `-f /opt/panel-api/gpu-pool-override.yml`** (the GPU pool's pins) —
   `panel-do` actions and `setup/common.sh` already do; a render without it
   pins fallback cards and shows up as drift in the GPU view.
8. **The auto-update re-runs `setup/08-update.sh` verbatim — no second update
   path** (no git pull: it floats the lines and applies what the server clone
   holds). The `panel-do` `update-*` actions re-validate the self-registered
   clone (state file's `repo_root`) before every exec — mode/owner/shebang,
   walking the whole directory chain up to `/` (a writable ancestor dir =
   replaceable clone); don't `chown` the clone to root.
9. **Every root-exec'd script passes `checkManagedScript`** — the
   auto-update clone scripts (`setup/08|09-*.sh`), the servitor deploy
   scripts (`/opt/servitor-repo/deploy/sync|doctor.sh`, the `servitor-sync` /
   `-doctor` panel actions), and the `08-update.sh` servitor tail's set,
   root-exec'd outside panel-do — `deploy/update.sh` + its bash siblings
   `sync/doctor.sh` + `deploy/guest/build.sh` (the bash mirror
   `managed_script_check` in `setup/common.sh`) and the executed python
   `deploy/resolve_target.py` (file-level `managed_data_check`; its chain is
   the siblings') — every one: file + its dir + every ancestor to `/` not
   group/other-writable, not `panel`-owned, exec bit, bash shebang. A
   hardcoded-but-unvalidated script path is a replaceable root script; a
   writable checkout is a security incident, not a perms quirk.

## Workflows (repo edit → sync → RUNBOOK section for the details)

- **New app**: compose service on `edge`
  + router/service in `compose/traefik/dynamic/`; optional `panel.category` label
  for the Services view's collapsible group (absent → “Other”); optional Authelia rule in
  `configuration.yml` **above the wildcard** (first match wins; the
  bare-domain bypass must stay **last** so new subdomains are
  private-by-default). → `make update`. RUNBOOK → "Add an app".
  Machine-facing (key-gated, no Authelia) apps follow the SearXNG pattern — a
  `forwardAuth` key checker on panel-api, never a data-path proxy — RUNBOOK →
  "Web search (SearXNG)".
- **New privileged action**: one Go function + one registry entry in the
  `ACTIONS` section of `panel-do/main.go` → `make panel-do`.
  RUNBOOK → "Add a privileged action".
- **New fleet instance**: copy a `gpu-*` compose block + its radiance config
  in `compose/gpu-configs/` (keep `gpu.cards` == the config's `tp`, distinct
  display label; non-LLM model classes get their own `panel.category`) +
  its router pair in `traefik/dynamic/gpu.yml` + an `|| Host(...)` clause in the `fleet-open` rule —
  six name references total; `make doctor` flags any that drift (it also flags the
  v2-style multi-value `Host(a, b)` that Traefik v3 drops silently, and
  cross-checks `gpu.cards` against the config's `tp`). A private slot
  (`gpu.private=true` — invariant 6) is the same block + config with NO router pair and NO
  fleet-open clause — four references, and `make doctor` fails it if a router ever appears. →
  `make update`. RUNBOOK → "GPU fleet".
- **Servitor VM (day-2)**: four `servitor-*` panel actions (status/vm-
  restart/sync/doctor); the route (`traefik/dynamic/servitor.yml`, raw VM IP
  upstream — invariant 2: still a file, no docker.sock) + its second
  middleware (`servitor-edge` in `traefik/dynamic/middlewares.yml`, injects
  `X-Servitor-Edge-Key` from `SERVITOR_EDGE_KEY` — the edge credential the
  VM app re-verifies on every route; invariant 3 in the servitor repo) and
  the actions live in THIS repo; the scripts live in the separate
  `/opt/servitor-repo` checkout. `SERVITOR_EDGE_KEY` must be the SAME value
  in this repo's `.env` and `/opt/servitor-repo/.env` (a mismatch 403s the
  whole servitor UI — `make doctor` cross-checks them). Day-2 updates +
  image swaps are SSH-side (`make update` in that
  repo — self-gated, deliberately not a panel action) and ALSO run as the
  non-gating tail of portcullis's own `make update` / auto-update
  (`SERVITOR_UPDATE=0` to skip; a servitor failure never rolls back the host
  stack; a shut-off VM is left asleep). → RUNBOOK →
  "Servitor (agent VM)"; `make doctor` has a tri-state section for it.
- **After editing a route**: client machines pin names with
  `tools/panel-hosts.sh` (the server re-pins itself as part of `make update`).

## Gotchas

- The `edge` network is `external: true` and **auto-subnetted** at creation —
  derive the subnet wherever it's needed; never hardcode `172.18.0.0/16`.
- `make update` does not touch: majors in any line, `panel-do` / `panel-ddns`
  (stdlib-only — their update is a repo edit + their make target), the auto-update
  timer (`make autoupdate`), or host OS packages.
