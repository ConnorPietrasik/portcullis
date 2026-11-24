# Runbook

Phase-by-phase deploy with a verification step after each. Each phase is independently testable — if something breaks, you know which layer.

Commands are written in bash; the two blocks that assign shell variables (UFW step 3, Phase 4 verify) also show a fish variant — everything else runs unchanged in fish.

On the server, `make doctor` runs every server-side check below as a single pass/fail report (read-only). (Phase 5 is by definition
from an external network, so it stays manual.)

## Phase 0 — NameSilo DNS prep (one-time, manual)

1. NameSilo → Domain Manager → **API** → generate an API key. Keep it handy — Phase 1 pastes it into `.env`.
2. NameSilo → DNS → add a `*` CNAME → `pietrasik.top`. The apex A record is created automatically by `make ddns` in Phase 2.

   **Do not remove `LEGO_DISABLE_CNAME_SUPPORT=true` (traefik env in `compose/docker-compose.yml`) when editing it.** This CNAME makes
   `_acme-challenge.pietrasik.top` a wildcard-synthesized CNAME, which lego would follow while solving DNS-01 and misplace the
   challenge TXT (go-acme/lego#1816); with the env var set, the TXT always lands at `_acme-challenge.pietrasik.top` itself, where the
   explicit record takes precedence over the wildcard (RFC 4592).

That's the whole phase. The wildcard cert comes from the DNS-01 challenge, which needs no A records at all. `panel-ddns` keeps the
apex A current forever after, and the reconciler never touches CNAMEs, so every subdomain follows the apex A automatically. If the
CNAME is ever deleted, re-create it by hand.

## Phase 1 — Tooling + secrets

```bash
make preflight      # docker, compose plugin, go, ufw, python3, ports 80/443 free
make secrets        # writes .env (chmod 600)
```

Then edit `.env`: paste the `NAMESILO_API_KEY` from Phase 0 — that's the only edit (`ACME_EMAIL` comes pre-filled, and `make secrets`
already generated the rest). Values land verbatim in the containers and systemd units (no shell execution), so keep them plain
`KEY=VALUE`:

- no `$`, backslashes, or ` #` inside a value — docker compose and systemd reinterpret those (single quotes make a value fully literal)
- no `export ` prefix on `NAMESILO_API_KEY`, `PANEL_TOKEN` & co. — systemd drops such lines silently (container-only keys tolerate it)
- no stray text anywhere — `make deploy`/`make doctor` refuse the file with a clear message (docker compose would reject it at `up` time)

