# Runbook

Phase-by-phase deploy with a verification step after each. Each phase is independently testable — if something breaks, you know which layer.

On the server, `make doctor` runs every server-side check below as a single pass/fail report (read-only) — handy after any phase,
after `make deploy`, or whenever something looks off. (Phase 5 is by definition from an external network, so it stays manual.)

## Phase 0 — NameSilo DNS prep (one-time, manual)

1. NameSilo → Domain Manager → **API** → generate an API key. Keep it handy — Phase 1 pastes it into `.env`.
2. NameSilo → DNS → add a `*` CNAME → `pietrasik.top`. The apex A record is created automatically by `make ddns` in Phase 2 (it needs
   `/opt/panel/.env` from `make deploy`, so it can't run earlier).

   **Why the stack sets `LEGO_DISABLE_CNAME_SUPPORT=true` (traefik env in `compose/docker-compose.yml`):** this CNAME makes
   `_acme-challenge.pietrasik.top` a wildcard-synthesized CNAME, which lego (Traefik's ACME client) would follow while solving DNS-01 and
   misplace the challenge TXT (go-acme/lego#1816). With the env var set, the TXT always lands at `_acme-challenge.pietrasik.top` itself,
   where the explicit record takes precedence over the wildcard (RFC 4592) — both for issuance and every renewal. Do not remove it.

That's the whole phase. The wildcard cert comes from the DNS-01 challenge, which needs no A records at all. `panel-ddns` keeps the apex A
(`pietrasik.top`) current forever after, and the reconciler never touches CNAMEs, so every subdomain follows the apex A automatically. If
the CNAME is ever deleted, re-create it by hand.

## Phase 1 — Tooling + secrets

```bash
make preflight      # docker, compose plugin, go, ufw, python3, ports 80/443 free
make secrets        # writes .env (chmod 600)
```

Then edit `.env`: paste the `NAMESILO_API_KEY` from Phase 0 (`ACME_EMAIL` comes pre-filled — change it if you want Let's Encrypt notices
elsewhere). Values are read literally as `KEY=VALUE` lines — no shell execution — and what the deploy scripts use is exactly what the
containers and the systemd units receive, so keep values free of `$`, backslashes and ` # ` (docker compose and systemd reinterpret those,
so `make deploy`/`make doctor` refuse them with a clear message; single quotes make a value fully literal). Values with spaces work
unquoted, but wrap them in a single pair of quotes to be safe. An `export ` prefix is tolerated on container-only keys (docker compose
strips it), but refused on `NAMESILO_API_KEY`, `PANEL_TOKEN` & co. — systemd drops such lines and panel-ddns/panel-api would silently lose
the value. A bare `KEY` line without `=` only warns (systemd and the deploy scripts ignore it; docker compose would inherit it only from
its own environment); any other stray text is refused — docker compose would reject the file at `up` time, or (for yaml-style `KEY:value`)
parse it as an assignment systemd would ignore.

Create `compose/authelia/users_database.yml` from the template — the real file is gitignored, so password hashes never reach GitHub (a fresh
clone won't have it):

```bash
cp compose/authelia/users_database.yml.example compose/authelia/users_database.yml
```

Then for each user generate a hash

```bash
docker run --rm authelia/authelia:4 authelia crypto hash generate argon2 --password 'YOUR-PASSWORD'
```

and paste it over `REPLACE_WITH_ARGON2_HASH`. Groups: `admins` = you, `friends` = shared-app users.

## Phase 2 — Edge stack

```bash
make deploy         # → /opt/panel, docker compose up -d --build
make ddns           # installs panel-ddns (needs /opt/panel/.env) — apex A self-creates
make logs           # watch: traefik acme lines = cert issuance (NameSilo TTLs are slow — minutes, not seconds)
sudo docker compose --project-directory /opt/panel logs authelia | grep -i error
```

**UFW (required step — do it now)** — add the 8010 rule before `panel-api` exists: `make deploy` just created the `edge` network the
8010 rule is scoped to, and having it in place before `panel-api` binds 8010 in Phase 4 means 8010 is never reachable from outside the
edge subnet, even briefly. UFW is already active and your SSH rules are untouched — the deploy adds only the
two rules at [UFW](#ufw-required), never an SSH one; note the 401-JSON verify there only passes once
`panel-api` is up (Phase 4). `make doctor`
fails while UFW is inactive or the 8010 rule is missing, and `make api` refuses to run until that is true — so a first deploy can't skip
this step by accident (a first `make stack` run stops at panel-api until you do it).

**First bring-up runs on the staging ACME directory** (`.env.example` defaults `ACME_CA_SERVER` to it): debugging the NameSilo DNS-01 dance
against production risks the 5-failures/hour rate limit, staging doesn't. `make deploy` then waits up to 10 min (`ACME_STAGING_WAIT` in
`.env`) for the staging cert and promotes to production automatically: traefik stops, the ACME store is wiped (the staging account URI
doesn't exist on prod — reusing it fails JWS verification; staging certs are worthless anyway), the config flips, traefik starts and issues
the real cert. If the wait times out, `make logs` until the staging cert appears, then finish with:

```bash
make promote   # idempotent; also safe to just re-run make deploy
```

Verify (subdomains resolve via the `*` CNAME → apex A; if the apex A was just created by `make ddns`, give resolvers a minute):
- `curl -I https://auth.pietrasik.top` → 200 (valid prod wildcard cert; an *untrusted* staging cert means promotion hasn't run yet —
  `make promote`)
- `curl -I https://pietrasik.top` → 200 (portfolio placeholder — public, no auth)
- `curl -I https://panel.pietrasik.top` → redirects to Authelia login (no user yet)

First login: open `https://auth.pietrasik.top`, log in, register TOTP when prompted. Then `https://panel.pietrasik.top` should show the
dashboard shell (API calls will fail until Phases 3–4 are done — expected).

## Phase 3 — panel-do (privileged dispatcher)

```bash
make panel-do
```

The script builds (`gofmt`/`go vet` checked), installs to `/usr/local/libexec/panel-do` (root-owned, `0755`), writes `/etc/sudoers.d/panel`, and
runs the negative-test suite (unknown action → 64, path/flag injection → 65). Re-run any time after editing `panel-do/main.go` — it's
idempotent.

## Phase 4 — panel API

```bash
make api
```

Verify from the server:

```bash
# The API re-verifies the token AND the Remote-User/Remote-Groups headers
# Authelia sets, so curls direct to the bridge must supply both (fake them:
# only 172.17.0.1 is reachable, and traefik overwrites client-supplied ones).
# /api/health is deliberately open (liveness-probe pattern) — verify auth
# against /api/metrics.
TOKEN=$(sudo grep '^PANEL_TOKEN=' /opt/panel/.env | cut -d= -f2)
AUTH=(-H "X-Panel-Token: $TOKEN" -H "Remote-User: connor" -H "Remote-Groups: admins")
curl -s http://172.17.0.1:8010/api/health                # {"ok":true,...} — open liveness endpoint
curl -s http://172.17.0.1:8010/api/metrics               # 401 (expected — auth enforced)
curl -s "${AUTH[@]}" http://172.17.0.1:8010/api/metrics  # GPUs/CPU/RAM/disks JSON
```

Disk dials: one per mount in `PANEL_DISK_MOUNTS` (`.env` — this server: `/`, `/home`, `/models`). A mount that isn't there (partition failed
to mount, path missing, pseudo-fs) shows as a red ERROR card on the dashboard, never as another filesystem's numbers or a silent gap — check
`journalctl -u panel-api` for the matching line.

Then from a browser logged into Authelia: `https://panel.pietrasik.top` → Overview dials live, Actions lists the registry.

## Phase 5 — trust, but verify (from an external network)

- [ ] panel behind 2FA, only `admins` group works
- [ ] `curl -H "X-Panel-Token: <wrong>" https://panel.pietrasik.top/api/health` from outside → Authelia login page (never reaches API)
- [ ] demos/apps you mark public open without an Authelia prompt
- [ ] `sudo -n /usr/local/libexec/panel-do restart-service foo.service` as a non-panel user → password prompt/denied

## Dynamic IP

The server's IP isn't static. `panel-ddns` (systemd timer, user `panel`, stdlib python, outbound only, no root) reconciles the apex NameSilo
A record declaratively every 10 minutes: detect public IP → list records → update stale ones, create missing ones. CNAMEs at configured
hosts are skipped (`*.pietrasik.top` is a CNAME → apex, so it rides along) — never overwritten. No state file, no short-circuit — whatever
drifted (a changed IP, a record deleted by hand, an IP that flipped and flipped back between ticks) is fixed within one tick. Cost is one
list call per tick (~6/hour — trivial). TTL is pinned at 3600 (NameSilo's minimum), so worst-case cutover after an IP change ≈ 10 min + 1 h
of resolver caching. Reuses `NAMESILO_API_KEY` — the same key Traefik uses for DNS-01. The key reaches
the oneshot via a reduced env file (`.panel-ddns.env` — only that key + `DDNS_HOSTS`), so the full
`.env` secret set never sits in the oneshot's environ while it runs as the panel uid.

```bash
journalctl -u panel-ddns -n 50                  # what it did and when
sudo systemctl start panel-ddns.service         # reconcile now — also the manual run (the script
                                                #   FATALs if run directly: the API key only comes
                                                #   from the unit's EnvironmentFile)
dig +short pietrasik.top @ns1.dnsowl.com        # what the authoritative NS says (check NS at NameSilo)
```

## Updates

`make update` is the one Day-2 command: it applies the repo's configs **and** floats everything that has a *line*:

| Component | Line | A breaking major is a deliberate edit of… |
|---|---|---|
| traefik, authelia, portfolio | major tags: `traefik:v3`, `authelia/authelia:4`, `nginx:stable-alpine` | the tag in `compose/docker-compose.yml` |
| panel-web base images | `node:22-alpine`, `nginx:stable-alpine` | the `FROM` lines in `compose/panel-frontend/Dockerfile` |
| panel-web npm deps | the ranges in `package.json` (e.g. `react ^18.3.1`) | a range in `compose/panel-frontend/package.json` + commit |
| panel-api pip deps | the ranges in `requirements.txt` (e.g. `fastapi>=0.115,<1`) | a range in `panel-api/requirements.txt` + commit |
| stack configs + secrets | whatever is committed in this repo (compose, routes, Authelia ACLs, `.env`) | — the repo edit **is** the change (holding a line = a full-version pin, e.g. `traefik:v3.7.9`) |
| radiance (LLM fleet) | `latest` — the one component deliberately OUTSIDE the declared-line policy (frequent builds wanted) | the tag in `x-llm-base`; a build that breaks behavior (gates only see health) → `make rollback WHAT=radiance` |

Patch/minor security releases within those lines ride in on every update. The internet-facing stack should
never run stale, so updates are routine and *safe by construction*:

```bash
make update      # doctor precondition (aborts before touching anything if not green)
                 # → snapshot everything (image IDs, deployed configs, exact panel-api copy, npm files)
                 # → npm bump (in-repo, within ranges) → sync configs from the repo → pull → rebuild (bases pulled)
                 # → recreate what changed (+ restart on start-time config change) → update panel-api
                 # → gate: pre-update services healthy + panel-api serving + full doctor pass
                 # → on any failure: automatic rollback of all of it, re-gated
make rollback              # on demand: undo the last `make update` (same machinery)
make rollback WHAT=radiance  # or: images ONLY of the services WHAT matches — a name (llm-27b), a
                             # prefix (llm), or an image name (radiance); everything else stays
```

How it works (`setup/08-update.sh`, shared helpers in `setup/common.sh`):

- **Snapshot** = `/opt/panel/.update-snapshot-<ts>` (state in `/opt/panel/.update-state`): the exact
  *image IDs* of what is running (tags move, IDs don't), re-tagged `portcullis-rollback/<svc>:<ts>`;
  **the deployed config tree** (`/opt/panel`: docker-compose.yml, traefik/, portfolio/, the authelia
  configs, panel-frontend, `.env`, the reduced env files — a tarball; runtime data like `db.sqlite3` is
  excluded); an **exact copy of `/opt/panel-api` (venv included)**; and the npm `package.json` + the
  server's `package-lock.json` (gitignored server-local state — snapshotted when it exists) about to be
  bumped. A failed npm bump, config sync, or pull stops the run *before* any container is touched.
- **npm bump** = `npm update --package-lock-only` in `compose/panel-frontend` — floats every dependency within the range
  declared in `package.json` (the npm analogue of the image policy: a MAJOR is a deliberate range edit).
  It updates the `package-lock.json` to the newest versions within the declared ranges — the
  `package.json` ranges are what bound the bump and stay untouched. The lockfile is **gitignored
  server-local state**, deliberately not a repo artifact: it keeps `git pull` conflict-free between
  updates and stops publishing the exact resolved versions. A fresh clone has no lockfile — the first
  update generates it from the ranges, and the Docker build falls back to `npm install` until then
  (on the server, builds use it via `npm ci`). Rollback restores the pre-update files from the
  snapshot; only a deliberate range edit in `package.json` is a git commit.
- **configs** = the same repo → `/opt/panel` sync `make deploy` performs (compose + traefik + authelia
  + portfolio + `.env`, secrets substituted, stale route files pruned, the panel-uid services' reduced
  env files re-derived). A committed config change no longer waits for a deploy, and a `.env` edit is applied here
  too. authelia + traefik read some files only at container start — those services are restarted after
  the recreate **only if their content changed** (a routine update adds no such downtime). A sync failure
  aborts before any container changes.
- **panel-web** = built from the synced build context (committed frontend code + the server's current
  lockfile ship together), with `--pull` so its `FROM` bases float too — without `--pull`, BuildKit
  would reuse the local bases from the first build. A build/base-pull failure aborts before any
  container changes.
- **Recreate** = `up -d` converges only what should be running after the update: the pre-update
  running set, plus services this update *added* (the “add an app → `make update`” flow — the new
  service starts). A service the operator deliberately *stopped* stays stopped (and out of the gate
  below) — an update doesn't resurrect it; a bare `make deploy` (ungated) still starts everything.
  Services *removed* from compose have their containers dropped by the recreate, and a rollback
  brings them back.
- **panel-api** = `main.py` + `requirements.txt` + the systemd unit re-deployed, `pip install --upgrade`
  within the ranges in `requirements.txt`, service restarted, then verified serving (`/api/health`) before
  the gates. (The first-install UFW gate in `make api` is deliberately not re-run — on an already-deployed
  server a firewall regression surfaces in the doctor gate → auto-rollback. `.env` edits are applied by
  the config sync above, which re-derives both reduced env files — panel-api's is applied by this
  restart, panel-ddns's on its next tick.)
- **Gate** = every service that was *running before the update* healthy again (compose healthchecks,
  `UPDATE_HEALTH_TIMEOUT=600`s — a deliberately stopped service can't fail the gate, and neither can
  one this update *removed* from compose: its container is already gone by then), then a full
  `make doctor` pass (`UPDATE_DOCTOR=0` to skip both). Doctor is also enforced as a *precondition*:
  the update aborts before touching anything if `make doctor` is not green — a pre-existing doctor
  failure would otherwise auto-revert every update.
- **Rollback** = restore the pre-update config tree first (so the recreate below reads the pre-update
  compose file + `.env`), then recreate the snapshot's images via a temporary compose override (no edits
  to the compose file for the image swap; `--remove-orphans` drops containers for services that the
  update added), restore the exact panel-api copy, restore the npm files — all from **local** snapshots,
  so it needs no registry access: it works on a dead connection. A gate failure triggers it automatically;
  a component that fails to revert is reported as a PARTIAL rollback (the rest is reverted — fix and
  re-run `make rollback`). With WHAT (`make rollback WHAT=radiance`) it recreates ONLY the matching
  services' images — matched against the service name (exact or prefix) or the pre-update image name
  (substring, recorded in the snapshot) — and leaves the config tree, panel-api, npm files, and every
  other service in place. That is the escape hatch for a floating image (radiance rides `latest`) that
  passed the gates but broke behavior; the next `make update` re-anchors the snapshot on the reverted
  build.
- **Runtime data is outside the snapshot**: the rollback restores images, the config tree, the panel-api
  copy, and the npm files — not runtime state. The one place that matters is authelia: it migrates its sqlite DB in
  `/opt/panel/authelia/` at startup across minor releases, so a rolled-back image boots against the
  *already-migrated* DB. Older 4.x images read the newer schema fine in practice, but if a migration ever
  breaks a rolled-back image, the DB — not the image — is what you restore: back up `/opt/panel/authelia/`
  (db.sqlite3 + users_database.yml) before an update you don't fully trust.
- After a *successful* update the state file still holds the pre-update snapshot, so one `make rollback`
  undoes the last update; the next `make update` replaces the snapshot. Snapshots (rollback tags + dirs,
  including the ~100 MB panel-api venv copy) are GC'd after 14 days.
- `make deploy` is the **first-boot** path (network, secrets, ACME staging→prod, first build). It does
  **not** pull — with floating tags a pull is an upgrade, so image movement only happens through
  `make update` (gated). A Day-2 re-run of it applies config ungated and warns — `make update` is the
  Day-2 path for everything.

Escapes:

- A breaking major you didn't want (any line — image tag, `FROM`, npm/pip range): `make rollback`, then pin
  it in the repo (e.g. `traefik:v3.7.9`, or the old range back in `package.json`/`requirements.txt`),
  `make update`. (Re-running `make update` without the pin would just retry the same major.)
- A floating image that passed the gates but broke behavior (radiance rides `latest` — the gates only
  see health, not behavior): `make rollback WHAT=radiance` (images only — configs, panel-api, and every
  other service stay in place). Then either pin the good build's tag in `x-llm-base` + `make update`
  to hold it, or just `make update` again to re-try `latest` (the snapshot re-anchored on the
  reverted build in the meantime).
- Update interrupted mid-run (Ctrl-C, power loss): the snapshot + rollback tags survive on disk; `make
  rollback` restores everything, or just re-run `make update`.

What `make update` does **not** touch — by design:

- **Majors** in any line: deliberate edits (a tag, a `FROM`, a range).
- **panel-do / panel-ddns**: no external dependencies (stdlib-only Go / Python) — nothing to float. Their
  update is a repo edit + `make panel-do` / `make ddns`.
- **Host OS packages**: `sudo pacman -Syu` on your normal schedule.

## Add an app

Two edits in this repo, then `make update`. Traefik is file-provider only — it never reads docker labels, so every app needs a route in
`traefik/dynamic/`:

```yaml
# compose/docker-compose.yml — the service, on the edge network
  jellyfin:
    image: jellyfin/jellyfin:10   # major-line float, like the edge stack (RUNBOOK → Updates)
    restart: unless-stopped
    networks: [edge]
    labels:
      - panel.manage=Jellyfin        # shows up in the panel's Services view (optional)
```

Tag it at the major line (`:10`, not `latest` or a full version): `make update` then floats it with the rest of
the stack (snapshot + gate + auto-rollback) and never crosses a major on its own.

```yaml
# compose/traefik/dynamic/apps.yml — the route (one file can hold many apps;
# the file provider watches this directory — no traefik restart needed)
http:
  routers:
    jellyfin:
      rule: "Host(`jellyfin.pietrasik.top`)"
      middlewares: [auth-chain@file]   # sec-headers@file instead for public apps
      service: jellyfin
  services:
    jellyfin:
      loadBalancer:
        servers:
          - url: "http://jellyfin:8096"
```

`make update` (syncs configs + recreates what changed, gated + auto-rollback). Default policy: any
authenticated user. Add an Authelia rule in `compose/authelia/configuration.yml` **above the wildcard**
only if it needs group restriction or a public bypass — `make update` restarts authelia whenever
`configuration.yml` or `users_database.yml` content changes (both are read only at container start, no
hot reload), so no manual recreate step is needed.

## LLM fleet (Radiance instances)

[Radiance](https://codeberg.org/StillDeadcode/radiance) instances on 6 shared R9700s; start/stop
from the panel's Services view. Machine APIs, not browser apps: no Authelia — the gate is the
server's own API key (`Authorization: Bearer`) behind the wildcard cert + `llm-rate`; clients use
`base_url=https://<name>.pietrasik.top/v1` + key, exactly like OpenAI.

**An instance = a service block + a router pair**, deployed by `make update`: `llm-<name>` in
`compose/docker-compose.yml` (merges `x-llm-base`) ↔ `Host(`<name>.pietrasik.top`)` →
`http://llm-<name>:8000` in `traefik/dynamic/llm.yml`. One subdomain per instance so agents pin
the right server; the wildcard CNAME + cert make that zero DNS work. Port is always 8000 (own IP
per container). The display name is the `panel.manage` label — **no commas** in it (`docker ps`
encodes labels comma-joined and the Services view parses them that way: `cards 0+2+5`).

**Cards:** `ROCR_VISIBLE_DEVICES: "i,j,k"` (indices are fixed 0–5; per-card VRAM shows in the
panel's GPU stats) + `--tp` = card count in the
set. Everything else in `x-llm-base` is identical for all instances.

**`restart: "no"` on purpose:** after a reboot nothing LLM comes back by itself — you start the
set you want from the panel. `make update` still starts new/changed instances and leaves
deliberately-stopped ones down (and out of the health gate).

**Add an instance:** clone a block (name, model, cards, flags) + the router pair, then `make update`
(first load ~1 min on this hardware, shown in Services). The flags in the committed blocks are
this host's own tuning — the blocks are the truth, do not reconcile them against upstream. To
trial a NEW model first: `git clone --depth 1` upstream, run its `deploy/compose/<model>.yaml`
ad hoc (one at a time), delete the clone — its flags belong to the trial, not to a fleet block.

**Key:** one shared `LLM_API_KEY` for all instances (a key authorizes LLM use, not one instance).
Rotate = edit `.env` + `make update` (radiance reads it at startup).

**Updates:** a config-only update recreates nothing. When an update DOES recreate an instance
(radiance moved on `latest`, block edited), its model reload (~1 min on this hardware) sits
inside the 5m healthcheck start_period, and the default 600s budget (`UPDATE_HEALTH_TIMEOUT`)
covers both the update gate and the rollback re-gate — they share it — raise it only for a much
slower model. A new build that is *healthy but behavior-breaking* is different — the gates can't
see it: `make rollback WHAT=radiance` returns just the instances to the pre-update build (re-gated
on the same budget; configs and the rest of the stack stay in place), and the next `make update`
re-anchors the snapshot on it.

**Models:** `/models/llm/*.rad` (self-contained; ro-mounted at `/models`). If `/models` is btrfs
with compression, keep `.rad` files uncompressed (`chattr +C`) — direct-I/O n-gram reads degrade
otherwise.

**Later — dynamic GPU allocation:** the pin is just env at start time, so the end state is a
panel-do action (ledger from running instances' env, conflict check, write the new set, recreate)
+ a six-slot free/held UI; the file's pins become last-allocation defaults.

## Add a privileged action

One Go function in the ACTIONS section of `panel-do/main.go` + one registry entry, then `make panel-do`. The UI picks it up from `--list`
within ~60s; the API needs no change. The rules are in the comment block at the top of the ACTIONS section in `main.go` (violations =
security bug) — in short: exec only via `runCmd(ctx, "/absolute/path", …)`; args arrive pre-validated as opaque tokens and are never file
paths; no `os.Getenv`, no network, no goroutines; separators like `--` are hardcoded inside `runCmd` calls. Before deploying, check:

- [ ] `git diff` shows changes only inside the ACTIONS section
- [ ] every `Run` calls only `runCmd` — no direct `exec.Command`, no `os.Open`/`os.Create`/`os.Getenv`, no goroutines, no network imports
- [ ] all regexes anchored (`^…$`), arity exact, no optional/variadic args
- [ ] no action treats an arg as a path or places an arg before a hardcoded `--`
- [ ] timeout sane for the action; destructive ones flagged `Confirm: true`
- [ ] `make panel-do` passes (gofmt + `go vet` checks + negative-test suite)

## Hardening checklist

- [ ] The clone stays the source of truth — edit tracked files in the repo (commit before `make update` for a rollback point),
  never hand-edit the deployed copies: updates sync repo → `/opt/panel` one-way (stale managed traefik configs get pruned).
  `.gitignore` already keeps `.env`, built binaries, and caches out of git.
- [ ] Persistent journald — panel-do's audit trail lives there: `sudo mkdir -p /var/log/journal && sudo systemctl restart systemd-journald`
  (skip if `/var/log/journal` already exists).
- [ ] SSD trim: `sudo systemctl enable --now fstrim.timer` (weekly; HDDs don't need it).
- [ ] UFW active and configured per [UFW](#ufw-required) below — **required on this host** (no firewalld/iptables fallback); only 80/443
  public; `172.17.0.1:8010` never reachable externally.
- [ ] No container mounts `/var/run/docker.sock` — not even `:ro` (a ro-mounted socket still grants full Docker API access). Traefik runs
  the file provider precisely so it needs none.
- [ ] `chmod 600 /opt/panel/.env /opt/panel/.panel-api.env /opt/panel/.panel-ddns.env /opt/panel/traefik/dynamic/middlewares.yml /opt/panel/authelia/users_database.yml` (deploy script does this — verify after manual edits;
  `.panel-api.env` / `.panel-ddns.env` are derived from `.env` — regenerate with `make api` / `make ddns`, never hand-edit).
- [ ] `/opt/panel/authelia/db.sqlite3` mode 600 (TOTP seeds — authelia creates it 0644 at first boot; `make deploy` tightens it in the same run once it exists, subsequent `make deploy`/`make update` re-tighten it, `make doctor` checks it).
- [ ] CrowdSec + Traefik bouncer plugin (optional, later).
- [ ] SSH keys-only as break-glass access — and still reachable under UFW (your existing SSH rule; the deploy
  never touches SSH rules — see [UFW](#ufw-required)). Active UFW + no SSH rule = console-only.
- [ ] Backups: `/opt/panel/.env`, `compose/` (this repo), Authelia `/opt/panel/authelia/` (config + `db.sqlite3` — TOTP seeds!), the `acme`
  docker volume, and this git repo.

## UFW (required)

This host always runs UFW — CachyOS (the target OS) ships it installed and enabled, so the deploy never activates it; the deploy step
is just adding the two rules the stack needs. If `ufw status` reports inactive: run `sudo ufw enable` first — rules added while inactive
are stored and load once it activates. `make doctor` fails while UFW is inactive or the 8010 rule is missing, and `make api` refuses to
bind 8010 until both are true. **The deploy adds exactly two rules (80/443 + the 8010 rule) and never touches SSH** — your existing
SSH policy (port, source scope) stays as-is.

```bash
sudo ufw status verbose    # confirm active; eyeball your SSH rules (the deploy never touches them)

# Rule 1: web (TCP only — 443/udp is HTTP/3, not served here):
sudo ufw allow 80,443/tcp comment 'traefik web'
# Rule 2: panel-api, scoped to the edge network's ACTUAL subnet (source) — Docker assigns
# it from a pool that depends on which subnets are already taken (a fresh
# daemon starts at 172.18.0.0/16); derive, don't guess. The destination is
# pinned to panel-api's bind IP (keep in sync with panel-api.service):
EDGE_SUBNET=$(sudo docker network inspect edge -f '{{(index .IPAM.Config 0).Subnet}}')
sudo ufw allow from "$EDGE_SUBNET" to 172.17.0.1 port 8010 proto tcp comment 'panel-api from edge network'

sudo ufw status verbose    # both rules present; SSH rules unchanged
```

Two gotchas:

- **Docker-published ports bypass UFW.** 80/443 are open via Docker's NAT rules regardless of ufw; the real access control for containers is
  what you put in `ports:`. Nothing in this stack publishes a port besides Traefik — keep it that way, and never publish the API beyond
  loopback/bridge IPs.
- **The 8010 rule is required**, not optional: container → host traffic hits UFW's INPUT chain, and `default deny incoming` turns the panel
  into 502s. Scoped to the edge network's own subnet and to 172.17.0.1, the internet can't touch it.

Verify after adding the rules (want: 401 JSON = reachable, not a timeout; then refused from outside):

```bash
docker run --rm --network edge curlimages/curl -s -m 5 http://172.17.0.1:8010/api/metrics  # 401 JSON = reachable, auth enforced
curl -m 5 http://SERVER_IP:8010/api/health                                                  # refused from outside
```

If you later enable HTTP/3: add `http3: {}` to the `websecure` entryPoint in `traefik.yml`, add `"443:443/udp"` to traefik's ports in
docker-compose.yml, and `sudo ufw allow 443/udp`.

## Troubleshooting

| symptom | likely cause / fix |
|---|---|
| cert never issues | wrong `NAMESILO_API_KEY`; DNS records missing; propagation — `make logs`, traefik container |
| 401 loop in browser | server clock skew breaks TOTP — `timedatectl`; clear `authelia_session` cookie |
| `/api/*` → 502 | panel-api down: `sudo systemctl status panel-api`, `sudo journalctl -u panel-api -n 50` |
| `/api/*` → 502 only in the browser (host curls fine) | missing UFW 8010 rule for the edge subnet (RUNBOOK → UFW), or the traefik service URL in `traefik/dynamic/panel.yml` drifted from `172.17.0.1:8010` |
| `/api/actions` → 500 | panel-do not installed or sudoers missing → `make panel-do` |
| Services view empty | containers lack the `panel.manage` label |
| container action → 500 | panel-do too old for the container actions → `make panel-do`; details in `sudo journalctl -u panel-api -n 50` |
| `up -d` → "network edge declared as external, but could not be found" | the `edge` network was deleted — it's created only by `make deploy` / `make stack` (setup/02-deploy-stack.sh), never by compose (`external: true`). Cause: the edge containers were stopped (or removed) and unused networks pruned — `docker system prune` does both in one run (stopped containers first, then the network; that's what the panel's *old* `prune-docker` action ran); a bare `docker network prune` only deletes it once the stopped containers are removed (their endpoints protect it until then). The current `prune-docker` never touches networks (dangling images + build cache only). Fix: `sudo docker network create edge`, then `sudo docker compose --project-directory /opt/panel up -d` (or re-run `make deploy`); `make doctor` checks for this |
| domain resolves to an old IP | panel-ddns failing → `journalctl -u panel-ddns`; then `sudo systemctl start panel-ddns` (missing/stale records are fixed in place) |
| panel-api won't bind | docker failed to start (`panel-api` does `Wants=docker.service`, so a docker *failure* surfaces here too): `systemctl status docker`, `ip addr show docker0` missing |
