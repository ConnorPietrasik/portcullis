# portcullis

Home-server admin panel: AMD temps/usage dials, container control, whitelisted privileged actions — behind Traefik + Authelia 2FA.

*The name is the architecture: one gated entrance to the host (Traefik), one privileged path through it (`panel-do`), everything else
default-deny.*

```
Traefik ─┬─ pietrasik.top            → portfolio container (public)
         ├─ panel.pietrasik.top/     → panel-web container (Authelia: admins)
         ├─ panel.pietrasik.top/api  → host API :8010 (Authelia + X-Panel-Token)
         ├─ auth.pietrasik.top       → Authelia portal
         └─ *.pietrasik.top          → future apps (per-group rules, private by default)

host API (user 'panel', systemd) ─┬─ /sys + /proc       → AMD temps/util/VRAM, RAM, load (read-only)
                                  └─ sudo -n panel-do   → compiled whitelist dispatcher — the ONLY
                                     privileged path: container list/start/stop/restart,
                                     systemd restarts, docker prune

panel-ddns (systemd timer, user 'panel') ─→ NameSilo API — the apex A record
   follows the public IP; *.pietrasik.top is a CNAME → apex, so it follows for
   free (reuses the cert's DNS-01 API key; no new secrets)

Traefik runs the file provider only (traefik/dynamic/*.yml) and mounts no
docker.sock — the internet-facing process gets zero docker access.
```

## Repo layout

| path | what it is |
|---|---|
| `setup/` | idempotent setup scripts, run in order (see below) |
| `compose/` | the edge stack deployed to `/opt/panel` (traefik file-provider only, authelia, panel UI) |
| `panel-api/` | FastAPI host service → `/opt/panel-api`, run by `panel-api.service` |
| `panel-do/` | Go whitelist dispatcher — add actions per [RUNBOOK](docs/RUNBOOK.md#add-a-privileged-action) |
| `ddns/` | stdlib-python systemd timer → keeps the apex NameSilo A record current |
| `docs/RUNBOOK.md` | phase-by-phase deploy + verify + hardening guide |
| `docs/ARCHITECTURE.md` | why it's built this way, trust boundaries |

## Quickstart (on the server)

**Main target: CachyOS** (Arch-based; UFW ships installed and enabled, so the deploy only adds its two rules and never touches SSH).
Any other Arch-based distro works: `pacman -S ufw` and enable it before Phase 2 (RUNBOOK → UFW).

**[docs/RUNBOOK.md](docs/RUNBOOK.md) is the deploy guide — follow it in order** (phase 0 is manual DNS prep; phase 5 is the final check
from an external network). It is the single source of truth for the sequence:

- Phase 0 is manual: NameSilo API key + the one-time `*` CNAME → `pietrasik.top`.
- **UFW is a required step between `make deploy` and `make api`** (RUNBOOK → UFW). `make api` refuses to run until it is in place.

```bash
make preflight   # phase 1: host tooling
make secrets     # phase 1: .env (then fill NAMESILO_API_KEY + users_database.yml)
make deploy      # phase 2: edge stack → /opt/panel (staging cert → auto-promotes to prod)
make ddns        # phase 2: apex A record self-creates
make panel-do    # phase 3: privileged dispatcher
make api         # phase 4: host API (gated on UFW)
make doctor      # any time: whole-stack pass/fail report (read-only)
```

`make stack` = `deploy` → `ddns` → `panel-do` → `api` in one shot, for **re-runs**; the first deploy follows the RUNBOOK step by step so
UFW lands in the window. First login: `https://auth.pietrasik.top` (registers TOTP), then `https://panel.pietrasik.top`.

## Local dev (no deploy)

```bash
# on the server, in one shell:
cd /opt/panel-api && PANEL_DEV=1 venv/bin/uvicorn main:app --port 8010   # ⚠ auth disabled, loopback only
# in the repo:
cd compose/panel-frontend && npm install && npm run dev   # vite proxies /api → localhost:8010
# (npm install, not npm ci: the lockfile is gitignored server-local state — a fresh clone has none)
```

Never set `PANEL_DEV=1` in production — the systemd unit doesn't, and nothing else should.

## Day-2 ops

- **Stack updates**: `make update` is the one Day-2 command — it syncs every repo change (compose, routes, Authelia config, `.env`, the
  panel frontend) and floats the stack within its declared *lines* (major image tags, npm/pip ranges). It requires a green `make doctor`
  up front, snapshots everything, changes only what moved, gates on post-update health + panel-api + a full doctor pass, and
  auto-rolls-back on failure (offline — the snapshot is local). `make rollback` undoes the last update. Majors stay deliberate edits
  (a tag in compose, a range in `package.json`/`requirements.txt`). `make deploy` is the first-boot path.
  Details: [RUNBOOK](docs/RUNBOOK.md#updates)
- **New app**: compose service on `edge` + router in `compose/traefik/dynamic/` + optional Authelia rule, then `make update` → [RUNBOOK](docs/RUNBOOK.md#add-an-app)
- **New privileged action**: edit `panel-do/main.go` ACTIONS section → `make panel-do` → [RUNBOOK](docs/RUNBOOK.md#add-a-privileged-action)
  (container control is registry actions too — Services and Actions views both render from `panel-do --list`)
- **Rotate secrets**: edit `.env`, `make update` (it re-derives the panel-api + panel-ddns env files — panel-api is restarted, panel-ddns
  picks the new value up on its next tick)
- **Logs / status**: `make logs`, `make status`

## License

[GPL-3.0](LICENSE) © Connor Pietrasik