Create `compose/authelia/users_database.yml` from the template — the real file is gitignored, so password hashes never reach GitHub
(a fresh clone won't have it):

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

### UFW (required)

The deploy adds **no** firewall rules — these four commands are the whole step. **Do them now** (after `make deploy`, before any
8010 listener exists): `make api` refuses to run until the rule is in place and `make doctor` checks it too, so a first deploy
can't skip this by accident. Your SSH rules are never touched.

**1 — confirm UFW is active**

```bash
sudo ufw status            # want: "Status: active"
```

Inactive → `sudo ufw enable`, then check again. (CachyOS ships UFW installed and enabled, so on target this is usually just a check.)

**2 — add the web rule** (Traefik only; TCP only — 443/udp is HTTP/3, not served here)

```bash
sudo ufw allow 80,443/tcp comment 'traefik web'
```

**3 — add the panel-api rule**, scoped to the edge network's *actual* subnet. Derive the subnet — don't hardcode it: Docker picks
it from a pool depending on what's already taken (fresh daemon → `172.18.0.0/16`, otherwise something else). The destination is
`172.17.0.1` — panel-api's bind IP, keep in sync with `panel-api.service`.

```bash
EDGE_SUBNET=$(sudo docker network inspect edge -f '{{(index .IPAM.Config 0).Subnet}}')
sudo ufw allow from "$EDGE_SUBNET" to 172.17.0.1 port 8010 proto tcp comment 'panel-api from edge network'
```

```fish
set EDGE_SUBNET (sudo docker network inspect edge -f '{{(index .IPAM.Config 0).Subnet}}')
sudo ufw allow from "$EDGE_SUBNET" to 172.17.0.1 port 8010 proto tcp comment 'panel-api from edge network'
```

**4 — confirm both rules are present, and your SSH rules unchanged** (this step only ever ADDS — it never deletes or rewrites anything else)

```bash
sudo ufw status verbose
```

You should see your existing rules plus these two (an IPv6 mirror line may appear too; column spacing varies):

```
80,443/tcp                 ALLOW       Anywhere
172.17.0.1 8010/tcp        ALLOW IN    172.18.0.0/16     ← whatever your EDGE_SUBNET was
```

Done — continue with the rest of Phase 2

#### Why it's set up this way

- **The 8010 rule is required, not optional.** Container → host traffic hits UFW's INPUT chain, and with `default deny incoming`
a missing rule turns every panel → API call into a 502. Scoped to the edge subnet + `172.17.0.1`, the API is unreachable from the
internet — which is why `make api` refuses to bind 8010 before the rule exists and `make doctor` keeps checking it.
- **SSH rules are never touched.** The only firewall change this stack makes is the two rules above. Active UFW with no SSH rule =
console-only access, so don't clean up your 22/tcp rule.
- **Docker-published ports bypass UFW.** 80/443 pass through Docker's NAT rules regardless of ufw; the real access control for
containers is what you put in `ports:`. Nothing in this stack publishes a port besides Traefik — keep it that way, and never
publish the API beyond loopback/bridge IPs.
- **Enabling HTTP/3 later:** `http3: {}` in the `websecure` entryPoint of `traefik.yml`, `"443:443/udp"` in traefik's ports in
docker-compose.yml, and `sudo ufw allow 443/udp`.

Nothing to do here in the common case: `make deploy` waits up to 10 min (`ACME_STAGING_WAIT`) for the staging cert and promotes to
production on its own — watch it happen in `make logs`. If the wait times out: keep watching until the staging cert appears, then:

```bash
make promote   # idempotent; also safe to just re-run make deploy
```

**Why staging first:** first bring-up runs on the staging ACME directory (`.env.example` defaults `ACME_CA_SERVER` to it)
so a broken NameSilo DNS-01 config can't burn the production 5-failures/hour rate limit. Promotion is a full re-issue: traefik stops,
the ACME store is wiped (the staging account URI doesn't exist on prod — reusing it fails JWS verification), the config flips, traefik
starts and issues the real cert.

Verify (subdomains resolve via the `*` CNAME → apex A; if the apex A was just created, give resolvers a minute):
- `curl -I https://auth.pietrasik.top` → 200 (valid prod wildcard cert; an *untrusted* staging cert means promotion hasn't run yet —
  `make promote`)
- `curl -I https://pietrasik.top` → 200 (portfolio placeholder — public, no auth)
- `curl -I https://www.pietrasik.top` → 200 (same portfolio — explicit www host, public)
- `curl -I https://panel.pietrasik.top` → redirects to Authelia login (no user yet)

First login: open `https://auth.pietrasik.top`, log in, register TOTP when prompted. Then `https://panel.pietrasik.top` should show
the dashboard shell (API calls will fail until Phases 3–4 — expected).

## Phase 3 — panel-do (privileged dispatcher)

```bash
make panel-do
```

The script builds (`gofmt`/`go vet` checked), installs to `/usr/local/libexec/panel-do` (root-owned, `0755`), writes
`/etc/sudoers.d/panel`, and runs the negative-test suite (unknown action → 64, path/flag injection → 65). Re-run any time after
editing `panel-do/main.go` — it's idempotent.

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
AUTH=(-H "X-Panel-Token: $TOKEN" -H "Remote-User: connor" -H 'Remote-Groups: admins')
curl -s http://172.17.0.1:8010/api/health                # {"ok":true,...} — open liveness endpoint
curl -s http://172.17.0.1:8010/api/metrics               # 401 (expected — auth enforced)
curl -s "${AUTH[@]}" http://172.17.0.1:8010/api/metrics  # gpus/cpu/ram/swap/disks/net/sensors JSON

# From inside the edge network — this is exactly what the panel's containers do, so a timeout here means the UFW 8010 rule is wrong:
docker run --rm --network edge curlimages/curl -s -m 5 http://172.17.0.1:8010/api/metrics   # 401 JSON (reachable, auth enforced)
```

```fish
# fish: same curls, fish list instead of a bash array
set TOKEN (sudo grep '^PANEL_TOKEN=' /opt/panel/.env | cut -d= -f2)
set -l AUTH -H "X-Panel-Token: $TOKEN" -H "Remote-User: connor" -H "Remote-Groups: admins"
curl -s http://172.17.0.1:8010/api/health                # {"ok":true,...} — open liveness endpoint
curl -s http://172.17.0.1:8010/api/metrics               # 401 (expected — auth enforced)
curl -s $AUTH http://172.17.0.1:8010/api/metrics         # gpus/cpu/ram/swap/disks/net/sensors JSON
```

Storage: one row per mount in `PANEL_DISK_MOUNTS` (`.env` — this server: `/`, `/home`, `/models`), each with its device, filesystem,
capacity, throughput and the drive's own temperature. A mount that isn't there (partition failed to mount, path missing, pseudo-fs)
shows as a red row on the Overview, never as another filesystem's numbers or a silent gap — check `journalctl -u panel-api` for the
matching line.

Then from a browser logged into Authelia: `https://panel.pietrasik.top` → Overview goes live (its traces fill in over the first
minute — they are differences between polls, so the first sample of every rate is empty), Actions lists the registry.

## Phase 5 — trust, but verify (from an external network)

- [ ] panel behind 2FA, only `admins` group works
- [ ] `curl -H "X-Panel-Token: <wrong>" https://panel.pietrasik.top/api/health` from outside → Authelia login page (never reaches API)
- [ ] `curl -m 5 http://SERVER_IP:8010/api/health` from outside → **connection refused** (the API is not internet-reachable)
- [ ] demos/apps you mark public open without an Authelia prompt
- [ ] `sudo -n /usr/local/libexec/panel-do restart-service foo.service` as a non-panel user → password prompt/denied

## Dynamic IP

Nothing to do here after Phase 2 (`make ddns` installed it). `panel-ddns` (systemd timer, user `panel`, stdlib python, outbound
only, no root) reconciles the apex NameSilo A record every 10 minutes on its own; these commands are how you watch it:

```bash
journalctl -u panel-ddns -n 50                  # what it did and when
sudo systemctl start panel-ddns.service         # reconcile now — also the manual run (the script
                                                #   FATALs if run directly: the API key only comes
                                                #   from the unit's EnvironmentFile)
dig +short pietrasik.top @ns1.dnsowl.com        # what the authoritative NS says (check NS at NameSilo)
```

**How it works (read once):** the server's IP isn't static. Each tick: detect public IP → list records → update stale ones, create
missing ones. Declarative, no state file, no short-circuit — whatever drifted is fixed within one tick (~6 API calls/hour —
trivial). CNAMEs at configured hosts are skipped (`*.pietrasik.top` is a CNAME → apex, so it rides along) — never overwritten. TTL is
pinned at 3600 (NameSilo's minimum), so worst-case cutover after an IP change ≈ 10 min + 1 h of resolver caching. Reuses
`NAMESILO_API_KEY` (the same key Traefik uses for DNS-01), reaching the oneshot via the reduced env file (`.panel-ddns.env`), so the
full `.env` secret set never sits in its environ.

## Updates

`make update` is the one Day-2 command: it syncs every repo change to `/opt/panel` (compose, routes, Authelia, `.env`, panel
frontend) **and** floats everything within its declared *line*. It requires a green `make doctor`, snapshots everything before
touching anything, and auto-rolls-back on any failure — updates are routine and safe by construction. After the gates pass it
also updates the servitor agent VM (when installed) as a **best-effort tail**: a servitor failure never rolls back the host
stack (the servitor world rolls back on its own) — `SERVITOR_UPDATE=0` skips it.

```bash
make update      # preflight (repo compose must render with repo .env) + doctor precondition —
                 # both abort before touching anything → snapshot everything (image IDs, deployed configs, exact panel-api copy, npm files)
                 # → npm bump (in-repo, within ranges) → sync configs from the repo
                 # → re-pin the server's /etc/hosts (tools/panel-hosts.sh — the route set may have changed)
                 # → pull → rebuild (bases pulled)
                 # → recreate what changed (+ restart on start-time config change) → update panel-api
                 # → gate: post-update services healthy (pre-update set + added) + panel-api serving + full doctor pass
                 # → servitor (agent VM) update — best-effort tail after the gates: floats pi/extension +
                 #   rebuilds/swaps the guest image when its userland is stale (skips a shut-off VM — the
                 #   tick never wakes it; never gates, never rolls back the host)
                 # → on any failure of the GATES above: automatic rollback of all of it, re-gated
make rollback              # on demand: undo the last `make update` (same machinery)
make rollback WHAT=radiance  # or: images ONLY of the services WHAT matches — a name (gpu-27b), a
                             # prefix (gpu), or an image name (radiance); everything else stays
```

### What it does **not** touch — by design

- **Majors** in any line: deliberate edits (a tag, a `FROM`, a range).
- **panel-do / panel-ddns**: no external dependencies (stdlib-only Go / Python) — nothing to float. Their update is a repo edit +
  `make panel-do` / `make ddns`.
- **Host OS packages**: `sudo pacman -Syu` on your normal schedule.

### The lines — what floats, and how a breaking major happens

Patch/minor security releases ride in on every update — the internet-facing stack should never run stale. A breaking major only
happens through a deliberate edit of that component's line:

| Component | Line | A breaking major is a deliberate edit of… |
|---|---|---|
| traefik, authelia, portfolio | major tags: `traefik:v3`, `authelia/authelia:4`, `nginx:stable-alpine` | the tag in `compose/docker-compose.yml` |
| panel-web base images | `node:22-alpine`, `nginx:stable-alpine` | the `FROM` lines in `compose/panel-frontend/Dockerfile` |
| panel-web npm deps | the ranges in `package.json` (e.g. `react ^18.3.1`) | a range in `compose/panel-frontend/package.json` + commit |
| panel-api pip deps | the ranges in `requirements.txt` (e.g. `fastapi>=0.115,<1`) | a range in `panel-api/requirements.txt` + commit |
| stack configs + secrets | whatever is committed in this repo (compose, routes, Authelia ACLs, `.env`) | — the repo edit **is** the change (holding a line = a full-version pin, e.g. `traefik:v3.7.9`) |
| radiance (GPU fleet) | `latest` — deliberately OUTSIDE the declared-line policy (frequent builds wanted) | the tag in `x-gpu-base`; a build that breaks behavior → `make rollback WHAT=radiance` |
| searxng | `latest` — deliberately OUTSIDE the declared-line policy (rolling date tags, no releases — see the image-bump duty in **Web search (SearXNG)**) | the tag in `compose/docker-compose.yml`; a build that breaks behavior → `make rollback WHAT=searxng` (images only) |

### If something goes wrong

- A breaking major you didn't want (any line): `make rollback`, then pin it in the repo (e.g. `traefik:v3.7.9`, or the old range back
  in `package.json`/`requirements.txt`), `make update`. (Re-running `make update` without the pin would just retry the same major.)
- A floating image that passed the gates but broke behavior: `make rollback WHAT=radiance` (images only). Then either pin the good
  build's tag in `x-gpu-base` + `make update` to hold it, or just `make update` again to re-try `latest`.
- Update interrupted mid-run (Ctrl-C, power loss): the snapshot + rollback tags survive on disk; `make rollback` restores everything,
  or just re-run `make update`.

### How it works (read once)

Mechanics in `setup/08-update.sh` (shared helpers in `setup/common.sh`):

- **Preflight** = `docker compose config -q` renders the repo's compose against the repo's `.env` before
  anything is touched (a var a new block needs but `.env` lacks aborts the run pre-snapshot/pre-sync, leaving it
  recorded as `failed` in the state file;
  `make deploy` gates on it too, and `make doctor` checks both trees).
- **Snapshot** = `/opt/panel/.update-snapshot-<ts>` (state in `/opt/panel/.update-state`): the exact *image IDs* of what is running
  (tags move, IDs don't), re-tagged `portcullis-rollback/<svc>:<ts>`; **the deployed config tree** (`/opt/panel`: docker-compose.yml,
  gpu-configs/ (the radiance configs — coupled to the compose that mounts them, so they roll back with it),
  traefik/, portfolio/, searxng/, the authelia configs, panel-frontend, `.env`, the reduced env files — a tarball; runtime data like
  `db.sqlite3` excluded); an **exact copy of `/opt/panel-api` (venv included)**; and the npm files about to be bumped. A failed npm
  bump, config sync, or pull stops the run *before* any container is touched.
- **npm bump** = `npm update --package-lock-only` in `compose/panel-frontend` — floats every dependency within the range declared in
  `package.json` (a MAJOR is a deliberate range edit). The lockfile is **gitignored server-local state**, deliberately not a repo
  artifact: it keeps `git pull` conflict-free between updates and stops publishing the exact resolved versions. A fresh clone has no
  lockfile — the first update generates it from the ranges, and the Docker build falls back to `npm install` until then. Rollback
  restores the pre-update files from the snapshot.
- **configs** = the same repo → `/opt/panel` sync `make deploy` performs (secrets substituted, stale route files pruned, the
  panel-uid reduced env files re-derived — a `.env` edit is applied here too). authelia + traefik read some files only at container
  start — those services are restarted **only if their content changed** (a routine update adds no such downtime). A sync failure
  aborts before any container changes.
- **hosts pin** = `tools/panel-hosts.sh` re-pins every routed name to the server's LAN IP in the server's own `/etc/hosts` —
  the router doesn't hairpin, so the doctor's endpoint checks (which curl the public names *from the server*) must resolve
  locally. Idempotent (no write when the managed block already matches); a shadowed pin aborts the run before any container is
  touched. Rollback does not restore `/etc/hosts` — a pin left behind for a removed route is harmless (Traefik 404s it) and the
  next update re-anchors.
- **panel-web** = built from the synced build context, with `--pull` so its `FROM` bases float too (without it, BuildKit reuses the
  local bases from the first build). A build/base-pull failure aborts before any container changes.
- **Recreate** = `up -d` converges only what should be running after the update: the pre-update running set, plus services this
  update *added* (the “add an app → `make update`” flow). A service the operator deliberately *stopped* stays stopped (and out of the
  gate) — an update doesn't resurrect it; a bare `make deploy` (ungated) still starts everything. Services *removed* from compose
  have their containers dropped, and a rollback brings them back.
- **panel-api** = `main.py` + `requirements.txt` + the systemd unit re-deployed, `pip install --upgrade` within the declared ranges,
  restarted, verified serving before the gates. (The first-install UFW gate in `make api` is deliberately not re-run — on an
  already-deployed server a firewall regression surfaces in the doctor gate → auto-rollback.)
- **Gate** = every service that should be *running after the update* healthy (the pre-update set plus what this update *added* —
  compose healthchecks, `UPDATE_HEALTH_TIMEOUT=600`s; a deliberately stopped or removed service can't fail it), then a full
  `make doctor` pass (`UPDATE_DOCTOR=0` to skip both). Doctor is also a *precondition*: the update aborts before touching anything
  if `make doctor` is not green — a pre-existing doctor failure would otherwise auto-revert every update. A pre-snapshot abort
  (preflight or doctor) leaves the run recorded as `failed` in the state file: the panel shows a red banner, nothing was
  applied, and there is nothing to roll back.
- **Rollback** = restore the pre-update config tree first (so the recreate reads the pre-update compose + `.env`), then recreate the
  snapshot's images via a temporary compose override (`--remove-orphans` drops containers for services the update added), restore the
  exact panel-api copy, restore the npm files — all from **local** snapshots, so it works on a dead connection. A gate failure
  triggers it automatically; a component that fails to revert is reported as PARTIAL (fix and re-run `make rollback`). With WHAT it
  recreates ONLY the matching services' images (matched by service name exact/prefix, or pre-update image name substring) and leaves
  everything else in place — the escape hatch for a floating image (radiance rides `latest`) that passed the gates but broke
  behavior; the next `make update` re-anchors the snapshot on the reverted build.
- **Runtime data is outside the snapshot**: authelia migrates its sqlite DB at startup across minor releases, so a rolled-back image
  boots against the *already-migrated* DB. Older 4.x images read the newer schema fine in practice, but if a migration ever breaks a
  rolled-back image, the DB — not the image — is what you restore: back up `/opt/panel/authelia/` before an update you don't fully
  trust.
- The state file's `status` tracks the run end-to-end: `in-progress` from the moment the run takes the lock (so the panel's
  banner flips immediately — even a run that dies at a pre-snapshot gate is seen, left `failed`), `updated` on success, and
  `rolled-back` / `partially-rolled-back` after a rollback. After a *successful* update the state file still holds the pre-update snapshot, so one `make rollback` undoes the last update; the
  next `make update` replaces it. Snapshots (rollback tags + dirs, including the ~100 MB panel-api venv copy) are GC'd after 14 days (only after the new snapshot is recorded, so the live tags are protected).
- `make deploy` is the **first-boot** path (network, secrets, ACME staging→prod, first build). It does **not** pull — with floating
  tags a pull is an upgrade, so image movement only happens through `make update` (gated). A Day-2 re-run of it applies config
  ungated and warns.

## Auto-updates

`make update` is manual by default. The auto-update re-runs the **same** command (same doctor precondition, snapshot, gates,
auto-rollback) on a schedule and from the panel, so nothing that has a line to float goes stale:

```bash
make autoupdate   # installs panel-autoupdate.{service,timer} — DISABLED by default
```

Then enable from the panel (Settings → auto-update → enable) or `systemctl enable --now panel-autoupdate.timer`. The timer
fires 2 min after the LAST boot (so a box that was off for days updates on its next boot, not a day after) and daily
(+10 min jitter), and each
tick is:

```
panel-do update-run  →  setup/08-update.sh --detach   (the exact same make update, in its own session)
```

- **What it keeps fresh** — everything in the **lines** table above: floating container images (traefik/authelia/nginx
  patch+minor security releases; the full `latest` movement of radiance + searxng), panel-web's build bases, npm deps within
  `package.json`'s ranges, pip deps within `requirements.txt`'s ranges, plus any committed-but-unapplied change in the server
  clone (and a re-anchor after a targeted rollback).
- **Servitor tail (when installed)**: the tick's `make update` includes the best-effort servitor update AFTER the host gates —
  it can add ~40 min (an image rebuild when the userland outgrows the freshness floor) and the VM is down for the swap itself;
  a shut-off VM is left asleep (planned downtime — the tail never starts it; the float lands on the next tick after it is
  back, or via a manual `sudo make update` in the checkout); a servitor failure never fails the host tick
  (`SERVITOR_UPDATE=0` skips it).
- **What it does NOT do**: no `git pull` — the clone only moves when *you* edit it (when you're at the computer you can run a
  manual update anyway). No majors — those are deliberate edits, and the panel flags a major in red when one lands (e.g.
  `authelia 4 → 5`, `node base 22 → 24`). No host OS packages. `panel-do` / `panel-ddns` still update only via a repo edit +
  their make target.
- **Registration**: every `make update` records `repo_root` (+ `repo_head` when the checkout is a git repo) in
  `/opt/panel/.update-state`; the auto-update runs from the **last clone you ran `make update` from**. Moved the clone? Run a
  manual `make update` from the new location once to re-register it.
- **Dirty clone = skip**: uncommitted changes in the clone make a tick skip (the panel shows why). A skipped tick —
  or one that fires while an update is already running — exits 75 (EX_TEMPFAIL) and counts as a **success** for the
  timer (`SuccessExitStatus=` in the unit), so `journalctl -u panel-autoupdate` shows panel-do's SKIPPED line, not a
  failed unit. A git checkout whose status **cannot be read** (a corrupt index —
  e.g. power loss or an interrupted git write) makes the tick **fail**, not skip — fail
  closed: an unverifiable working tree is never applied unattended — the
  panel's Overview banner flags it ("clone git status unreadable"). (A leftover
  `.git/index.lock` alone is handled by the skip path above.) The
  panel's *run anyway*
  button (and a manual `make update`) apply the working tree as-is — that is the deliberate path for in-progress edits.
- **Dashboard notifications** (Overview): a blue banner while an update runs (with the live run-log tail — panel-api restarts
  mid-update, so the API blips for a few seconds; the banner survives it), an amber *update available* banner listing the
  pending commits / line changes with a red **MAJOR** badge when a line's major version moves, a red banner when an update
  fails and rolls back (plus *re-run rollback* / *re-run update* buttons), a red banner when an update fails at a
  pre-snapshot gate (nothing was applied — nothing to roll back — *re-run update* button), a red banner when the
  automatic rollback was only PARTIAL (unknown stack state — the state stays `in-progress`, and the presence of the
  `failure` field is what turns the banner red; *re-run rollback* / *re-run update* buttons), and a collapsed *updates* card with the last
  update's before→after rows. A failed / rolled-back / partial banner shows the gate's **failing checks** (doctor's FAIL
  lines + their hints + the summary, recorded in the state file's `failure` field by the failing gate — the doctor gates
  pass their full report; the health/panel-api gates record their name only) — the run-log tail of a doctor report
  is mostly PASSes after the failing check, so it is the fallback only (or, on a rolled-back banner, the companion:
  it shows what the rollback did).
