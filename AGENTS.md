# Portcullis

A home-server admin panel: dashboard and whitelisted privileged actions —
behind Traefik + Authelia 2FA, on a CachyOS (Arch) box at `pietrasik.top`.
Also manages a Radiance LLM fleet (AMD AI PRO R9700 GPUs) and a NameSilo
DDNS timer for the dynamic public IP.

One gated entrance to the host (Traefik), one privileged path through
it (`panel-do`), everything else default-deny.

**`docs/RUNBOOK.md` and `docs/ARCHITECTURE.md` are the reference docs.**
RUNBOOK = phase-by-phase deploy, the UFW commands, updates, day-2 ops,
troubleshooting. ARCHITECTURE = *why* each trust boundary exists. Comments 
in the code carry security-critical rationale — read them, and update them 
when you change the behavior they explain.

## Environment

- **This container** is a dev environment. No sudo, no GPU, no target
  host: `setup/*.sh` scripts assume the real server. You can edit, read, and
  run `make verify` here; the deploy targets can't run.
- The **repo is the single source of truth.** Everything deploys to the server
  one-way.

## Repo layout

| path | what |
|---|---|
| `setup/` | idempotent bash, numeric run order
| `compose/` | edge stack → `/opt/panel`: `docker-compose.yml` (traefik, authelia, panel-web, portfolio, `llm-*` fleet via the `x-llm-base` anchor), `traefik/dynamic/*.yml` (routes — file provider, hot-reloaded), `authelia/configuration.yml` (+ `users_database.yml.example`), `panel-frontend/` (React+TS+Vite UI, multi-stage Dockerfile), `portfolio/` |
| `panel-api/` | FastAPI host service → `/opt/panel-api`, user `panel`, binds `172.17.0.1:8010` (docker0 gateway): `main.py` (metrics, containers, actions, LLM fallback router), `llm_gpus.py` (GPU pool: ledger/override/allocation), `llm_autostart.py` (boot oneshot), `panel-api.service` + `llm-autostart.service` |
| `panel-do/` | Go whitelist dispatcher → `/usr/local/libexec/panel-do` — the ONLY privileged path (`sudo -n panel-do`) |
| `ddns/` | stdlib-only Python + systemd timer/service → NameSilo apex A record |
| `tools/panel-hosts.sh` | client-side /etc/hosts pin of routed names (no-hairpin workaround) |
| `docs/` | RUNBOOK (deploy/ops), ARCHITECTURE (trust boundaries) |

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
   gated by the bearer `LLM_API_KEY`.
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
6. **LLM naming contract**: `llm-<name>` is 1:1 across traefik router,
   compose service, and container. Shared/non-instance routers are `fleet-*`,
   never `llm-*` (`make doctor`'s orphan check assumes that prefix).
   `llmServiceRe` (panel-do) and `LLM_NAME_RE` (panel-api) must stay
   identical.
7. **Every compose render that can create/recreate an `llm-*` container must
   include `-f /opt/panel-api/llm-gpu-override.yml`** (the GPU pool's pins) —
   `panel-do` actions and `setup/common.sh` already do; a render without it
   pins fallback cards and shows up as drift in the GPU view.

## Workflows (repo edit → sync → RUNBOOK section for the details)

- **New app**: compose service on `edge`
  + router/service in `compose/traefik/dynamic/`; optional `panel.category` label
  for the Services view's collapsible group (absent → “Other”); optional Authelia rule in
  `configuration.yml` **above the wildcard** (first match wins; the
  bare-domain bypass must stay **last** so new subdomains are
  private-by-default). → `make update`. RUNBOOK → "Add an app".
- **New privileged action**: one Go function + one registry entry in the
  `ACTIONS` section of `panel-do/main.go` → `make panel-do`.
  RUNBOOK → "Add a privileged action".
- **New LLM instance**: copy an `llm-*` compose block (keep `llm.cards` ==
  `--tp`, distinct display label) + its router pair in
  `traefik/dynamic/llm.yml` + its host in the `fleet-open` `Host()` list —
  five name references total; `make doctor` flags any that drift. →
  `make update`. RUNBOOK → "LLM fleet".
- **After editing a route**: client machines pin names with
  `tools/panel-hosts.sh` (the server re-pins itself as part of `make update`).

## Gotchas

- The `edge` network is `external: true` and **auto-subnetted** at creation —
  derive the subnet wherever it's needed; never hardcode `172.18.0.0/16`.
- `make update` does not touch: majors in any line, `panel-do` / `panel-ddns`
  (stdlib-only — their update is a repo edit + their make target), or host OS
  packages.
