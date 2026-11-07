# Architecture

## Components & trust boundaries

```
                    internet
                       │  (only :80/:443 open)
                ┌──────▼──────┐
                │   traefik   │  wildcard cert via NameSilo DNS-01
                │  file provider ONLY — mounts no docker.sock
                └──┬───┬───┬──┘
     sec-headers + │   │   │  + Authelia forward-auth (admins only) + X-Panel-Token
                   │   │   │
   ┌───────────────┘   │   └──────────────────┐
   │                   │                      │
┌──▼───────┐    ┌──────▼──────┐        ┌──────▼───────────┐
│panel-web │    │  authelia   │        │ host API :8010   │ user 'panel'
│(react)   │    │ (2FA,groups)│        │ FastAPI, systemd │ binds 172.17.0.1
└──────────┘    └─────────────┘        └────────┬─────────┘
                                                │
                                     ┌──────────▼───────────┐
                                     │  sudo -n panel-do    │ root, whitelist
                                     │  /usr/local/libexec  │ the only privileged
                                     └──────────────────────┘ path — containers
                                       included (list/start/stop/restart)
```

## Decisions & rationale

**Panel UI in a container, API on the host.** The UI is static files — a perfect container citizen, routed by Traefik like everything
else. The API must run on the host: it reads GPU sysfs and invokes `sudo -n panel-do`. It binds `172.17.0.1` (the docker0 gateway):
edge containers reach it by addressing that IP directly (the host is their default gateway and owns the IP; `host.docker.internal`
would resolve to the edge network's own gateway instead, where nothing listens) — but the internet can't.

**Two independent gates in front of the API.** Traefik injects `X-Panel-Token` (from `/opt/panel/.env`) — clients can't set it,
Traefik overwrites it. Authelia forward-auth strips client-supplied `Remote-*` headers and replaces them with its verdict. The API
re-checks both. Forging either requires already controlling Traefik or the host.

**One privileged path: panel-do handles containers too.** There is exactly one way for the panel to touch docker or systemd:
`sudo -n panel-do`. Container listing/control are registry actions (`list-containers`, `start/stop/restart-container` with anchored
12-hex-ID regexes) alongside `restart-service` and `prune-docker` — same central arg validation, same journald audit, same timeouts,
and the UI renders everything from `--list`. An earlier loopback docker socket-proxy is gone: its `POST`/`ALLOW_*` whitelist had no
arg validation, no confirm gating, no audit. The registry is ~250 lines of Go; panel-api caches `list-containers` ~3s (the UI polls
~2s) and invalidates after any action.

**Traefik runs the file provider only — no docker.sock, not even `:ro`.** A read-only socket mount does not make the Docker API
read-only: `connect()` on a unix socket is not a filesystem write, so a `:ro`-mounted socket still grants full control (privileged
containers, exec, host bind-mounts → root equivalent). Traefik is the only internet-facing process, so it gets zero docker access.
All routers/services live in reviewed files under `traefik/dynamic/`, versioned in this repo; adding an app = edit a file +
`make update`. The file provider watches that directory, so route changes apply without restarting Traefik.

**Dynamic IP, self-healing DNS.** The public IP isn't static, and a stale A record takes down everything at once — including the
panel you'd fix it with. `panel-ddns` is a stdlib-python systemd timer (user `panel`, no root, outbound only): every 10 minutes it
detects the public IPv4 (OpenDNS resolver first, then HTTPS fallbacks) and reconciles — updating stale A records, creating missing
ones; CNAMEs at a configured host are respected and skipped (the wildcard `*` is a manual CNAME → apex, so it follows for free and
must never get its own A). Deliberately stateless with no short-circuit: a "nothing changed, skip" check can silently trust a stale
assumption, for ~6 tiny API calls/hour saved. Reuses the API key Traefik already needs for DNS-01 (nothing new to leak; never
logged; it arrives via a reduced env file, so the rest of the secret set never enters the oneshot's environ). TTL is pinned at
NameSilo's 3600 minimum, so worst-case cutover after an IP change is ~10 min of detection + 1 h of resolver caching.

**Privileged actions through a compiled dispatcher.** `panel-do` is a small Go binary: hardcoded action registry, central arg
validation (arity, printable ASCII, no leading `-`, no path separators, per-action anchored regexes), no shell, scrubbed env,
per-action timeout, root-owned + `0755`, audit logs to journald. The sudoers rule grants `panel` exactly one command. The UI renders
whatever `panel-do --list` reports, so the registry can never drift from what the binary enforces. The binary self-checks at every
startup — it refuses to run unless it is at the pinned path, root-owned, and not group/other-writable, so a swapped or loosened
binary fails at runtime, not just at install time.

**Authelia groups.** `admins` (you) → panel; `friends` → e.g. Jellyfin; public demos bypass Authelia entirely and do their own auth;
everything else on any subdomain requires a logged-in user. Rule order matters (first match wins) and the bare-domain `bypass` sits
**last** so new subdomains are private by default.

**AMD GPUs without ROCm.** Everything glanceable lives in sysfs: `/sys/class/drm/card*/device` (busy %, VRAM) and its `hwmon`
(edge/junction/mem temps, power, fan). No `amd-smi` needed; revisit only for per-process VRAM or fan control.

**Secrets.** One `.env` (chmod 600) is the single source of truth: it feeds compose, and deploy derives the reduced env files the
panel-uid systemd units load — `/opt/panel/.panel-api.env` (0600, only the `PANEL_*` vars the API reads) and
`/opt/panel/.panel-ddns.env` (0600, only `NAMESILO_API_KEY` + optional `DDNS_HOSTS`) — because a panel-uid process can read its own
`/proc/<pid>/environ`, so the panel-uid services (long-lived panel-api, and the same-uid panel-ddns oneshot on each tick) must not
hold the rest of the secret set (Authelia, LLM) in their environment. Deploy-time placeholders (`__PANEL_TOKEN__`, `__ACME_EMAIL__`)
are substituted into Traefik's dynamic config by `setup/02-deploy-stack.sh`; the repo copy stays clean.