- **Observability**: the run log is `/opt/panel/.update-run.log` (rotated to `.old` past 1 MB); the state file's status is
  `sudo jq .status /opt/panel/.update-state` (`in-progress` for the whole locked run, `failed` when a pre-snapshot gate aborts it
  — the gate's failing lines are in its `failure` field: `sudo jq .failure /opt/panel/.update-state`, the full gate output in the
  run log); the timer's own journal:
  `journalctl -u panel-autoupdate`; `make doctor` checks the state file, a dead partial-rollback run (in-progress + the
  `failure` field — flagged now, not after the 2h stuck threshold), a stuck in-progress run, a stale lock, and — when
  the timer is enabled — the clone's auto-exec safety (the same checks the action makes).
- **First deploy of this feature**: `git pull` in the server clone (08/09 gain the committed exec bit + `--detach`),
  `make panel-do` (the new actions + the update-report smoke test — first, so the panel's update endpoints exist as
  soon as the new UI ships), `make update` (records `repo_root`, installs the new panel-api + UI), `make autoupdate`
  (the timer, disabled), and enable it from the panel.

| Symptom | Diagnosis / fix |
|---|---|
| panel: "no update state yet" / tick fails "no update state" | `make update` has never run from a clone on this host — run it once |
| auto-run skipped: "uncommitted changes in the clone" | commit (or discard) the clone's changes — or the panel's *run anyway* |
| timer ticks (clean journal, no SKIPPED line) but no update ever lands | the installed unit lost `KillMode=process` — the default `control-group` kill SIGTERMs the detached update when the oneshot completes (`setsid` protects against terminal HUP, not cgroup teardown) — `make doctor` red-lights it; fix: git pull, `make autoupdate`, then force a tick (panel toggle off→on, or `systemctl restart panel-autoupdate.timer`) |
| tick fails: "clone … is a git checkout but git status failed" | the clone's index is unreadable/corrupt (power loss or an interrupted git write) — inspect/fix the clone (worst case: re-clone + re-copy `.env`/`users_database.yml`), then re-run; *run anyway* applies the working tree as-is, deliberately |
| action fails: "update state holds an invalid repo_root" | the state file was hand-edited — `sudo rm /opt/panel/.update-state`, then `make update` |
| doctor: "last update's automatic rollback was PARTIAL" (state: in-progress + failure) | the run is DEAD — its automatic rollback was only partial, the stack's state is unknown — `sudo jq .failure /opt/panel/.update-state` (what failed), `sudo tail /opt/panel/.update-run.log` (what the rollback did); re-run the rollback: `make rollback` (the panel's banner offers it too) |
| doctor: "update stuck in-progress" (no failure field) | the update process died mid-run — `sudo tail /opt/panel/.update-run.log`, `pgrep -af '08-update.sh\|09-rollback.sh'`, kill the leftover, then `make rollback` |
| panel: "the last update failed before it changed anything" (state: failed) | a pre-snapshot gate failed (preflight or doctor — nothing was applied, nothing to roll back) — the failing checks are in the state file's `failure` field (the banner shows them): `sudo jq -r '.failure.detail' /opt/panel/.update-state`; the full gate output is in the run log: `sudo tail /opt/panel/.update-run.log`; fix it, then re-run (the panel's *re-run update* or `make update`) |
| doctor: "update lock held with no in-progress update" | a leftover from an interrupted run is still alive and holds the lock — flock is released only on process exit, so a held lock proves the holder never died; `sudo fuser -v /opt/panel/.update.lock` names it; kill it if stale, then `make doctor` |
| doctor: clone "fails panel-do's auto-exec check" | the clone, its `setup/`, or a directory ABOVE the clone became group/other-writable (or is owned by `panel`) — `chmod go-w` it; `08/09-update.sh` not executable, panel-owned, or missing the bash shebang → re-pull the repo (exec bit + shebang are committed) |

## Add an app

Two edits in this repo, then `make update`. Traefik is file-provider only — it never reads docker labels, so every app needs a route
in `traefik/dynamic/`:

```yaml
# compose/docker-compose.yml — the service, on the edge network
  jellyfin:
    image: jellyfin/jellyfin:10   # major-line float, like the edge stack (RUNBOOK → Updates)
    restart: unless-stopped
    networks: [edge]
    labels:
      - panel.manage=Jellyfin        # shows up in the panel's Services view (optional)
      - panel.category=Media         # collapsible group in the Services view (optional; absent → “Other”)
```

Services group under collapsible category headers by `panel.category` (same no-commas rule as
`panel.manage` — `docker ps` encodes labels comma-joined); unpinned groups sort alphabetically and
uncategorized services land under “Other”. A new group needs no code — just start using the label
(`panel-frontend/src/views/Services.tsx` only pins the display ORDER of a few known names).

Tag it at the major line (`:10`, not `latest` or a full version): `make update` then floats it with the rest of the stack and never
crosses a major on its own.

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

`make update` (syncs configs + recreates what changed). Default policy: any authenticated user. Add an Authelia rule in
`compose/authelia/configuration.yml` **above the wildcard** only if it needs group restriction or a public bypass — `make update`
restarts authelia whenever `configuration.yml` or `users_database.yml` content changes (both read only at container start), so no
manual recreate step is needed.

If a client machine pins the names via `tools/panel-hosts.sh` (README → From the LAN), re-run it
there — it reads the new route from its own checkout, no pull or deploy step on that side. The server
re-pins itself automatically as part of `make update` (see Updates).

## GPU fleet (Radiance instances)

[Radiance](https://codeberg.org/StillDeadcode/radiance) instances on 7 shared R9700s (six at
x16 + the 7th at x8 — see **The writer** below); start/stop and GPU assignment from the panel
(Services view for the containers, **GPUs view** for the cards — see **GPU pool** below).
Machine APIs, not browser apps: no Authelia — the gate is the server's own API key (`Authorization: Bearer`) behind the
wildcard cert; clients use `base_url=https://<name>.pietrasik.top/v1` + key, exactly like OpenAI. No rate-limit middleware
on these hosts — see the "no rate-limit" note in `traefik/dynamic/middlewares.yml`.

**An instance = a service block + a radiance config + a router pair**, deployed by `make update`: `gpu-<name>` in `compose/docker-compose.yml` (merges
`x-gpu-base`) ↔ `Host(`<name>.pietrasik.top`)` → `http://gpu-<name>:8000` in `traefik/dynamic/gpu.yml`. One subdomain per instance so
agents pin the right server; the wildcard CNAME + cert make that zero DNS work. Port is always 8000 (own IP per container). The
display name is the `panel.manage` label — **no commas** in it (`docker ps` encodes labels comma-joined and the Services view parses
them that way); `panel.category` groups the Services view by its label value — the chat-LLM
instances carry `LLM`, image/video generators will carry `Image gen` / `Video gen` — and copying a
block for a new instance carries both labels with it.

**Cards:** assigned at run time from the panel's **GPUs view** (a pool with auto-grab — see **GPU pool** below).
The block's `ROCR_VISIBLE_DEVICES` is the FALLBACK default a hand-run compose
would use, and the `gpu.cards` label (== the `tp` in the instance's radiance config) is how many
cards the instance needs — REQUIRED: radiance 2.x takes no flags, so the label is the pool's only
card-need source (doctor cross-checks it against the config; missing, the GPUs view errors instead
of silently grabbing one card). Everything else in
`x-gpu-base` is identical for all instances.

**Radiance config (2.x):** radiance 2.x takes NO command-line flags — the container's command is
`radiance --config /etc/radiance/radiance.yaml` (in `x-gpu-base`), and every setting is a YAML key in
`compose/gpu-configs/<instance>.yaml`, mounted per block (a block's `volumes` list REPLACES the
anchor's — YAML `<<:` is shallow — so each block re-lists its class-dir mount
(`/models/llm`, `/models/image` or `/models/video` at that subpath — see **Models** below)). A key is the pre-2.x
flag minus its dashes, at its level: `server:` holds `host` / `port` / `api-key(-env)`, `memory:` holds
`gpu-headroom-mib`, the rest (`model`, `tp`, `max-num-seqs`, `max-model-len`, `prefix-cache-mib`,
`checkpoint-policy`, ...) sit under the one entry in `models:` (upstream `docs/MULTIMODEL.md` §4;
key reference: `radiance --help`). The API key reaches the server through `RADIANCE_API_KEY` in the
block's environment (`api-key-env` in the file) — never written into the file itself. A config edit
syncs with `make update` but recreates NOTHING (the mount path is unchanged) — radiance
reads it once at start, so restart the instance from the GPUs view to apply it. Model containers:
2.x REFUSES block-FP8 / MXFP4 / M2XFP4 / ParoQuant weights at startup (by the first such weight's
name) — a refused model takes its int8/bf16 container (upstream README → Models).

**`restart: "no"` on purpose:** after a reboot nothing from the fleet comes back by itself, EXCEPT the set you mark
**boot** in the GPUs view (the boot-autostart list — applied by `gpu-autostart.service`, see **GPU pool** below).
`make update` recreates running instances whose block or image changed and leaves deliberately-stopped ones down (and out of the health gate).
It never *starts* a fleet instance from a missing container: a pruned stopped container (raw `docker container prune`)
is indistinguishable from a new one, and auto-starting the whole fleet at once is invalid when instances share cards —
new instances are deployed + pulled, then left down. Bring one up from the panel: the Services view lists defined
but container-less services under **defined, no container** with a *create + start* button — for gpu-*
instances that is the GPU-aware path (saved card set when free, else the lowest free set; see **GPU pool**
below), for everything else it is panel-do `start-service` (same validation/audit as the other actions).
The command-line equivalent is `cd /opt/panel && sudo docker compose -f docker-compose.yml -f
/opt/panel-api/gpu-pool-override.yml up -d <svc>` — keep the GPU override in the render for gpu-*
instances, or the revive pins the base-file fallback cards and shows up as drift in the GPU view.
After that the panel manages it as usual.

**Add an instance** (including a second copy of a model — `gpu-27b-1` next to `gpu-27b`): copy the block
(name, `ROCR_VISIBLE_DEVICES` fallback, `gpu.cards`), its config in `compose/gpu-configs/`
(named after the service — doctor checks the mount and the `tp` / `gpu.cards` agreement), + the router
pair — five name references must all match (service key, config mount, router key, `Host(...)`,
service `url`) + an `|| Host(`<name>.pietrasik.top`)` clause in the
`fleet-open` rule so the dashboard link works (Traefik v3's `Host()` takes ONE host — the v2
comma-list form is a parse error it drops silently, doctor flags it); `make doctor` flags any of
the six that don't (`fleet-open` routes to
panel-api, not an instance, so it sits outside the instance-pairing checks — in gpu.yml the
`gpu-*` prefix marks instance routers and `fleet-*` names shared ones — but its host list
is verified against the compose services). Give the twins distinct display labels
(append the instance name) so the Services view tells them apart. Then `git commit`, `make update`
(deploys + pulls it, leaves it down) and start it — panel GPUs view → *start* (or the Services
view → *defined, no container* → *create + start*, which for gpu-* services goes through the same GPU-aware
path). First load ~1 min on this hardware (the 113 GiB flashnext container is longer — if it ever
trips the update gate's 600s budget, raise that block's healthcheck `start_period` and
`UPDATE_HEALTH_TIMEOUT` rather than re-running into the rollback), shown in the panel. The keys in the
committed configs are this host's own tuning — the configs are the truth, do not reconcile them against
upstream. To trial a NEW model first: `git clone --depth 1` upstream, run its `deploy/compose/<model>.yaml`
ad hoc (one at a time), delete the clone — its keys belong to the trial, not to a fleet config.

**Key:** one shared `LLM_API_KEY` for all instances (a key authorizes fleet use, not one instance). Rotate = edit `.env` +
`make update` (radiance reads it at startup).

