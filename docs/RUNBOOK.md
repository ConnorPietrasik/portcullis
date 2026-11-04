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
elsewhere).

Create `compose/authelia/users_database.yml` from the template — the real file is gitignored, so password hashes never reach GitHub (a fresh
clone won't have it):

```bash
cp compose/authelia/users_database.yml.example compose/authelia/users_database.yml
```

Then for each user generate a hash

```bash
docker run --rm authelia/authelia:4.39 authelia crypto hash generate argon2 --password 'YOUR-PASSWORD'
```

and paste it over `REPLACE_WITH_ARGON2_HASH`. Groups: `admins` = you, `friends` = shared-app users.

## Phase 2 — Edge stack

```bash
make deploy         # → /opt/panel, docker compose up -d --build
make ddns           # installs panel-ddns (needs /opt/panel/.env) — apex A self-creates
make logs           # watch: traefik acme lines = cert issuance (NameSilo TTLs are slow — minutes, not seconds)
sudo docker compose --project-directory /opt/panel logs authelia | grep -i error
```

**UFW (required step — do it now)** — configure it before `panel-api` exists: `make deploy` just created the `edge` network the 8010 rule is
scoped to, and enabling UFW now — before `panel-api` binds 8010 in Phase 4 — means 8010 is never reachable from outside the edge subnet,
even briefly. Commands at [UFW](#ufw-required); note the 401-JSON verify there only passes once `panel-api` is up (Phase 4). `make doctor`
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
of resolver caching. Reuses `NAMESILO_API_KEY` — the same key Traefik uses for DNS-01.

```bash
journalctl -u panel-ddns -n 50                  # what it did and when
sudo systemctl start panel-ddns.service         # reconcile now — also the manual run (the script
                                                #   FATALs if run directly: the API key only comes
                                                #   from the unit's EnvironmentFile)
dig +short pietrasik.top @ns1.dnsowl.com        # what the authoritative NS says (check NS at NameSilo)
```

## Updates

Images are **pinned at the minor line, patches float** (`traefik:v3.7`, `authelia:4.39`, `nginx:1.31`): patch releases are security/bugfixes
only, and `make deploy` pulls them — so a routine deploy keeps the edge stack current within the line. Minor bumps are deliberate: edit the
tag in `compose/docker-compose.yml` (one minor at a time), skim the release notes for deprecations (especially Authelia's config changes),
then `make deploy`. `docker compose ps` + `make doctor` afterwards.

## Add an app

Two edits in this repo, then deploy. Traefik is file-provider only — it never reads docker labels, so every app needs a route in
`traefik/dynamic/`:

```yaml
# compose/docker-compose.yml — the service, on the edge network
  jellyfin:
    image: jellyfin/jellyfin
    restart: unless-stopped
    networks: [edge]
    labels:
      - panel.manage=Jellyfin        # shows up in the panel's Services view (optional)
```

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

`make deploy` (syncs configs + `docker compose up -d`). Default policy: any authenticated user. Add an Authelia rule in
`compose/authelia/configuration.yml` **above the wildcard** only if it needs group restriction or a public bypass —
`make deploy` restarts authelia whenever `configuration.yml` or `users_database.yml` content changes (both are read
only at container start, no hot reload), so no manual recreate step is needed.

## Add an LLM server (OpenAI-style API)

LLM endpoints are machine APIs, not browser apps, so they deliberately skip Authelia: forward-auth is cookie-based and API clients can't log
in with it. The gate is llama-server's own `--api-key` (standard `Authorization: Bearer`), TLS comes from the wildcard cert at Traefik, and
`llm-rate` blunts key-guessing. Clients get `base_url=https://llm.pietrasik.top/v1` + `api_key=sk-…` and behave exactly like OpenAI.

1. Build/pull the image on the server; models somewhere host-visible.
2. Uncomment + adapt the `llm-glm` block in `compose/docker-compose.yml`: mirror the `--device`/`--group-add`/`user` flags from the
   `docker run` you use today; expand `~` to an absolute path (compose runs no shell); the shared `LLM_API_KEY` from `.env`
   (`openssl rand -hex 32` — the block fails fast at deploy if the key is unset). The key is visible in `docker inspect` — same exposure
   class as every secret on this host; use `--api-key-file` + a mounted file if that ever bothers you.

   Key strategy: **one shared key across all LLM servers** — access policy is uniform per person (a key authorizes LLM use, not a specific
   server), so per-server keys would only add client-config churn. Keys live on your devices only; a leak means device compromise, where
   every key burns equally. llama-server reads keys at startup, so a rotate = edit `.env` + restart each server — batch the change in one
   edit, then cycle them (model reload is slow). If access ever becomes per-person: repeat `--api-key` (or `--api-key-file`, one key per
   line) and give each person their own value — revocation is still a restart. Only if you need hot revocation, per-person quotas, or usage
   attribution, put a real LLM gateway (e.g. LiteLLM) on its own subdomain in front — same bearer-auth pattern, and don't hand-roll the
   middle layer.
3. Clone the router+service pair in `compose/traefik/dynamic/llm.yml` (new subdomain, new container port). Traefik hot-reloads the file
   provider — no restart.
4. `make deploy`. The Services view lists it (`panel.manage` label); stop/start/restart work via panel-do — restart = full model reload,
   slow at 300k-context sizes.

The web UI at `https://llm.pietrasik.top/` sits behind the same API key. If you later want Authelia 2FA for humans on top, split the router:
keep `auth-chain@file` on `Host(…)` and add a higher-priority `Host(…) && PathPrefix(`/v1`)` router with `sec-headers@file` only — API
clients keep bearer auth, browsers get 2FA.

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

- [ ] The clone stays the source of truth — edit tracked files in the repo (commit before `make deploy` for a rollback point),
  never hand-edit the deployed copies: deploys sync repo → `/opt/panel` one-way (stale managed traefik configs get pruned).
  `.gitignore` already keeps `.env`, built binaries, and caches out of git.
- [ ] Persistent journald — panel-do's audit trail lives there: `sudo mkdir -p /var/log/journal && sudo systemctl restart systemd-journald`
  (skip if `/var/log/journal` already exists).
- [ ] SSD trim: `sudo systemctl enable --now fstrim.timer` (weekly; HDDs don't need it).
- [ ] UFW active and configured per [UFW](#ufw-required) below — **required on this host** (no firewalld/iptables fallback); only 80/443
  public; `172.17.0.1:8010` never reachable externally.
- [ ] No container mounts `/var/run/docker.sock` — not even `:ro` (a ro-mounted socket still grants full Docker API access). Traefik runs
  the file provider precisely so it needs none.
- [ ] `chmod 600 /opt/panel/.env /opt/panel/.panel-api.env /opt/panel/traefik/dynamic/middlewares.yml` (deploy script does this — verify after manual edits;
  `.panel-api.env` is derived from `.env` — regenerate with `make api`, never hand-edit).
- [ ] CrowdSec + Traefik bouncer plugin (optional, later).
- [ ] SSH keys-only as break-glass access.
- [ ] Backups: `/opt/panel/.env`, `compose/` (this repo), Authelia `/opt/panel/authelia/` (config + `db.sqlite3` — TOTP seeds!), the `acme`
  docker volume, and this git repo.

## UFW (required)

This host always runs UFW — it is a mandatory deploy step (Phase 2), not an option; `make doctor` fails while it is inactive or the 8010
rule is missing.

```bash
sudo ufw allow 80,443/tcp comment 'traefik web'   # TCP only — 443/udp is HTTP/3, not served here
# 8010 is scoped to the edge network's ACTUAL subnet (source) — Docker assigns
# it from a pool that depends on which subnets are already taken (a fresh
# daemon starts at 172.18.0.0/16); derive, don't guess. The destination is
# pinned to panel-api's bind IP (keep in sync with panel-api.service):
EDGE_SUBNET=$(sudo docker network inspect edge -f '{{(index .IPAM.Config 0).Subnet}}')
sudo ufw allow from "$EDGE_SUBNET" to 172.17.0.1 port 8010 proto tcp comment 'panel-api from edge network'
sudo ufw enable && sudo ufw status verbose
```

Two gotchas:

- **Docker-published ports bypass UFW.** 80/443 are open via Docker's NAT rules regardless of ufw; the real access control for containers is
  what you put in `ports:`. Nothing in this stack publishes a port besides Traefik — keep it that way, and never publish the API beyond
  loopback/bridge IPs.
- **The 8010 rule is required**, not optional: container → host traffic hits UFW's INPUT chain, and `default deny incoming` turns the panel
  into 502s. Scoped to the edge network's own subnet and to 172.17.0.1, the internet can't touch it.

Verify after enabling (want: 401 JSON = reachable, not a timeout; then refused from outside):

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
