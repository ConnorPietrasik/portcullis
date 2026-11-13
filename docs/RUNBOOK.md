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
touching anything, and auto-rolls-back on any failure — updates are routine and safe by construction:

```bash
make update      # preflight (repo compose must render with repo .env) + doctor precondition —
                 # both abort before touching anything → snapshot everything (image IDs, deployed configs, exact panel-api copy, npm files)
                 # → npm bump (in-repo, within ranges) → sync configs from the repo
                 # → re-pin the server's /etc/hosts (tools/panel-hosts.sh — the route set may have changed)
                 # → pull → rebuild (bases pulled)
                 # → recreate what changed (+ restart on start-time config change) → update panel-api
                 # → gate: post-update services healthy (pre-update set + added) + panel-api serving + full doctor pass
                 # → on any failure: automatic rollback of all of it, re-gated
make rollback              # on demand: undo the last `make update` (same machinery)
make rollback WHAT=radiance  # or: images ONLY of the services WHAT matches — a name (llm-27b), a
                             # prefix (llm), or an image name (radiance); everything else stays
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
| radiance (LLM fleet) | `latest` — deliberately OUTSIDE the declared-line policy (frequent builds wanted) | the tag in `x-llm-base`; a build that breaks behavior → `make rollback WHAT=radiance` |
| searxng | `latest` — deliberately OUTSIDE the declared-line policy (rolling date tags, no releases — see the image-bump duty in **Web search (SearXNG)**) | the tag in `compose/docker-compose.yml`; a build that breaks behavior → `make rollback WHAT=searxng` (images only) |

### If something goes wrong

- A breaking major you didn't want (any line): `make rollback`, then pin it in the repo (e.g. `traefik:v3.7.9`, or the old range back
  in `package.json`/`requirements.txt`), `make update`. (Re-running `make update` without the pin would just retry the same major.)
- A floating image that passed the gates but broke behavior: `make rollback WHAT=radiance` (images only). Then either pin the good
  build's tag in `x-llm-base` + `make update` to hold it, or just `make update` again to re-try `latest`.
- Update interrupted mid-run (Ctrl-C, power loss): the snapshot + rollback tags survive on disk; `make rollback` restores everything,
  or just re-run `make update`.

### How it works (read once)

Mechanics in `setup/08-update.sh` (shared helpers in `setup/common.sh`):

- **Preflight** = `docker compose config -q` renders the repo's compose against the repo's `.env` before
  anything is touched (a var a new block needs but `.env` lacks aborts the run pre-snapshot/pre-sync;
  `make deploy` gates on it too, and `make doctor` checks both trees).
- **Snapshot** = `/opt/panel/.update-snapshot-<ts>` (state in `/opt/panel/.update-state`): the exact *image IDs* of what is running
  (tags move, IDs don't), re-tagged `portcullis-rollback/<svc>:<ts>`; **the deployed config tree** (`/opt/panel`: docker-compose.yml,
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
  if `make doctor` is not green — a pre-existing doctor failure would otherwise auto-revert every update.
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
- After a *successful* update the state file still holds the pre-update snapshot, so one `make rollback` undoes the last update; the
  next `make update` replaces it. Snapshots (rollback tags + dirs, including the ~100 MB panel-api venv copy) are GC'd after 14 days.
- `make deploy` is the **first-boot** path (network, secrets, ACME staging→prod, first build). It does **not** pull — with floating
  tags a pull is an upgrade, so image movement only happens through `make update` (gated). A Day-2 re-run of it applies config
  ungated and warns.

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

## LLM fleet (Radiance instances)

[Radiance](https://codeberg.org/StillDeadcode/radiance) instances on 6 shared R9700s; start/stop and GPU assignment from the panel
(Services view for the containers, **GPUs view** for the cards — see **GPU pool** below).
Machine APIs, not browser apps: no Authelia — the gate is the server's own API key (`Authorization: Bearer`) behind the wildcard cert
+ `llm-rate`; clients use `base_url=https://<name>.pietrasik.top/v1` + key, exactly like OpenAI.