**Dashboard:** radiance serves its dashboard at the instance host root — public HTML, data calls
bearer-gated like `/v1`. The page takes the key from its OWN origin's localStorage only (no URL
param, and a panel page can't write cross-origin), so panel-api serves a one-line bootstrap at
`https://<sub>.pietrasik.top/open` (router `fleet-open` in gpu.yml — the ONLY Authelia-gated path on
these hosts; admins-only: panel-api re-checks Remote-User/Remote-Groups). No secret gate by design
— a browser can't hold the panel token — so a direct 172.17.0.1:8010 bind hit (reachable from any
edge container) 401s only while it leaves Remote-* unset: one that CAN spoof them gets the key,
accepted because it already sits on the fleet's own network. The panel's Services view links there
per instance (*dashboard ↗*, shown only while the container is running — down, the bounce 502s):
Authelia pass → key written to that origin's localStorage → bounce to `/`, dashboard live.
The `/v1` routers are untouched — API clients keep the bearer gate. No-JS/first-use fallback:
type the key into the field the dashboard shows in its header after a 401 (persists in
localStorage).

**Logs:** every instance sets `RADIANCE_LOG: warn` in its own `environment` (it can't live in `x-gpu-base` — YAML `<<:`
merges top level only, and an instance's `environment` replaces the base's whole map). Per-request INFO lines (token
counts, cache, tok/s, ttft) are the chattiest thing on the box and carry nothing the API's timings/metrics don't; `warn`
keeps errors + ROCm warnings. Raise one instance to `info` (or `debug`, `-v`) when debugging it. json-file rotation caps each
instance at ~30 MB regardless (`x-logging`).

**Updates:** a config-only update recreates nothing — a radiance-config edit included (the mount path
is unchanged; restart the instance from the GPUs view to apply it, see **Radiance config** above).
When an update DOES recreate an instance (radiance moved on `latest`, block
edited), its model reload (~1 min) sits inside the 5m healthcheck start_period, and the default 600s budget
(`UPDATE_HEALTH_TIMEOUT`) covers both the update gate and the rollback re-gate — they share it — raise it only for a much slower
model. A new build that is *healthy but behavior-breaking* is different — the gates can't see it: `make rollback WHAT=radiance`
returns just the instances to the pre-update build (re-gated on the same budget), and the next `make update` re-anchors the snapshot
on it.

**Models:** by class dir — `/models/llm/*.rad` (chat LLMs; the dir name predates the `llm-*` → `gpu-*` rename), `/models/image/*.rad`, `/models/video/*.rad` (gen models — see **Gen slots** below). Every instance ro-mounts its class dir AT that subpath (configs address models by container path, so the mount target is the class subdir, not the bare `/models`): the gen section's config parser takes the model's kind from the 2nd path component, so an instance's LLM entries stay discoverable (e.g. as Gen enhance targets) only under `/models/llm/`. If `/models` is btrfs with compression, keep `.rad` files uncompressed (`chattr +C`) — direct-I/O n-gram reads degrade otherwise. Artifacts land in `/models/gen-output` (panel-owned, served read-only at `panel.pietrasik.top/gen/*`).

**Gen slots (image/video)** — the panel's own generators: three `gpu.private=true` fleet instances on the
lane cards (5,6 today — the 7th card took tile 4), no router pair, no host, no subdomain — the only thing that talks to them is
panel-api, by container IP over the edge network. They join the same GPU pool (assign/start/stop from
the **GPUs** view, or directly from the **Gen** tab), same ledger, same override, same lazy-stop
semantics; what differs is the surface (internal, so `make doctor` exempts them from the
router/Host/fleet-open checks and FAILs them if a router ever appears) and the config shape. The Gen
section also talks to the **writer** instance (below) by container IP — but that one is a normal
PUBLIC fleet instance, reached for prompt enhancement only by panel-api.

**Config shape:** each gen config hosts ONE model — its generator (an image OR a video model, its
kind taken from the container path: `/models/image/…` → image, `/models/video/…` → video). Gen-ONLY:
the in-slot enhancer is GONE — prompt enhancement runs off-slot, on the writer instance (below), so
the gen model is the sole entry and simply stays resident (no `ttl:`, no swap partner, `warm-up: true`
absorbs the one load into boot). A slot is a gen slot IFF its config hosts at least one image/video
model — an LLM-only config (its entries parse as `llm` kind, not
image/video) makes it a plain instance.

| slot | cards | lane fallback | generator | host-mib |
|---|---|---|---|---|
| `gpu-gentp1a` | 1 | 5 | `Krea2` (16.4 GiB, image) | 24576 |
| `gpu-gentp1b` | 1 | 6 | `Krea2` (16.4 GiB, image) | 24576 |
| `gpu-gentp2` | 2 | 5,6 | `Minimax-H3-T2V` (62.7 GiB, video) | 73728 |

Both tp1 slots are identical and run concurrently (disjoint cards, same config — one krea-2 job each).
`minimax-h3-i8.rad` is 98% of the pair as a FILE — it fits only because of
`vision-tower-placement: ram` (the ViT towers sit in host RAM, which `host-mib` budgets — the value
in the config file, not the container env). No pre-flight check: if it doesn't
fit, the first boot fails and says so.

