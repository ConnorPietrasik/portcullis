# panel-do — handoff for the implementing model

`main.go` is a security framework with a fenced-off ACTIONS section. The
security-critical code is already written; the implementing model may only edit
between the `ACTIONS` markers. The framework mechanically prevents the classic
failure modes (shells, path args, flag injection, passthrough) — it cannot
write insecure actions without also fighting the validator.

## What enforces safety (already implemented)

- **Central validation before any action runs**: exact arity, printable-ASCII-only,
  length cap, no leading `-` (flag injection), no `/` or `\` (path injection),
  then the action's own anchored regex per slot. Implementers never write arg validation.
- **No shell exists in the binary.** The only exec primitive is
  `runCmd(ctx, "/abs/path", args...)` with a scrubbed environment.
- **Unknown action → hard fail.** No passthrough, no `exec anything`.
- **Self-check at startup**: refuses to run if the binary or its directory is
  non-root-owned or group/other-writable, or if invoked from a different path.
- **Per-action timeout**, structured audit logging (including `SUDO_USER`) to stderr → journald.
- `--list` emits the registry as JSON, so the API's `/api/actions` calls it via
  sudo and **the registry can never drift** from what the UI shows.

## Handoff prompt (paste to the implementing model)

> In `main.go`, implement the following actions, **editing only between the
> `ACTIONS` markers**: [list your actions, e.g. `trim-filesystem` (no args, runs
> `/usr/sbin/fstrim -av`), `restart-timer <name.timer>` (uses unitNameRe, runs
> `/usr/bin/systemctl restart -- <name.timer>`)]. Obey the rules in the ACTIONS comment block
> exactly: only `runCmd` with absolute paths, args are opaque tokens, no new
> imports beyond the existing ones, each action under 30 lines, return errors.
> Do not modify the FRAMEWORK section, the registry patterns except to add your
> own anchored regexes, or any file besides `main.go`. When done run
> `gofmt -w main.go` and `go vet ./...` and report the output.

## Review checklist (verify before deploying)

1. `git diff` shows **zero changes outside the ACTIONS section**.
2. Every `Run` calls only `runCmd`; no direct `exec.Command`, no
   `os.Open`/`os.Create`/`os.Getenv`, no goroutines, no network imports.
3. All regexes anchored (`^...$`), arity exact, no optional/variadic args.
4. No action treats an arg as a path or passes an arg before a hardcoded `--`
   where relevant.
5. Timeouts sane per action; destructive ones flagged `Confirm: true`.
6. `go vet` clean; binary installs to the pinned path and self-check passes;
   the negative tests below exit 64/65.

## Build, install, test

```bash
make panel-do        # builds, gofmt/go vet checks, installs, sudoers, runs tests
```

Manual equivalent:

```bash
cd panel-do && go vet ./... && gofmt -l .   # must be clean
go build -trimpath -ldflags="-s -w" -o panel-do .
sudo install -o root -g root -m 0755 panel-do /usr/local/libexec/panel-do
sudo chattr +i /usr/local/libexec/panel-do
```

sudoers (`/etc/sudoers.d/panel`, installed by the script):

```
Defaults:panel !requiretty
panel ALL=(root) NOPASSWD: /usr/local/libexec/panel-do
```

Negative tests (must all hold):

```bash
sudo -u panel sudo -n /usr/local/libexec/panel-do --list                          # valid JSON, exit 0
sudo -u panel sudo -n /usr/local/libexec/panel-do nope;              echo $?      # → 64
sudo -u panel sudo -n /usr/local/libexec/panel-do restart-service '../../etc'; echo $?  # → 65
sudo -u panel sudo -n /usr/local/libexec/panel-do restart-service -foo; echo $?         # → 65
sudo -u panel sudo -n /usr/local/libexec/panel-do start-container zzz; echo $?          # → 65 (container actions live in the registry too)
```

## Adding a capability later

One Go function in the ACTIONS section + one registry entry → `make panel-do`.
The API picks it up automatically via `--list` (cached ~60s); no API change.