**An instance = a service block + a router pair**, deployed by `make update`: `llm-<name>` in `compose/docker-compose.yml` (merges
`x-llm-base`) ↔ `Host(`<name>.pietrasik.top`)` → `http://llm-<name>:8000` in `traefik/dynamic/llm.yml`. One subdomain per instance so
agents pin the right server; the wildcard CNAME + cert make that zero DNS work. Port is always 8000 (own IP per container). The
display name is the `panel.manage` label — **no commas** in it (`docker ps` encodes labels comma-joined and the Services view parses
them that way); `panel.category=LLM` groups the fleet in the Services view, and copying a block
for a new instance carries both labels with it.

**Cards:** assigned at run time from the panel's **GPUs view** (a pool with auto-grab — see **GPU pool** below).
The block's `ROCR_VISIBLE_DEVICES` is the FALLBACK default a hand-run compose
would use, and the `llm.cards` label (== `--tp`) is how many cards the instance needs. Everything else in
`x-llm-base` is identical for all instances.

**`restart: "no"` on purpose:** after a reboot nothing LLM comes back by itself, EXCEPT the set you mark
**boot** in the GPUs view (the boot-autostart list — applied by `llm-autostart.service`, see **GPU pool** below).
`make update` recreates running instances whose block or image changed and leaves deliberately-stopped ones down (and out of the health gate).
It never *starts* a fleet instance from a missing container: a pruned stopped container (raw `docker container prune`)
is indistinguishable from a new one, and auto-starting the whole fleet at once is invalid when instances share cards —
new instances are deployed + pulled, then left down. Bring one up from the panel: the Services view lists defined
but container-less services under **defined, no container** with a *create + start* button — for llm-*
instances that is the GPU-aware path (saved card set when free, else the lowest free set; see **GPU pool**
below), for everything else it is panel-do `start-service` (same validation/audit as the other actions).
The command-line equivalent is `cd /opt/panel && sudo docker compose -f docker-compose.yml -f
/opt/panel-api/llm-gpu-override.yml up -d <svc>` — keep the GPU override in the render for llm-*
instances, or the revive pins the base-file fallback cards and shows up as drift in the GPU view.
After that the panel manages it as usual.

**Add an instance** (including a second copy of a model — `llm-27b-1` next to `llm-27b`): copy the block
(name, model, `llm.cards`, flags) + the router pair — four name references must all match (service key,
router key, `Host(...)`, service `url`) + an `|| Host(`<name>.pietrasik.top`)` clause in the
`fleet-open` rule so the dashboard link works (Traefik v3's `Host()` takes ONE host — the v2
comma-list form is a parse error it drops silently, doctor flags it); `make doctor` flags any of
the five that don't (`fleet-open` routes to
panel-api, not an instance, so it sits outside the instance-pairing checks — in llm.yml the
`llm-*` prefix marks instance routers and `fleet-*` names shared ones — but its host list
is verified against the compose services). Give the twins distinct display labels
(append the instance name) so the Services view tells them apart. Then `git commit`, `make update`
(deploys + pulls it, leaves it down) and start it — panel GPUs view → *start* (or the Services
view → *defined, no container* → *create + start*, which for llm-* services goes through the same GPU-aware
path). First load ~1 min on this hardware, shown in the panel. The flags in the committed blocks are this
host's own tuning — the blocks are the truth, do not reconcile them against upstream. To trial a NEW model
first: `git clone --depth 1` upstream, run its `deploy/compose/<model>.yaml` ad hoc (one at a time), delete
the clone — its flags belong to the trial, not to a fleet block.

**Key:** one shared `LLM_API_KEY` for all instances (a key authorizes LLM use, not one instance). Rotate = edit `.env` +
`make update` (radiance reads it at startup).