**The writer (the 7th card):** `writer.pietrasik.top` — a standard PUBLIC fleet instance on the new
card (PCIe5 **x8**, the odd one out: load traffic ~half speed, inference unaffected — so the
chat models live here, and the swapping gen slots keep the x16 cards). Its config hosts TWO
models, both `tp: 1`, disk-park only (`park: {ram: never}` — pinned host RAM is the scarce shared
resource). Only ONE of the two fits the card's VRAM at a time; there is deliberately NO
`priority` on either entry (neither "always wins"): a model at work keeps its cards (its demand
waits for the other to finish — a switch is bounded by the config's `drain-timeout`), and between
sessions the cheaper-to-reload one (Writer) yields first. Listing `Writer` FIRST makes it the
resident one right after boot (the warm-up loads later-in-file entries first and the last loaded
parks the earlier one — see the config's header):

| entry (the /v1 `model` name) | file (`/models/llm/`) | role |
|---|---|---|
| `Writer` | `hemmingway-1.5-iq4r-s-tool-tail-mtp-gptq.rad` | the Gen section's prompt enhancement (listed first → loads last in the warm-up → resident after boot) |
| `Uncensored` | `gemma4-31b-heretic-ara-a4.rad` | unfiltered chat (100k ceiling, thinking on by default) |

Standard six name references (RUNBOOK → GPU fleet: compose block, config, router pair, fleet-open
host). `panel.category=LLM`, so it is also eligible for the `llm.pietrasik.top` fallback order (LLM
settings — your call). The Gen section's ENHANCE TARGET (Gen settings) names the instance + model by
default (`gpu-writer` / `Writer`); the engine reaches it by container IP over the edge network, starts
it via the same `gpu-apply` when a job needs it, and the target can be repointed at ANY chat-capable
instance in the settings (e.g. `Uncensored` on the same instance, or a 27b instance) — a misconfigured
target shows in the Gen tab and 400s at the door.

**Card index (MEASURED — re-derive after any card add/remove):** the pool is ROCR-index based and
`amd_gpus()` reads sysfs, so a card appears in the GPUs view automatically — but the PCIe slot
order is NOT the enumeration order. The 7th card (PCIE6, the x8 slot — ~29 GB/s vs ~57 GB/s in a
pinned-RAM↔VRAM memcpy) trained at **tile 4**, inserting itself mid-order and pushing the old
tiles 4,5 to 5,6 (the lane's physical cards); tiles 0–3 are unchanged. The compose fallbacks match
this (writer `4`, lane `5,6`). The stable per-card identity is the PCI BDF — the Overview's
`GPU-N` headers show each tile's BDF chip; record that map after any hardware change (the BDFs
themselves are slot-stable; it's the tile→BDF mapping that moves). The GPU view's assignment —
the override — wins anyway, so the fallbacks only matter for a start without an assignment.
The pool makes the dedication durable too: the x8 card is **reserved** for `gpu-writer`
(GPUs view → *reserve a card…*, note `PCIe5 x8`) — the auto-grab then never hands tile 4 to
another instance, and the reservation records the card's BDF (`bc:00`) at reserve time: if the
tile→BDF map ever moves (a card add/remove), the GPUs view's **warnings** say so, so the
dedication can't silently re-point at a different physical card. The Overview's `GPU-N`
header shows the same mark at a glance (its note chip).

**Deploy (first landing):**

```bash
# 0. the 7th card is in the PCIE6 slot (x8). MEASURED: it landed at tile 4
#    (old tiles 4,5 shifted to 5,6) — verify on your boot the same way (GPUs
#    view + the Overview's per-tile BDF chips; a pinned-RAM↔VRAM memcpy on a
#    tile shows ~29 GB/s on the x8 card vs ~57 on the x16 ones).
# 1. the .rad files (their sizes above; chattr +C on btrfs) into the class dirs:
mkdir -p /models/llm /models/image /models/video
#    krea-2-raw-iq8r.rad             → /models/image/  minimax-h3-i8.rad   → /models/video/
#    hemmingway-1.5-iq4r-s-tool-tail-mtp-gptq.rad → /models/llm/  gemma4-31b-heretic-ara-a4.rad → /models/llm/
# 2. free the lane: stop gpu-27b-2 from the panel (GPUs view → stop). It stays stopped across updates.
# 3. git commit; make update   # deploys the 3 slots + the writer + panel-api + the UI, leaves
                               #    them down — and recreates the four LLM instances (their /models
                               #    mount moved to the class subpath: one model reload each)
# 4. GPUs view → reserve tile 4 for gpu-writer (note "PCIe5 x8" — the auto-grab
#    then never hands it to another instance, and the writer's own grab lands on it
#    first: no "lowest free card" pitfall), then assign the lane while the slots are
#    still down (5 → gpu-gentp1a, 6 → gpu-gentp1b, 5,6 → gpu-gentp2 — an explicit
#    assign refuses a card a running instance holds; the tp2 set overlaps the tp1
#    ones on purpose — that overlap IS the lane), then GPUs view → start both tp1
#    slots (the Gen bar's `pool ↗` links there; the writer comes up on demand — or
#    start it to warm it).
#    The tp2 assignment is what makes the tp2 slot's status read `down` (hover the
#    Gen bar's TP2 chip: a start would stop both tp1 slots) while the tp1 slots
#    hold the lane (smoke below); without it the engine still
#    yields the lane for the video job (its start auto-grabs the lowest free set,
#    and the idle yield covers that grab) — but it would record whatever it takes
#    (e.g. [2,5] if a card is down), off the x16 lane.
#    Also re-assign gpu-27b-2 (auto): its saved set still names the OLD lane tiles
#    (4,5 pre-7th-card) — after the renumbering that includes tile 4, now reserved
#    for the writer. The fence makes its next start fall back to a free set anyway,
#    but the stale set would keep showing as assigned (the GPUs view's warnings name
#    it); re-assigning (auto) refreshes it to the cards it actually takes.
#    gpu-gentp2 is NOT started by default: it needs BOTH lane cards, so it can only run while the
#    two tp1 slots are down (a queued video job stops them itself; see below).
# 5. smoke: queue a small image job (model `image`) and a ~5 s video job
#    (model `video`). The video needs the tp2 slot — with the tp1 slots holding
#    5 and 6, the Gen bar's TP2 chip sits `down` (hover: a start would stop both
#    tp1 slots) and the job starts only after they drain + stop.
#    A job with enhance on should log a chat completion on the writer (its RADIANCE_LOG lines).
```

**Day-2 — the Gen tab:** one FIFO, three slots.

- **Job = model + prompt + (video: seconds) + (both: size, seed).** `model` is a purpose key
  (`image` / `video` — mapped to exact models in the tab's settings, admin-editable) or an exact
  model name. Enhancement (the enhance target — default: the writer's `Writer` model — rewrites the
  prompt first, off-slot) is per-job, defaulted by the settings' global toggle; a model without a
  configured prompt can't be enhanced (the checkbox says so), and the server refuses `enhance: true`
  for one — it is not a silent skip at the door.
- **Cancel:** the record is marked canceled immediately, and the in-flight request's connection is
  dropped within ~2 s — radiance treats an abnormally closed response as its own cancel (the
  ticket's flag, the same one the video's `DELETE` sets), so the render retires at the next pass
  boundary instead of burning out; a video render in progress is additionally `DELETE`d by its
  poll within ~5 s. No half-artifact ever lands (the state is re-checked before every write).
- **Gallery (data budget):** the tab is two small polls, never the whole store —
  `GET /api/gen` (counts + the in-flight set — the queue, whatever the operator
  queued, plus the in-flight jobs the admission caps bound; the store cap
  `MAX_JOBS_KEPT` bounds the whole) and
  `GET /api/gen/jobs?limit&cursor&q&filter` (ONE page of history, newest first;
  the cursor is the `job-NNNNNN` seq of the record to resume before, the response's
  `next` carries the next one; `filter` is all/active/image/video/failed, `q` a
  substring over id/model/prompt/enhanced prompt — the server searches the whole
  capped store, the page is the unit of transfer: the tab polls from phones on
  metered lines). The page carries CARD records, not full ones: prompt/error
  clipped (200 chars, the card's tooltip shows the clip), `params`,
  `radiance_video_id` and `enhanced_prompt` dropped — the card previews the
  HUMAN prompt only (the rewrites are machine text, not prose — the Minimax
  ones read “integrated_multimodal_description: [Shot 1]…” — and they are the
  modal's, which shows them on select). The modal and the remix fetch the
  FULL record per id from `GET /api/gen/jobs/{id}` for a terminal job (a
  clipped prompt is exactly what they must never show). The in-flight set in
  `GET /api/gen` stays FULL and is merged into whatever page is showing,
  subject to the same filter/search the page was asked with — it is the
  gallery's live source (progress + the prompt the modal/remix read off the
  poll), so a long video keeps its progress bar on every page of a tab that
  shows it, and the filter tabs (whole-store counts) filter everything on
  screen, not just the fetched records. “Select all” selects what is on
  screen; the batch endpoints below take any ids.
- **The wire (data budget, part 2):** the two polls are ETag/304'd — the etag
  is the response body's hash, so an unchanged store (the idle ticks: nothing in
  flight, no card changing hands, no instance flapping) answers `304 Not
  Modified`, headers only, and the browser's HTTP cache re-serves its stored
  body for the app — the wire transfer is what shrinks, the fetch code path
  is unchanged
  (`Cache-Control: no-cache` forces the revalidation every tick — the poll
  always reaches the server, it never serves a stale body; the UI's `usePoll`
  additionally keeps its data if a 304 ever reaches the app directly, e.g.
  through a layer that passes it through instead of re-serving the cache). And
  the `/api` path is gzipped by Traefik's `compress` middleware (panel-api
  router only): `/api` never reaches panel-web's nginx — Traefik hands it to the
  host service directly — and uvicorn doesn't compress, so without it the
  decoded size IS the transfer size; the one exclusion is the batch
  archive's stored ZIP, which is already compressed.
- **Batch:** the tab's selection acts on many jobs at once. The batch delete applies the same
  per-job rule as the single one (cancellable → canceled, terminal → record + files; one bad id
  never aborts the rest — the response says what happened to each). The download packs the
  selected artifacts in one ZIP (stored — they're already compressed) plus a `prompts.txt`
  manifest, built as a temp file in the artifact dir and unlinked after the response (the age
  GC skips it — non-`job-*` name — and the nginx allowlist can never serve it; a hard kill's
  leftover is reaped by the engine's zip sweep, which only touches names not being served). No id or size
  caps: the admin double gate is the authorization, and the store's own history cap
  (`MAX_JOBS_KEPT`) bounds what any page can select.
- **Lanes:** a running slot owns its cards exclusively; a job becomes eligible when all of its
  slot's cards are free or held by an idle gen slot, and among the eligible slots it is claimed on
  the LEAST-LOADED one that hosts its model (a model on two slots spreads one-each — not two on
  the first). Per slot: at most 2 jobs in the GEN PHASE — one in progress + one queued in
  radiance, which sequences the model's work itself (the video model runs one denoise + one
  conditioning at a time); a starting flight (the slot booting) consumes no gen headroom.
  Enhancement has its OWN cap — 8 concurrent rewrites system-wide (the enhance target is one
  entry; its `max-num-seqs` carries it): it runs OFF-SLOT (the engine starts the target if it is
  down) and BEFORE the slot phase, so a queued job enhances as soon as there is enhance
  headroom, without a slot being claimed or started (a different card: the gen phase never waits
  for the enhancement, and the enhancement never waits for the lane). A job that
  needs a down slot starts it (its assigned cards, or the lowest free set, exactly like the GPUs
  view's *start* — and the same idle-yield covers that free set: a slot with NO usable assigned
  set stops the idle gen slots holding the cards the grab would take, instead of flapping a start
  the pool can't cover); a start that hits a conflict (a card gets held between the claim and the
  attempt — e.g. a job queued in the same tick as its neighbour's) re-queues the job with the
  reason in its error column and the next tick retries: the slot then reads blocked (wait for
  the holder — or lane short: the pool can't cover the slot even after stopping every idle gen
  slot, and the job's error column carries that reason — it names what holds the gap (a busy
  gen slot that drains on its own, an LLM occupant to stop, a fenced card to unreserve) — until
  it clears), down-idle-stops (the holder gets stopped), or free (it starts). A permanent
  conflict (the lane is short, docker down) re-queues with the same reason until it clears. A config drift after a job is
  queued (the model vanished, or its enhance target became misconfigured while the job wants
  enhance) says so in the error column instead of the job waiting in silence. The GPUs
  view's stop does not wait for a drain — in-flight jobs on the slot fail (`lost the slot
  mid-render`); the graceful exit is the lazy stop (a queued job needing the cards
  stops a drained slot itself).
- **Lazy stop:** a drained slot STAYS UP (the gen model is resident; the next job is fast). It
  yields its cards only when (a) a queued job needs them — the slot stops automatically (only when
  drained — nothing in flight is lost), (b) you stop it (GPUs view), or (c) a reboot. An explicit
  start of ANY instance may stop idle gen slots holding the cards it wants — server-side, with no
  UI confirmation: the audit log records it (`stopped-idle-gen:<slot>` on the `gpu-start` entry).
  LLM instance occupants are NEVER auto-stopped (their traffic is not panel-visible, so idleness
  can't be proven): a conflicting start fails with the shortage (`not enough free cards: …
  only N free (…)`) — the GPUs view names the holders; stop them there and retry.
- **Artifacts:** written to `/models/gen-output/job-<n>.(png|jpg|mp4)` (+ `-thumb.webp` for videos)
  when a job completes — the job is `completed` only after the file is on disk (the full download
  is awaited before the write; a slot that dies mid-transfer fails the job before anything is
  written). Served at
  `https://panel.pietrasik.top/gen/<file>` (ro bind into panel-web, nginx allowlisted to the
  `job-*` names) and via `GET /api/gen/jobs/{id}/content` (the API shape the servitor agent will use
  in V2). No size cap; an age GC (settings, default 14 days) sweeps the dir
  every 6 hours — it skips the artifacts of jobs still in the store (a record
  the tab still shows keeps its file even past gc_days). Deleting a terminal
  job removes its files with the record; a record trimmed past the 500-job
  history cap orphans its files, and this pass takes them on the first sweep
  after they age past gc_days.
- **Settings** (Gen tab, or `PUT /api/gen/settings`): the purpose→model map, the global enhance
toggle, the per-model enhancement prompts, the ENHANCE TARGET (instance + model — default
`gpu-writer` / `Writer`), the GC age. State lives in `/opt/panel-api/gen-settings.json`
(panel 0600) + `/opt/panel-api/gen-jobs.json` (queue + jobs, 0600) — both in the update snapshot,
like the LLM router's settings. A model rename/swap leaves the old name's prompt entry behind:
the *prompts* tab of the settings dialog lists it under **stale entries** (its text can be copied
into a current model, the entry deleted — one by one or all) — the PUT, and with it the tab's
save, is rejected while any stale entry is in the draft, so that section is where a model
change gets cleaned up, not a hand-edit of the file.
- **V2 (servitor):** the same endpoints re-exposed under `/machine/gen/*` with the bearer
`LLM_API_KEY` (no Authelia) — one handler set, two gates. Not in this release.

**Troubleshooting:** `make doctor` checks the gen state-file modes, the `/models/gen-output`
ownership (panel:panel — a docker-created root-owned dir turns the check red), and that each private
slot's config actually hosts an image/video model (a typo'd path silently makes it a plain instance
— no gen tab, no error). Video jobs poll every 5 s; a panel-api restart mid-render re-attaches to
the recorded radiance video id while the slot is still up (the video lives in radiance's memory — a
slot that went down with it fails the job saying so); a restart before the gen request (a job still
starting/enhancing/loading) re-queues it — it resumes on the next tick — and a cancel DELETEs the
radiance side within one poll. A slot that never becomes healthy within 10 min fails its jobs with the
reason — check `docker logs panel-gpu-gentp*` first (model file missing, OOM on the tp2 pair without
the vision-tower-ram placement, or a `.rad` radiance refuses — 2.x's int8/bf16 container rule,
see **Radiance config** above).

**Fallback router (`llm.pietrasik.top`):** one base URL for clients that just want the best model
currently serving — agents, defaults, anything that shouldn't pin an instance. Set the priority
order in the panel's Settings view: a request goes to the FIRST instance in the list that's up
(strictly single-active — the order is priority, traffic is never split). The order holds the
compose SERVICE name (`gpu-27b` — not the `panel-gpu-27b` container name compose prefixes);
panel-api resolves name → container (the compose service label) → edge IP (`edge-ips`).
Clients use
`base_url=https://llm.pietrasik.top/v1` + the same `LLM_API_KEY` as every instance; `/v1/models`
reflects the active instance only. Served by panel-api itself (router `fleet-fallback` in
`traefik/dynamic/llm.yml` → the `panel-api` service in `panel.yml` → the `172.17.0.1:8010` host
bind — the router omits `auth-chain` like the instance names; the gate is the bearer key).
CHAT-LLM INSTANCES ONLY: the order routes chat /v1, so image/video generators (same fleet,
/v1/images + /v1/videos on their own hosts) never go in it — they would 501 on it.
The router is scoped to `/v1`: everything else on the llm
host 404s, so the admin `/api/*` stays behind `panel.pietrasik.top`'s Authelia + `panel-secret`
chain — on the llm host a client could supply those headers itself. Health is each instance's
own `/health`: probed every 30 s, and a failed proxied request (connection error or 5xx) parks
that instance for 30 s, so failover lands
within a couple of probe cycles even between probes. All instances down → 503 with a per-instance
reason; an empty order → 503. The order lives in `/opt/panel-api/settings.json` (panel-owned 0600,
in the update snapshot — `make rollback` restores it). The `edge-ips` panel-do action supplies the
name→IP map (read-only, no args) — first deploy needs `make panel-do` (it ships the new binary)
BEFORE `make update`: with a stale binary the action is unknown, so the router reports every
instance “not on the edge network” (a 503 the route check still calls healthy). `make doctor`
checks the route (200 or 503 = healthy), that unauthenticated `/v1/*` gets 401, and that the
registry carries `edge-ips` (catches the stale binary).

**GPU pool** — per-instance card sets, auto-grab, boot autostart.

The panel's **GPUs** tab is the control surface: a tile per card (free / held by whom /
reserved for whom, temp, VRAM, utilisation) and one row per fleet instance with its **assigned**
set (the ledger's intent), its **holding** set (the live truth — drift between the two means a
failed apply or a hand-run compose), start/stop, an **auto** button (grab the lowest free cards,
sized by the instance's `gpu.cards` label — a card reserved for that instance comes first),
a **pick…** row for exact card sets, a **boot** checkbox, and a **reserve a card…** form
(the dedicated-card fence — see the bullet below).

- **State:** the LEDGER `/opt/panel-api/gpu-pool.json` (panel-owned 0600) holds `allocations`
  (instance → card set) + `autostart` (ordered list) + `reserved` (card → {for, note, bdf} — the
  dedicated-card fence, see below). The OVERRIDE `/opt/panel-api/gpu-pool-override.yml`
  (0644) is its compose projection — `ROCR_VISIBLE_DEVICES` per instance — and is included in EVERY
  compose render (`COMPOSE_STACK` in setup/common.sh, panel-do `gpu-apply`/`gpu-stop`/`start-service`): a `make update`
  recreate (radiance floats on `latest`) therefore keeps the assignment instead of reverting to the
  base-file default. Both files are re-rendered by panel-api from the ledger (never hand-edit), and
  both ride the update snapshot — `make rollback` restores them.
- **Live holds** are read from the running containers' env (panel-do `gpu-live`), never from the
  ledger: a stopped instance holds nothing, and `assigned`/`holding` are shown separately.
- **Reservations fence a card** — dedicate it to one instance (the physically-odd card, e.g. the
  PCIe5 x8 one, that must never be mixed into the others' pool): GPUs view → *reserve a card…*
  (card + instance + a note, e.g. `PCIe5 x8`). Then every auto-grab path (the **auto** button, a
  GPU-aware start, the boot planner, the Gen engine's instance starts) treats the card as not free
  for anyone but the owner, an explicit **pick…** of it by another instance is refused with the
  owner + note named, and the OWNER's own grab lands on its reserved cards first (a first start of
  the writer takes tile 4, not tile 0). The owner may still take other free cards; the reservation
  holds nothing itself and is never written to the OVERRIDE — it is a constraint in the LEDGER,
  audited like every other mutation (`gpu-reserve` / `gpu-unreserve` in journald). A SAVED set
  that predates a reservation is fenced the same way: if the dedication lands on a card another
  instance's saved set already names (a card add renumbers the tiles under it), that instance's
  next start/boot does NOT take the saved set — it falls back to the reservation-aware grab — so
  the fence can't be bypassed by a set saved before the card was reserved. The BDF is
  captured at reserve time and re-verified on every view: the warnings section names a stale
  reservation — the card left the pool, its BDF moved (a card add/remove renumbers the tiles, so
  the dedication may now point at the wrong physical card), the owner left the fleet, a
  hand-run compose pinned a foreign instance on it, or another instance's saved set still names
  it. A stale reservation keeps fencing the card
  (safer than opening it to the auto-grab) until you unreserve it; re-reserving the same owner
  re-records the BDF. Unreserving = the card rejoins the auto-grab pool for everyone. The same ops
  as JSON: `POST /api/gpus/reserve {card, for, note}` / `POST /api/gpus/unreserve {card}`.
- **Assigning cards (re)creates the container** — GPU env only exists at create time — so "set cards"
  and "run on them" are one operation (brief downtime + model reload on a running instance). Conflicts
  (a card held by another RUNNING instance, a card reserved for another instance, wrong count for the
  instance's `gpu.cards`, out-of-range index) are refused
  with the reason; the instance's own current cards are allowed (a move or a no-op recreate).
- **Count drift is warned, not silently run:** a saved set whose count no longer matches the instance's
  `gpu.cards` (e.g. after a config edit without a re-assign) shows in the panel's warnings — a
  GPU-aware start self-heals by re-grabbing the right number of free cards, but a hand-run `up -d`
  would pin the old count and the engine would fail to load. Reassign (auto) or fix the label/config.
- **Starts are GPU-aware everywhere**: the GPUs view's *start*, and the Services view's *start* /
  *create + start* for gpu-* services, all go through `POST /api/gpus/start` — the saved set when
  it is free, else the lowest free set (recorded in the ledger). A raw `docker start` is deliberately
  off the panel's path: it would reuse the create-time env and could resurrect cards another instance
  now owns. Stop is plain (the container is kept; cards free on exit; the assignment is remembered).
- **Boot autostart:** `gpu-autostart.service` (oneshot, User=panel, after docker; installed + enabled
  by `make api`, no-op when the list is empty) runs `panel-api/gpu_autostart.py`, which walks the
  autostart list IN ORDER: an instance already up is left alone; a saved set that is free is honored;
  otherwise the lowest free set is grabbed and recorded (the ledger converges on what actually
  starts). So the boot set is conflict-free by construction — whatever fits comes up; a set that
  doesn't fit is skipped with the reason in `journalctl -u gpu-autostart`, and shows down in the
  panel. Overlapping saved sets are warned about in the panel (autostart warnings) but never block
  saving — boot resolves them.
- **Privilege path** (all panel-do, audited in journald like every other action): `gpu-fleet`
  (instances + cards needed, read-only), `gpu-live` (live containers + holds, read-only — it extracts
  ONLY the ROCR entry from the inspect env; the API key is never logged or forwarded), `gpu-apply
  <svc>` (`compose up -d` with the override), `gpu-stop <svc>` (`compose stop`). `make doctor` checks
  the registry carries `gpu-apply` (stale-binary tripwire) and that the unit is enabled.
- **Multiple instances of one model:** a second `gpu-27b` is just another instance (see **Add an instance** above) —
  e.g. `gpu-27b` on 0,1 and `gpu-27b-1` on 2,3, each with its own subdomain, each assignable
  independently here.

## Web search (SearXNG)

Self-hosted metasearch: an agent-facing keyed JSON API (`search.pietrasik.top`) and a private
browser UI (`searx.pietrasik.top`, the Authelia `*.pietrasik.top` two_factor wildcard — any 2FA
user, zero ACL edits, droppable later — the agent path is byte-identical without it). The API
gate is `panel-api`'s `/gate/search` — a **key checker, not a proxy** (forwardAuth's contract and
the no-proxy rationale are in `search.yml`'s header) — and the key is the fleet's `LLM_API_KEY`,
reused on an owner decision: there is no search-only rotation (rotating it re-creates the `gpu-*`
containers), and a leaked key grants search too.

### Agent contract

- `GET` or `POST` `https://search.pietrasik.top/search`, `Authorization: Bearer <LLM_API_KEY>`
  (Basic works too — `user` ignored, password = the key — for Basic-only clients).
- Query params pass through to SearXNG: `q`, `format=json` (mandatory — it is in the deployed
  `formats`, 403 without it; `make doctor` probes it), `categories`, `engines`, `language`,
  `pageno`, `time_range`, `safesearch`.
- Response: SearXNG's native JSON — `{"query":…, "results": [{title, url, content, engine, …}],
  "number_of_results":…, …}`.
- The host is **key-gated whole, not path-scoped**: `mcp-searxng` probes `/config` +
  `/autocompleter` on the same host, and whole-host is safe because the host exposes no
  state-changing surface (instance state lives in `settings.yml`) and no arbitrary-fetch surface
  (`/image` needs an HMAC under the sed-filled instance secret; `/metrics` needs its own
  password).
- **POST keeps the query out of the request line** — query params in GET URLs leak into logs and
  proxies (see the access-log note below). Prefer POST for query-sensitive clients; pi-web-access
  is GET-only.
- No Authelia on this host: the key is the identity. The browser UI is the separate `searx.`
  host behind the wildcard.

### Traefik version gate (landing check)

`search.yml` uses two version-gated forwardAuth keys (`maxResponseBodySize` ≥ v3.6.9;
`trustForwardHeader` valid through v3, deprecated v3.6.14+, gone in v4). An unknown key is a
parser decode error that voids the **whole file provider** — on a traefik restart with the bad
file on disk, every host 404s with traefik itself healthy (full edge outage; see the
troubleshooting row). Before landing, print the version and verify BOTH constraints (major v3
AND ≥ v3.6.9); if it fails, do not ship the file — defer the feature (or the offending key) as
its own change:

```bash
sudo docker exec traefik traefik version
```

### Status table (attribution — one place)

| code (keyed, on `search.`) | reading |
|---|---|
| 400 | authenticated + configured + `json` present — the `No query` 400 is the healthiest possible answer (zero engine queries) |
| 401 | credential — key mismatch (repo `.env` vs deployed) or unkeyed |
| 403 | `json` not in the deployed `formats` — the silent misconfiguration; re-run `make update` |
| 404 | router missing — may be the **whole file tree** (a decode failure — see the troubleshooting row) |
| 500 | the gate was UNREACHABLE (panel-api down/restarting — forwardAuth on an unreachable auth server is a 500, not a 503; `pkg/middlewares/auth/forward.go`, verified on v3.6.14 AND v3.7.9) or a gate-handler fault |
| 502 | searxng process faulting mid-request |
| 503 | the gate answered key-unset, OR no healthy searxng server (router matched, no backend ready) |
| unkeyed 400 (no `q`) or 200 (with a `q`) on `search.` | **the gate middleware fell off — WIDENED ROUTE, incident**: SearXNG answering keyless is 400 `No query` without a `q` (the doctor's probe) and 200-with-results with one — a 200 on the no-q probe instead means a different backend now serves the host: restore `search-gate` in `search.yml` |
| unkeyed 302 on `search.` | auth-chain drift — an Authelia router now matches the API host; audit `search.yml` |
| keyed `/metrics` ≠ 404 | `open_metrics` was set — if to the LLM key, the fleet key now opens Prometheus; clear it in `compose/searxng/settings.yml` + `make update` |

(No 405 row: the forwardAuth hop is GET by construction — the gate handler declares all methods
for hardening, but the router never sees a method-rejected request.)

**Attribute a public 503/500 with the direct gate probe** `curl http://172.17.0.1:8010/gate/search`
(exactly 401 when configured; the doctor runs it):

- it says **503 itself** → `LLM_API_KEY` missing from `/opt/panel/.panel-api.env` → the public
  503 is the gate's own (`make api` re-derives the reduced env);
- it says **401** → gate configured → the public 503 is downstream of the gate: no healthy
  searxng server (check `sudo docker compose --project-directory /opt/panel ps searxng`);
- it **refuses / times out / 404s** → panel-api down or stale code (a 404 = the pre-gate binary,
  `make api`) → the public 500 is the unreachable gate.

### Rotating the keys (reuse-aware)

- **`SEARXNG_SECRET`** (the instance secret — cookie signing, `/image` HMAC): repo-`.env` edit +
  `make update`. The sync's sed re-fills the deployed `settings.yml` (chown/chmod re-land), and
  the restart-on-change hook restarts searxng — start-time-only config, no re-create.
- **`LLM_API_KEY`** (the GATE key — and the fleet's): the existing fleet procedure (repo `.env`
  edit + `make update`, `gpu-*` re-create expected). It re-keys every search client at the same
  time — the `web-search.json` / MCP config on each machine.
- Rotate the **repo** `.env`, never the server copy: a full rollback restores `.env` + both
  reduced env files from the update's snapshot with no re-derivation.

### Access-log note

Traefik's `accessLog: {}` **keeps query strings** — agents searching via GET (pi-web-access
does) leave their queries in the docker json-file log. Query-sensitive clients should POST
(query in the body, not the line). Deliberately NOT touching `traefik.yml` in this change: the
drop-the-query option needs traefik ≥ 3.7.3 under a floating v3, and the static file is
start-time config → a restart of the edge. Not worth it for this traffic.

### The shared-bucket note (accepted)

`search.` and `searx.` draw on the **SAME** `search-rate` bucket (2/s sustained, burst 10 —
sized to the 4-thread capacity ceiling, not to inference). An agent burst plus a UI page load
(document + `/config` + bundles) can 429 the load's tail. It self-heals on refresh (the bucket
refills at 2/s), and a 429 on the UI is the limit working, not a fault. If it gets noisy: raise
the burst, or give `searx.` its own rateLimit middleware (separate state). The limit keys on
the socket address (never client XFF — see `search.yml`'s header): behind the one proxy it is a
per-client-IP flood brake, and the key is the identity control.

### Image-bump duty

The image floats on `latest` — rolling date tags, no releases (declared OUTSIDE the
declared-line policy, the radiance precedent; see the "The lines" table row). Before an update
lands a new build:

1. Diff `compose/searxng/settings.yml` against the file the new image ships — under
   `use_default_settings`, an upstream default flip is a silent behavior change (`formats` is
   the live example).
2. Diff the image's `searx/settings_defaults.py` `SCHEMA`: the `environ_name` list (a new
   env-override name is a new channel over our settings.yml) AND the default values (a flip for
   a key our settings.yml doesn't set is invisible in the shipped-file diff; the doctor's
   probes cover the security-critical ones).
3. Confirm the app uid is still 977 — the compose `user` pin, the sync's `chown` target and the
   doctor's owner check all name it.
4. Re-run the effective-secret probe (the doctor carries the file-comparing variant at day-2 cadence):

```bash
sudo docker exec -w /usr/local/searxng "$(sudo docker compose --project-directory /opt/panel ps -q searxng)" \
  /usr/local/searxng/.venv/bin/python -c \
  "from searx import settings; s=settings['server']['secret_key']; print('OK' if len(s) >= 32 and s not in ('ultrasecretkey','__SEARXNG_SECRET__') else 'FAIL')"
```

Hold a known build: pin the tag in `compose/docker-compose.yml`. A build that passes the gates
but breaks behavior: `make rollback WHAT=searxng`.

### Rollback split

- **Images only** (`make rollback WHAT=searxng`): a bad build — the settings file (secret-filled
  on disk) is untouched.
- **Full** (`make rollback`): a settings/route regression — the config tree, including
  `searxng/settings.yml`, is part of the snapshot and is restored with the secret already in it.

### First-landing transient (expected, fail-closed)

Between update step 3/7 (route hot-reload lands `search.yml`) and 6/7 (panel-api re-deploy
installs `/gate/search`), the API host answers denied — the forwardAuth target 404s and the gate
copies that to the client. Fail-closed, zero users, seconds long. **Do NOT pre-run `make api`
before the update**: the 1/7 snapshot would then anchor the NEW gate code, and a rollback would
silently keep it.

### The ownership warning (pre-empted)

The container runs as uid 977 (the image's own uid; the image sets no `USER`). The sync's tar
leaves the config DIR non-977, so every start prints ONE benign line — `/etc/searxng is not
owned by "searxng:searxng"` — and continues (the data volume seeds 977:977 from the image and
stays quiet). **Do not "fix" it**:

- dropping `user: "977:977"` → the app runs as root, and with `FORCE_OWNERSHIP=true` it would
  `chown -R` the repo-synced tree every start;
- hand-`chown`-ing under `/opt/panel` → undone by the next tar sync. The sync's OWN
  `chown 977:977` of the deployed `settings.yml` (landed with the chmod, every sync) is the
  sanctioned chown — a hand chown elsewhere is the one that gets undone;
- making the config bind writable to silence it → the bind is `:ro` because the app never writes
  the config dir in this mode (we ship `settings.yml`, so the template copy never runs).

The warning is expected output, not an error — ONE line per start, naming the config dir only.
The container log accumulates one line per start (and rotates at 10m×3), so count per start:
(a line naming the data volume, or **two lines in a single start**, is a real problem — check
the mount + the volume.)

### Rollback residue (pre-searxng rollback)

Config restore is additive and the file provider hot-reloads: after a rollback to a
pre-searxng snapshot, `search.yml` STAYS live on disk, pointing at a gate/container that no
longer exist — fail-closed in two places (the gate 404s → the client gets 500; the container is
gone → no backend). Cure = a later `make update` (re-syncs + prunes the stale route), or
`sudo rm /opt/panel/traefik/dynamic/search.yml` now (the watcher reloads). The searxng
container itself is dropped by the rollback's recreate (`--remove-orphans`); the
`searxng-cache` named volume survives (runtime data is never auto-deleted) — remove it with
`docker volume rm panel-searxng-cache` if you want it gone.

### Client setup

**pi-web-access** (current latest: 0.48.0 — its SearXNG provider speaks exactly this endpoint:
`GET {base}/search?q=…&format=json` (+ `time_range`)). Per pi machine, NOT in the repo — merge
into the `web-search.json` that pi ACTUALLY reads (**chmod 600 — it holds the key**): pi checks
`~/.pi/agent/web-search.json` first and falls back to the legacy `~/.pi/web-search.json` only
while the agent-dir file is absent — so if both exist, a field written to the legacy file is
silently shadowed and the routing never engages:

```json
{
  "exaApiKey": "…",
  "searxngBaseUrl": "https://search.pietrasik.top",
  "searxngHeaders": { "Authorization": "Bearer <LLM_API_KEY>" },
  "searchRouting": {
    "providers": ["searxng", "exa"],
    "fallbackOn": ["network", "transient", "quota", "invalid-response", "empty"]
  }
}
```

- `searxngHeaders` carries the key (the extension has no native SearXNG key concept; the hook
  exists for reverse-proxy auth). Same value the fleet clients already hold.
- A selected-provider failure does NOT cascade to Exa implicitly — `searchRouting` is the
  explicit fallback. `auth`-kind (401) is deliberately not a valid fallback kind: a wrong key
  stays loud.
- The extension is **GET-only with the query in the URL** — see the access-log note above.

**mcp-searxng** — Basic-only client (the gate's Basic branch exists for it). stdio, client-side
only — an MCP server container on `edge` would be an SSRF pivot into panel-api/authelia/LAN:

```bash
pi mcp add searxng -- npx -y mcp-searxng
```

with the instance URL as `https://<anything>:<LLM_API_KEY>@search.pietrasik.top` (URL
userinfo). Behavior verified against 2.10.1 (current latest at plan time; `npx -y` floats on
latest by preference — the design is robust to change: whole-host routing serves its
`/config` + `/autocompleter` probes and the gate answers both schemes; the fallback is curl):

- SearXNG instance auth is HTTP Basic only — URL userinfo or the `AUTH_USERNAME`/
  `AUTH_PASSWORD` env fallback — applied to all three endpoints (`/search` + the two probes).
- URL userinfo needs a **non-empty username**: 2.10.1 treats password-only userinfo as ABSENT
  (silent fallback to the `AUTH_*` env) — hence `<anything>:`.
- The username is ignored by the gate; the password IS the key. Percent-encode the key first
  if it carries URL userinfo specials (inert for a hex key).
- The 2.x static-bearer/OAuth options protect the MCP server's own HTTP transport, not the
  SearXNG instance connection (and the recipe runs stdio, where there is no transport at all).

**Raw**: `curl -H "Authorization: Bearer $LLM_API_KEY" …/search?q=…&format=json` (POST is a
first-class verb).

### Verification (post-deploy / post-update)

`make doctor` runs the whole matrix — it must stay engine-free (every probe runs before any
engine contact; the doctor runs twice per update, from the home IP, against engines that
throttle):

| probe | expect | proves |
|---|---|---|
| unkeyed `GET https://search.pietrasik.top/search?format=json` | exactly **401** | the gate answers (302 = auth-chain drift; 400 = gate fell off = WIDENED ROUTE, incident — this probe has no `q`, so a widened route is 400 `No query`, never 200) |
| keyed `GET https://search.pietrasik.top/search?format=json` (no `q`) | exactly **400**, JSON body containing `No query` | credential + Traefik→gate→searxng chain + `json` in deployed formats — ZERO engine queries |
| keyed `GET https://search.pietrasik.top/healthz` | exactly **200**, body `OK` | liveness disambiguator (a 503 here is ambiguous between gate-unconfigured and no healthy server) |
| keyed `GET https://search.pietrasik.top/metrics` | exactly **404** | `open_metrics` still empty — any other code means it was set |
| `GET http://172.17.0.1:8010/gate/search` | exactly **401**, NEVER 503 | gate CONFIGURED (503 = `LLM_API_KEY` missing from the reduced env; 404 = old panel-api, run `make api`) |
| unkeyed `GET https://panel.pietrasik.top/gate/search` | 30x for an unauth curl (Authelia); an authed browser gets the SPA 200 — NEVER 401/503 gate JSON | negative reachability — the root-level placement is one future panel.yml edit from becoming a key oracle |

Every FAIL names the code expected and the code seen; `code != 401`-style assertions are
forbidden in these checks (403/404/502/503 would all pass one). A **429** on the keyed probes
is the shared `search-rate` bucket consumed by live traffic — wait a few seconds and re-run.

Plus the container-side checks (doctor, always on when the feature is deployed): `settings.yml`
owner `977:977` + mode `600`, zero `__*__` tokens in the deployed file, the
`/opt/panel/searxng → /etc/searxng (ro)` bind intact (a dropped bind boots healthy on the
anonymous-volume template — the mount check is the only thing that catches it), the
effective-secret probe (post-init value compared against the deployed file inside the container —
catches the env-override mode the file checks can't see), and the env allow-list (the compose
`environment:` block carries no guard key — that absence IS the secret path).

**Manual one-offs (never from the doctor — it must stay engine-free)**:

```bash
# one real search (the doctor's matrix proves the chain without touching an engine):
K="$(sudo grep '^LLM_API_KEY=' /opt/panel/.panel-api.env | cut -d= -f2)"
curl -s -H "Authorization: Bearer $K" \
  "https://search.pietrasik.top/search?q=traefik+healthz&format=json" | jq '.results | length'   # > 0
```

- **Lock falsification** (the `image_proxy` lock is structural — falsify it, don't trust it):
  in a fresh browser/cookie jar with `image_proxy=1` set (UI preferences or a bare `&image_proxy=1`
  on a keyed `/search`), the result URLs must STILL be plain — no `/image` path.
- **Ownership log line** (manual — deliberately NOT a doctor check: the line lands once PER
  START and the json-file log accumulates across restarts and rotates at 10m×3, so a bare count
  is only meaningful against a known start count):
  `sudo docker compose --project-directory /opt/panel logs searxng | grep -cF 'not owned by "searxng:searxng"'`
  → expect **1 per start** (1 on a fresh container — an image-float update re-created it — plus
  one more per restart since). The line names the config dir (`/etc/searxng`) only; the data
  volume seeds 977:977 and stays quiet (the embedded quote is part of the pattern). A line
  naming `/var/cache/searxng`, or **two lines in a single start**, is a real problem — check the
  mount + the volume.
- **From a configured pi machine**: `web_search({ query: "traefik healthz", provider: "searxng" })`
  — the real client against the real endpoint.
## Servitor (agent VM)

The [servitor](https://github.com/ConnorPietrasik/servitor) project — a home
assistant that runs pi (a coding agent) in a libvirt VM on the edge segment —
is hosted on this server. The VM is a **raw IP upstream in one traefik route**,
not a container: the file provider reaches it at `SERVITOR_VM_IP:8787`, the
auth chain (`auth-chain@file`) gates the user and the `servitor-edge`
middleware injects the edge credential the app re-verifies on every route
(invariant 3 in the servitor repo — the app authenticates no user, and the
credential is what stops a neighbor container from driving the agent: the
next line explains why reachability alone is not enough), and the VM's own
nftables admits 8787 from the WHOLE edge subnet (the proxy's source IP is
indistinguishable from a neighbor's — hence the credential) + 22 from the
host's edge-bridge IP /32 + the ops subnet (the host's own ssh to the VM
sources its edge-bridge IP — the kernel picks the output-interface address
for the connected route — so neither LAN-only nor the full edge segment is
right). Invariant 2 holds: the route is a file, no docker.sock
anywhere. Day-2 control is four panel-do actions (`servitor-status`,
`servitor-vm-restart`, `servitor-sync`, `servitor-doctor`); the scripts they
run live in the servitor repo's host checkout at **`/opt/servitor-repo`**
(a separate repo — keep that path, the actions are pinned to it).

`make doctor` has a tri-state section for it (absent = skip, no libvirt /
no domain / stale binary = fail, VM stopped or endpoint dead = warn —
planned downtime must not red-light every update). It also verifies the one
host firewall line below.

### One-time install (short version)

The servitor repo's `deploy/portcullis/INSTALL.md` is the step-by-step
reference (image build, the domain template, the failure-mode
table distilled from the dev box). The host-side shape, in order:

```bash
# 1. Host packages (none of this exists on a fresh box; rsync/zstd/curl
#    are the guest image build's needs, nodejs/npm also build the chat UI)
sudo pacman -S --needed libvirt qemu nodejs npm git e2fsprogs rsync zstd curl
sudo systemctl enable --now libvirtd

# 2. Servitor checkout + the single-purpose deploy key (NOT your SSH key).
# The key lives in ROOT's home: panel-do's scrubbed env has no $HOME, so the
# actions pass this exact path; sync.sh's ${HOME} fallback can never work
# under panel-do.
sudo git clone git@github.com:ConnorPietrasik/servitor.git /opt/servitor-repo
sudo mkdir -p /root/.srv && sudo chmod 700 /root/.srv
sudo ssh-keygen -t ed25519 -N '' -f /root/.srv/deploy_key   # pub half gets baked into the image

# 3. Servitor .env (the VM's contract: keys, subnets, VM IP, versions)
sudo cp /opt/servitor-repo/.env.example /opt/servitor-repo/.env
sudo chmod 600 /opt/servitor-repo/.env
#    — set OMNISSIAH_API_KEY (= the fleet's LLM_API_KEY), EXA_API_KEY,
#      SERVITOR_EDGE_KEY (the edge credential, invariant 3 over there —
#      generate ONE value, e.g. openssl rand -hex 32, and set the SAME
#      value in THIS repo's .env too: the servitor-edge middleware's
#      source; a mismatch 403s the whole UI, `make doctor` cross-checks),
#      SERVITOR_EDGE_SUBNET (from: sudo docker network inspect edge -f
#      '{{range .IPAM.Config}}{{.Subnet}}{{end}}'), SERVITOR_OPS_SUBNET
#      (your LAN subnet), SERVITOR_VM_IP (unused address in the edge subnet;
#      gateway is the subnet's .1 by build convention) and SERVITOR_BIND
#      (= SERVITOR_VM_IP). The template already carries the quoted
#      SERVITOR_DNS_UPSTREAM default — multi-word values must stay QUOTED
#      (sync.sh sources the file); the rest may stay at defaults.

# 4. Guest image (the Makefile target runs as the invoking user — the build
#    needs root, so the sudo is on `make`)
cd /opt/servitor-repo
sudo make guest   # = unshare -m deploy/guest/build.sh --env .env --deploy-key /root/.srv/deploy_key.pub

# 5. State disk (ext4, LABEL=servitor-state — the image's fstab contract)
sudo mkdir -p /opt/servitor-disk
sudo qemu-img create -f raw /opt/servitor-disk/servitor-state.raw 200G
sudo mkfs.ext4 -q -L servitor-state /opt/servitor-disk/servitor-state.raw

# 6. Domain + start (template: deploy/guest/servitor-domain.xml; substitutions
#    in its header; the image has no bootloader — libvirt boots it via
#    <kernel>/<initrd> exactly like the test harness)
sudo virsh define <substituted-template> && sudo virsh start servitor
sudo virsh console servitor   # watch the first boot; Ctrl-] to leave

# 7. Guest egress — the ONE host firewall change servitor needs: the VM is
#    not a docker container, so docker's FORWARD rules never see its traffic
#    and the host's ufw FORWARD policy (default deny routed) applies.
#    /32 = the VM's exact address; `make doctor` checks it against the
#    route file's upstream IP.
sudo ufw route allow from <SERVITOR_VM_IP>/32 comment 'servitor VM egress'

# 8. First sync = provisioning (keys + extension + config + doctor)
cd /opt/servitor-repo && sudo deploy/sync.sh
#    (as root: .env + deploy key are root-owned and root's $HOME is where
#    the scripts find both by default) — success marker:
#    "sync: OK — shape deployed, doctor GREEN"

# 9. Edge merge (this repo): copy the servitor route from the servitor repo's
#    deploy/portcullis/traefik-dynamic/servitor.yml into compose/traefik/
#    dynamic/, substituting the VM ingress IP + port (8787). Commit it in
#    this clone (the deploy prunes deployed route files absent from the repo
#    — hand-placed files don't survive an update). The panel-do actions and
#    the doctor section are already in this repo.
sudo -u "$SUDO_USER" make panel-do   # only if this clone pre-dates the servitor actions
make update                          # gated: sync + recreate + doctor gate

# 10. Verify: https://servitor.pietrasik.top (2FA → chat UI → one live
#     prompt), panel → servitor-status (vm running + healthz ok),
#     servitor-doctor (GREEN). Client machines: tools/panel-hosts.sh picks
#     the new name up automatically (it reads the routes).
```

### Day-2

| task | how |
|---|---|
| VM state / app liveness | panel → `servitor-status` (virsh state + /healthz probe) |
| planned reboot | panel → `servitor-vm-restart` (ACPI reboot; the app's boot reconciliation handles the unclean path — restart, never destroy) |
| edit prompts/skills/lib | edit `agent-config/` / `lib/` in `/opt/servitor-repo`, panel → `servitor-sync` (never blocks a run — an active run gets the update-waiting flag and exit 0; the app's tick applies at the next idle moment) |
| deploy gate | panel → `servitor-doctor` (guest certification + firewall probes + one live prompt + one live search) |
| update pi / extension / guest userland | it runs itself as the best-effort tail of portcullis's own `make update` (host gates pass first; its failure never rolls back the host stack — `SERVITOR_UPDATE=0` skips it), or `sudo make update` in the host checkout (SSH — long, network-bound; floats each selection to the newest release in the same major, reports out-of-major releases instead of applying them, rebuilds + swaps the image when the userland outgrows the 7-day freshness floor so the guest kernel updates with it, its own conformance gates + auto-rollback; deliberately NOT a panel action). First run on a fresh server: the gate is host-side, so the checkout needs `sudo make venv` + the `slot1` user (servitor INSTALL.md §10) |
| rotate keys | host `.env` → servitor `deploy/sync.sh` (re-injects the key slice into the guest's 0600 env file) + restart; the portcullis stack's copy of the key floats with its own `make update` as usual. The edge key (`SERVITOR_EDGE_KEY`) rotates on BOTH sides: this repo's .env (the middleware's source) + `/opt/servitor-repo/.env` (the guest's copy), then `make update` here + a servitor-sync — the doctor's key probe catches a half-rotation |

The four actions exec `virsh` and the checkout's `deploy/sync.sh` /
`deploy/doctor.sh` as root, and portcullis's `make update` servitor tail
(`setup/08-update.sh`) root-execs `deploy/update.sh` (which in turn
root-execs `deploy/guest/build.sh` / `sync.sh` / `doctor.sh`, and runs the
executed python `deploy/resolve_target.py` under `python3`) — so those scripts
(their directory, and every ancestor up to `/`) must not be
group/other-writable and must not be owned by `panel` (`checkManagedScript`
for the actions, the bash mirror `managed_script_check` for the tail, which
runs outside panel-do — plus the file-level `managed_data_check` for
`resolve_target.py` — run under python3 by update.sh, its shebang never
the exec path; the same anti-swap standard as the auto-update clone).
If the checkout is ever relocated, move it under a non-writable tree — a
group/other-writable directory anywhere in the chain is a replaceable root
script.

## Add a privileged action

One Go function in the ACTIONS section of `panel-do/main.go` + one registry entry, then `make panel-do`. The UI picks it up from
`--list` within ~60s; the API needs no change. The rules are in the comment block at the top of the ACTIONS section (violations =
security bug) — in short: exec only via `runCmd(ctx, "/absolute/path", …)`; args arrive pre-validated as opaque tokens and are never
file paths; no `os.Getenv`, no network, no goroutines; separators like `--` are hardcoded inside `runCmd` calls. Before deploying,
check:

- [ ] `git diff` shows changes only inside the ACTIONS section
- [ ] every `Run` calls only `runCmd` — no direct `exec.Command`, no `os.Open`/`os.Create`/`os.Getenv`, no goroutines, no network imports
- [ ] all regexes anchored (`^…$`), arity exact, no optional/variadic args
- [ ] no action treats an arg as a path or places an arg before a hardcoded `--`
- [ ] timeout sane for the action; destructive ones flagged `Confirm: true`
- [ ] `make panel-do` passes (gofmt + `go vet` checks + negative-test suite)

## Hardening checklist

**Do once** (one-time host setup — `make doctor` verifies both and prints the exact commands if
either is missing; on CachyOS both are distro defaults, so this is usually a no-op):

- **persistent journald** — panel-do's audit trail lives there; stock Arch is volatile by default.
  Note CachyOS caps the journal at 50 MB (`SystemMaxUse` in
  `/usr/lib/systemd/journald.conf.d/00-journal-size.conf`) — hours of a busy box, not days; bump
  it via an `/etc/systemd/journald.conf.d/` drop-in if the audit trail must outlast that.
- **fstrim.timer** — weekly SSD trim (HDDs don't need it).

**Keep true** (invariants — `make doctor` checks what it can; the rest holds by review):

- [ ] The clone stays the source of truth — edit tracked files in the repo (commit before `make update` for a rollback point),
  never hand-edit the deployed copies: updates sync repo → `/opt/panel` one-way (stale managed traefik configs get pruned).
  `.gitignore` keeps `.env`, built binaries, and caches out of git.
- [ ] UFW active and configured per [UFW](#ufw-required) (Phase 2) — **required on this host** (no firewalld/iptables fallback); only
  80/443 public; `172.17.0.1:8010` never reachable externally.
- [ ] No container mounts `/var/run/docker.sock` — not even `:ro` (a ro-mounted socket still grants full Docker API access).
  Traefik runs the file provider precisely so it needs none.
- [ ] `/opt/panel/authelia/db.sqlite3` mode 600 (TOTP seeds — authelia creates it 0644 at first boot; `make deploy`/`make update`
  re-tighten it, `make doctor` checks it).
- [ ] Servitor (when installed): the VM's firewall is guest-enforced (nftables — 8787 from the edge subnet only, 22 from the
  LAN subnet only); the host never opens 8787, and its only firewall change is the one `ufw route allow from <the VM's IP>/32`
  egress line (doctor checks it against the route file's upstream IP). The `/opt/servitor-repo` checkout stays root/admin-owned
  and not group/other-writable anywhere in its directory chain (panel-do's `checkManagedScript` refuses to exec its scripts otherwise).

## Troubleshooting

| symptom | likely cause / fix |
|---|---|
| cert never issues | wrong `NAMESILO_API_KEY`; DNS records missing; propagation — `make logs`, traefik container |
| 401 loop in browser | server clock skew breaks TOTP — `timedatectl`; clear `authelia_session` cookie |
| `/api/*` → 502 | panel-api down: `sudo systemctl status panel-api`, `sudo journalctl -u panel-api -n 50` |
| `/api/*` → 502 only in the browser (host curls fine) | missing UFW 8010 rule for the edge subnet (RUNBOOK → UFW), or the traefik service URL in `traefik/dynamic/panel.yml` drifted from `172.17.0.1:8010` |
| `/api/actions` → 500 | panel-do not installed or sudoers missing → `make panel-do` |
| Services view empty | containers lack the `panel.manage` label |
| GPUs view errors (`panel-do gpu-* failed`) | panel-do too old for the GPU pool actions → `make panel-do`; `make doctor` checks for `gpu-apply` in the registry — also: a `gpu-*` block missing its `gpu.cards` label (radiance 2.x: the label is the pool's only card-need source) errors the whole view → add the label (== the config's `tp`); doctor checks it |
| container action → 500 | panel-do too old for the container actions → `make panel-do`; details in `sudo journalctl -u panel-api -n 50` |
| `up -d` → "network edge declared as external, but could not be found" | the `edge` network was deleted — it's created only by `make deploy` / `make stack`, never by compose (`external: true`). Cause: the edge containers were stopped (or removed) and unused networks pruned — `docker system prune` does both in one run (that's what the panel's *old* `prune-docker` action ran). The current `prune-docker` never touches networks. Fix: `sudo docker network create edge`, then `sudo docker compose --project-directory /opt/panel up -d` (or re-run `make deploy`); `make doctor` checks for this |
| `servitor.pietrasik.top` → 502 | the route's upstream is a raw VM IP: VM down (panel `servitor-status` → `sudo virsh start servitor`), app down (`sudo virsh console servitor`, `systemctl status servitor` inside), or the route's IP drifted from `SERVITOR_VM_IP` (fix the route, `make update`) — doctor's servitor section covers all three |
| panel's `servitor-*` actions missing or 500 | stale panel-do binary → `make panel-do` (doctor fails it explicitly: `registry missing servitor-status`) |
| `servitor-sync` times out in the panel | the action's budget (10 min) IS a stop: at the deadline panel-do kills the script (runCmd's ctx deadline — only in-flight scp/ssh grandchildren may linger); the script is idempotent and resumable, so re-run `servitor-sync`, or check how far it got in `sudo journalctl -u panel-api -n 50`; a run active on the VM makes sync exit 0 fast (update-waiting) instead |
| `servitor-sync`/`-doctor` → "group/other-writable" or "owned by the panel user" (or the update log's servitor tail skipping on the same check) | the `/opt/servitor-repo` checkout (or a directory above it) lost its modes/owner — `chmod go-w` it (or the offending ancestor); never `chown panel` the checkout (RUNBOOK → Servitor, day-2 note) |
| all hosts 404 + traefik container healthy | a dynamic route file failed to parse — an unknown/invalid key makes the file provider send NO config at all. On a hot reload (the common case — the update landing the file on a running traefik) the LAST GOOD config persists (the feature simply doesn't land; everything else routes) and traefik logs "Error occurred during watcher callback"; on a traefik RESTART with the bad file on disk EVERY file-provider route (panel, gpu fleet, portfolio, authelia) 404s with traefik itself healthy — full edge outage. `sudo docker compose --project-directory /opt/panel logs traefik 2>&1 | grep -iE "watcher callback|building configuration"` ("building configuration" only appears on a first load / restart) — fix or remove the offending file at the repo (the single source of truth) and `make update`, or `sudo rm` it from `/opt/panel/traefik/dynamic/` to let the watcher reload |
| update gate: `gpu-* (exited)`, and the update started the WHOLE fleet | a stopped fleet container was pruned (raw `docker container prune` / `docker system prune`), so the update read the missing container as a new service and started every instance at once — invalid when they share cards. The auto-rollback leaves the fleet containers in place, so a re-run of `make update` classifies them as deliberately stopped again (deploys the new configs, pulls the images, starts nothing fleet). With the current guard, a pruned (or brand-new) `restart: "no"` service is never auto-started — the update names it and leaves it down. Bring it back from the panel: Services view → *defined, no container* → *create + start*. Don't raw-prune containers on this box — use the panel's `prune-docker` action (images + build cache only) |
| domain resolves to an old IP | panel-ddns failing → `journalctl -u panel-ddns`; then `sudo systemctl start panel-ddns` (missing/stale records are fixed in place) |
| panel-api won't bind | docker failed to start (`panel-api` does `Wants=docker.service`, so a docker *failure* surfaces here too): `systemctl status docker`, `ip addr show docker0` missing |