**Dashboard:** radiance serves its dashboard at the instance host root — public HTML, data calls
bearer-gated like `/v1`. The page takes the key from its OWN origin's localStorage only (no URL
param, and a panel page can't write cross-origin), so panel-api serves a one-line bootstrap at
`https://<sub>.pietrasik.top/open` (router `fleet-open` in llm.yml — the ONLY Authelia-gated path on
these hosts; admins-only: panel-api re-checks Remote-User/Remote-Groups). No secret gate by design
— a browser can't hold the panel token — so a direct 172.17.0.1:8010 bind hit (reachable from any
edge container) 401s only while it leaves Remote-* unset: one that CAN spoof them gets the key,
accepted because it already sits on the fleet's own network. The panel's Services view links there
per instance (*dashboard ↗*, shown only while the container is running — down, the bounce 502s):
Authelia pass → key written to that origin's localStorage → bounce to `/`, dashboard live.
The `/v1` routers are untouched — API clients keep the bearer gate. No-JS/first-use fallback:
type the key into the field the dashboard shows in its header after a 401 (persists in
localStorage).

**Logs:** every instance sets `RADIANCE_LOG: warn` in its own `environment` (it can't live in `x-llm-base` — YAML `<<:`
merges top level only, and an instance's `environment` replaces the base's whole map). Per-request INFO lines (token
counts, cache, tok/s, ttft) are the chattiest thing on the box and carry nothing the API's timings/metrics don't; `warn`
keeps errors + ROCm warnings. Raise one instance to `info` (or `debug`, `-v`) when debugging it. json-file rotation caps each
instance at ~30 MB regardless (`x-logging`).

**Updates:** a config-only update recreates nothing. When an update DOES recreate an instance (radiance moved on `latest`, block
edited), its model reload (~1 min) sits inside the 5m healthcheck start_period, and the default 600s budget
(`UPDATE_HEALTH_TIMEOUT`) covers both the update gate and the rollback re-gate — they share it — raise it only for a much slower
model. A new build that is *healthy but behavior-breaking* is different — the gates can't see it: `make rollback WHAT=radiance`
returns just the instances to the pre-update build (re-gated on the same budget), and the next `make update` re-anchors the snapshot
on it.

**Models:** `/models/llm/*.rad` (self-contained; ro-mounted at `/models`). If `/models` is btrfs with compression, keep `.rad` files
uncompressed (`chattr +C`) — direct-I/O n-gram reads degrade otherwise.

**Fallback router (`llm.pietrasik.top`):** one base URL for clients that just want the best model
currently serving — agents, defaults, anything that shouldn't pin an instance. Set the priority
order in the panel's Settings view: a request goes to the FIRST instance in the list that's up
(strictly single-active — the order is priority, traffic is never split). The order holds the
compose SERVICE name (`llm-27b` — not the `panel-llm-27b` container name compose prefixes);
panel-api resolves name → container (the compose service label) → edge IP (`edge-ips`).
Clients use
`base_url=https://llm.pietrasik.top/v1` + the same `LLM_API_KEY` as every instance; `/v1/models`
reflects the active instance only. Served by panel-api itself (router `fleet-fallback` in
`traefik/dynamic/llm.yml` → the `panel-api` service in `panel.yml` → the `172.17.0.1:8010` host
bind — the router omits `auth-chain` like the instance names; the gate is the bearer key).
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

The panel's **GPUs** tab is the control surface: a tile per card (free / held by whom, temp, VRAM,
utilisation) and one row per fleet instance with its **assigned** set (the ledger's intent), its
**holding** set (the live truth — drift between the two means a failed apply or a hand-run compose),
start/stop, an **auto** button (grab the lowest free cards, sized by the instance's `--tp` / `llm.cards`),
a **pick…** row for exact card sets, and a **boot** checkbox.

- **State:** the LEDGER `/opt/panel-api/llm-gpu.json` (panel-owned 0600) holds `allocations`
  (instance → card set) + `autostart` (ordered list). The OVERRIDE `/opt/panel-api/llm-gpu-override.yml`
  (0644) is its compose projection — `ROCR_VISIBLE_DEVICES` per instance — and is included in EVERY
  compose render (`COMPOSE_STACK` in setup/common.sh, panel-do `llm-apply`/`llm-stop`/`start-service`): a `make update`
  recreate (radiance floats on `latest`) therefore keeps the assignment instead of reverting to the
  base-file default. Both files are re-rendered by panel-api from the ledger (never hand-edit), and
  both ride the update snapshot — `make rollback` restores them.
- **Live holds** are read from the running containers' env (panel-do `llm-live`), never from the
  ledger: a stopped instance holds nothing, and `assigned`/`holding` are shown separately.
- **Assigning cards (re)creates the container** — GPU env only exists at create time — so "set cards"
  and "run on them" are one operation (brief downtime + model reload on a running instance). Conflicts
  (a card held by another RUNNING instance, wrong count for `--tp`, out-of-range index) are refused
  with the reason; the instance's own current cards are allowed (a move or a no-op recreate).
- **Count drift is warned, not silently run:** a saved set whose count no longer matches the instance's
  `llm.cards` / `--tp` (e.g. after a flag edit without a re-assign) shows in the panel's warnings — a
  GPU-aware start self-heals by re-grabbing the right number of free cards, but a hand-run `up -d`
  would pin the old count and the engine would fail to load. Reassign (auto) or fix the flag.
- **Starts are GPU-aware everywhere**: the GPUs view's *start*, and the Services view's *start* /
  *create + start* for llm-* services, all go through `POST /api/llm/gpus/start` — the saved set when
  it is free, else the lowest free set (recorded in the ledger). A raw `docker start` is deliberately
  off the panel's path: it would reuse the create-time env and could resurrect cards another instance
  now owns. Stop is plain (the container is kept; cards free on exit; the assignment is remembered).
- **Boot autostart:** `llm-autostart.service` (oneshot, User=panel, after docker; installed + enabled
  by `make api`, no-op when the list is empty) runs `panel-api/llm_autostart.py`, which walks the
  autostart list IN ORDER: an instance already up is left alone; a saved set that is free is honored;
  otherwise the lowest free set is grabbed and recorded (the ledger converges on what actually
  starts). So the boot set is conflict-free by construction — whatever fits comes up; a set that
  doesn't fit is skipped with the reason in `journalctl -u llm-autostart`, and shows down in the
  panel. Overlapping saved sets are warned about in the panel (autostart warnings) but never block
  saving — boot resolves them.
- **Privilege path** (all panel-do, audited in journald like every other action): `llm-fleet`
  (instances + cards needed, read-only), `llm-live` (live containers + holds, read-only — it extracts
  ONLY the ROCR entry from the inspect env; the API key is never logged or forwarded), `llm-apply
  <svc>` (`compose up -d` with the override), `llm-stop <svc>` (`compose stop`). `make doctor` checks
  the registry carries `llm-apply` (stale-binary tripwire) and that the unit is enabled.
- **Multiple instances of one model:** a second `llm-27b` is just another instance (see **Add an instance** above) —
  e.g. `llm-27b` on 0,1 and `llm-27b-1` on 2,3, each with its own subdomain, each assignable
  independently here.

## Web search (SearXNG)

Self-hosted metasearch: an agent-facing keyed JSON API (`search.pietrasik.top`) and a private
browser UI (`searx.pietrasik.top`, the Authelia `*.pietrasik.top` two_factor wildcard — any 2FA
user, zero ACL edits, droppable later — the agent path is byte-identical without it). The API
gate is `panel-api`'s `/gate/search` — a **key checker, not a proxy** (forwardAuth's contract and
the no-proxy rationale are in `search.yml`'s header) — and the key is the fleet's `LLM_API_KEY`,
reused on an owner decision: there is no search-only rotation (rotating it re-creates the `llm-*`
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
  edit + `make update`, `llm-*` re-create expected). It re-keys every search client at the same
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

**pi-web-access** (current latest: 0.47.0 — its SearXNG provider speaks exactly this endpoint:
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

## Troubleshooting

| symptom | likely cause / fix |
|---|---|
| cert never issues | wrong `NAMESILO_API_KEY`; DNS records missing; propagation — `make logs`, traefik container |
| 401 loop in browser | server clock skew breaks TOTP — `timedatectl`; clear `authelia_session` cookie |
| `/api/*` → 502 | panel-api down: `sudo systemctl status panel-api`, `sudo journalctl -u panel-api -n 50` |
| `/api/*` → 502 only in the browser (host curls fine) | missing UFW 8010 rule for the edge subnet (RUNBOOK → UFW), or the traefik service URL in `traefik/dynamic/panel.yml` drifted from `172.17.0.1:8010` |
| `/api/actions` → 500 | panel-do not installed or sudoers missing → `make panel-do` |
| Services view empty | containers lack the `panel.manage` label |
| GPUs view errors (`panel-do llm-* failed`) | panel-do too old for the GPU pool actions → `make panel-do`; `make doctor` checks for `llm-apply` in the registry |
| container action → 500 | panel-do too old for the container actions → `make panel-do`; details in `sudo journalctl -u panel-api -n 50` |
| `up -d` → "network edge declared as external, but could not be found" | the `edge` network was deleted — it's created only by `make deploy` / `make stack`, never by compose (`external: true`). Cause: the edge containers were stopped (or removed) and unused networks pruned — `docker system prune` does both in one run (that's what the panel's *old* `prune-docker` action ran). The current `prune-docker` never touches networks. Fix: `sudo docker network create edge`, then `sudo docker compose --project-directory /opt/panel up -d` (or re-run `make deploy`); `make doctor` checks for this |
| all hosts 404 + traefik container healthy | a dynamic route file failed to parse — an unknown/invalid key makes the file provider send NO config at all. On a hot reload (the common case — the update landing the file on a running traefik) the LAST GOOD config persists (the feature simply doesn't land; everything else routes) and traefik logs "Error occurred during watcher callback"; on a traefik RESTART with the bad file on disk EVERY file-provider route (panel, llm, portfolio, authelia) 404s with traefik itself healthy — full edge outage. `sudo docker compose --project-directory /opt/panel logs traefik 2>&1 | grep -iE "watcher callback|building configuration"` ("building configuration" only appears on a first load / restart) — fix or remove the offending file at the repo (the single source of truth) and `make update`, or `sudo rm` it from `/opt/panel/traefik/dynamic/` to let the watcher reload |
| update gate: `llm-* (exited)`, and the update started the WHOLE fleet | a stopped fleet container was pruned (raw `docker container prune` / `docker system prune`), so the update read the missing container as a new service and started every instance at once — invalid when they share cards. The auto-rollback leaves the fleet containers in place, so a re-run of `make update` classifies them as deliberately stopped again (deploys the new configs, pulls the images, starts nothing fleet). With the current guard, a pruned (or brand-new) `restart: "no"` service is never auto-started — the update names it and leaves it down. Bring it back from the panel: Services view → *defined, no container* → *create + start*. Don't raw-prune containers on this box — use the panel's `prune-docker` action (images + build cache only) |
| domain resolves to an old IP | panel-ddns failing → `journalctl -u panel-ddns`; then `sudo systemctl start panel-ddns` (missing/stale records are fixed in place) |
| panel-api won't bind | docker failed to start (`panel-api` does `Wants=docker.service`, so a docker *failure* surfaces here too): `systemctl status docker`, `ip addr show docker0` missing |
