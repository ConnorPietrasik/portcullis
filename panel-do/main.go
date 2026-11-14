// panel-do: non-resident privileged action dispatcher.
// Invoked ONLY via: sudo -n /usr/local/libexec/panel-do <action> [args...]
//
// INVARIANTS — do not weaken any of these:
//   - Actions exist ONLY in the REGISTRY in the ACTIONS section.
//   - Unknown action => exit 64. No passthrough, ever.
//   - Args validated centrally (arity + patterns) BEFORE Run is called.
//   - No shell anywhere; exec only via runCmd with absolute paths.
//   - Short-lived process: no listeners, no goroutines, no network.

package main

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"os/user"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"syscall"
	"time"
)

const expectedPath = "/usr/local/libexec/panel-do"

// ============================================================
// FRAMEWORK — DO NOT MODIFY ANYTHING IN THIS SECTION
// ============================================================

type RunFunc func(ctx context.Context, args []string, logf func(string, ...any)) error

type Action struct {
	Summary string
	Args    []*regexp.Regexp // one pattern per required arg; exact arity enforced
	Timeout time.Duration
	Confirm bool // surfaced to the UI ("are you sure" dialogs)
	Run     RunFunc
}

var argBaseRe = regexp.MustCompile(`^[ -~]{1,128}$`) // printable ASCII, <=128 chars

func validateArgs(a Action, args []string) error {
	if len(args) != len(a.Args) {
		return fmt.Errorf("expected exactly %d arg(s), got %d", len(a.Args), len(args))
	}
	for i, arg := range args {
		switch {
		case !argBaseRe.MatchString(arg):
			return fmt.Errorf("arg %d: must be 1-128 printable ASCII chars", i)
		case strings.HasPrefix(arg, "-"):
			return fmt.Errorf("arg %d: may not start with '-' (flag injection)", i)
		case strings.ContainsAny(arg, `/\`):
			return fmt.Errorf("arg %d: path separators not allowed", i)
		}
		if a.Args[i] != nil && !a.Args[i].MatchString(arg) {
			return fmt.Errorf("arg %d: failed action validation", i)
		}
	}
	return nil
}

// runCmd is the ONLY exec primitive. No shell, scrubbed env, ctx deadline kills.
// (Kills the direct child; grandchildren may linger — keep actions simple.)
func runCmd(ctx context.Context, path string, args ...string) (string, error) {
	if !filepath.IsAbs(path) {
		return "", fmt.Errorf("refusing non-absolute path %q", path)
	}
	cmd := exec.CommandContext(ctx, path, args...)
	cmd.Env = []string{"PATH=/usr/bin:/usr/sbin:/bin:/sbin", "LC_ALL=C"}
	out, err := cmd.CombinedOutput()
	if err != nil && errors.Is(ctx.Err(), context.DeadlineExceeded) {
		return string(out), fmt.Errorf("timed out")
	}
	return string(out), err
}

func logf(format string, a ...any) {
	fmt.Fprintf(os.Stderr, time.Now().UTC().Format(time.RFC3339)+" panel-do: "+format+"\n", a...)
}

func fail(code int, format string, a ...any) {
	logf("FATAL: "+format, a...)
	os.Exit(code)
}

func selfCheck() {
	if exe, err := os.Executable(); err != nil || exe != expectedPath {
		fail(70, "self-check: must run from %s (got %q, err=%v)", expectedPath, exe, err)
	}
	check := func(p string) {
		st, err := os.Stat(p)
		if err != nil {
			fail(70, "self-check: stat %s: %v", p, err)
		}
		if st.Mode()&0o022 != 0 {
			fail(70, "self-check: %s is group/other-writable", p)
		}
		if sys, ok := st.Sys().(*syscall.Stat_t); !ok || sys.Uid != 0 {
			fail(70, "self-check: %s not owned by root", p)
		}
	}
	check(expectedPath)
	check(filepath.Dir(expectedPath))
}

func sortedNames() []string {
	names := make([]string, 0, len(registry))
	for n := range registry {
		names = append(names, n)
	}
	sort.Strings(names)
	return names
}

func usageExit(code int) {
	fmt.Fprintf(os.Stderr, "usage: panel-do <action> [args...]\nactions:\n")
	for _, n := range sortedNames() {
		fmt.Fprintf(os.Stderr, "  %-20s %s\n", n, registry[n].Summary)
	}
	os.Exit(code)
}

type actionInfo struct {
	Name       string   `json:"name"`
	Summary    string   `json:"summary"`
	Arity      int      `json:"arity"`
	Patterns   []string `json:"patterns"`
	TimeoutSec int      `json:"timeout_sec"`
	Confirm    bool     `json:"confirm"`
}

func printRegistryJSON() {
	var out []actionInfo
	for _, n := range sortedNames() {
		a := registry[n]
		pats := []string{}
		for _, p := range a.Args {
			pats = append(pats, p.String())
		}
		out = append(out, actionInfo{n, a.Summary, len(a.Args), pats, int(a.Timeout.Seconds()), a.Confirm})
	}
	json.NewEncoder(os.Stdout).Encode(out) //nolint:errcheck — stdout only
}

func main() {
	selfCheck()
	logf("invoked uid=%d sudo_user=%q argv=%v", os.Getuid(), os.Getenv("SUDO_USER"), os.Args[1:])
	if len(os.Args) < 2 {
		usageExit(64)
	}
	if os.Args[1] == "--list" { // read-only metadata; safe to expose via sudo
		printRegistryJSON()
		return
	}
	action, ok := registry[os.Args[1]]
	if !ok {
		fail(64, "unknown action %q", os.Args[1])
	}
	args := os.Args[2:]
	if err := validateArgs(action, args); err != nil {
		fail(65, "invalid args: %v", err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), action.Timeout)
	defer cancel()
	start := time.Now()
	if err := action.Run(ctx, args, logf); err != nil {
		// skipResult (defined in the ACTIONS section) = an EXPECTED decline
		// (dirty clone, an update already running): exit 75 (EX_TEMPFAIL),
		// not a fault — panel-autoupdate.service declares SuccessExitStatus
		// for it, so a skipped tick leaves no failed unit behind; the reason
		// is the SKIPPED line below (this unit's journal).
		var skipped skipResult
		if errors.As(err, &skipped) {
			logf("action=%s SKIPPED dur=%s — %v", os.Args[1], time.Since(start), err)
			os.Exit(75)
		}
		logf("action=%s FAILED dur=%s err=%v", os.Args[1], time.Since(start), err)
		fail(1, "%v", err)
	}
	logf("action=%s ok dur=%s", os.Args[1], time.Since(start))
}

// ============================================================
// ACTIONS — THE ONLY SECTION AN IMPLEMENTER MAY EDIT.
//
// Rules for every action (violations = security bug):
//   1. Exec ONLY via runCmd(ctx, "/absolute/path", ...). Never bash/sh, never
//      direct exec.Command, never string-built command lines.
//   2. Args arrive pre-validated as OPAQUE TOKENS (no '/', no '-', no '..').
//      Never treat an arg as a file path.
//   3. No os.Getenv, no network, no goroutines, no os.Exit, no panic.
//      Return errors.
//   4. Separators like "--" belong hardcoded inside runCmd calls, never as args.
//   5. An EXPECTED decline (dirty clone, an update already running) returns
//      skipResult, not a plain error: main() maps it to exit 75 (EX_TEMPFAIL)
//      so callers (the timer's SuccessExitStatus, the dashboard) can tell
//      "nothing ran, by design" apart from a fault.
// ============================================================

// skipResult — an expected, transient decline (see rule 5). Still an error:
// actions return it, they never call os.Exit.
type skipResult struct{ reason string }

func (e skipResult) Error() string { return "skipped: " + e.reason }

var unitNameRe = regexp.MustCompile(`^[a-zA-Z0-9][a-zA-Z0-9._@-]*\.service$`)
var containerIDRe = regexp.MustCompile(`^[0-9a-f]{12}$`)
var serviceNameRe = regexp.MustCompile(`^[a-zA-Z0-9][a-zA-Z0-9._-]*$`)

// llmServiceRe — an LLM fleet instance's compose service name (llm-27b,
// llm-27b-1, …). Mirrors panel-api's LLM_NAME_RE, so the two sides can
// never disagree about what a fleet instance is called.
var llmServiceRe = regexp.MustCompile(`^llm-[a-z0-9][a-z0-9-]{0,62}$`)

// llmFleet lists the deployed stack's LLM fleet instances (services named
// llm-*) and the number of cards each one needs — the GPU pool's static
// half. cards comes from the llm.cards label (explicit, keep == --tp),
// falling back to the --tp flag in the instance's command (tensor
// parallelism = card count), then 1. JSON array on stdout; read-only, no args.
func llmFleet(ctx context.Context, args []string, logf func(string, ...any)) error {
	out, err := runCmd(ctx, "/usr/bin/docker", "compose", "--project-directory", "/opt/panel",
		"-f", "/opt/panel/docker-compose.yml", "config", "--format", "json")
	if err != nil {
		return fmt.Errorf("compose config: %v: %s", err, out)
	}
	var cfg struct {
		Services map[string]struct {
			// command is emitted as a string OR a token list depending on the
			// compose version — accept both (same drift-tolerance as labels
			// in composeLabel). Raw here; tokenized in tensorParallel.
			Command json.RawMessage `json:"command"`
			Labels  json.RawMessage `json:"labels"`
		} `json:"services"`
	}
	if err := json.Unmarshal([]byte(out), &cfg); err != nil {
		return fmt.Errorf("compose config: bad JSON: %v", err)
	}
	type inst struct {
		Name    string `json:"name"`
		Display string `json:"display"`
		Cards   int    `json:"cards"`
	}
	res := []inst{}
	for name, s := range cfg.Services {
		if !llmServiceRe.MatchString(name) {
			continue
		}
		display := composeLabel(s.Labels, "panel.manage")
		if display == "" || display == "true" {
			display = name
		}
		cards := 0
		if v := composeLabel(s.Labels, "llm.cards"); v != "" {
			if n, err := strconv.Atoi(v); err == nil && n > 0 {
				cards = n
			}
		}
		if cards == 0 {
			cards = tensorParallel(commandTokens(s.Command))
		}
		res = append(res, inst{name, display, cards})
	}
	sort.Slice(res, func(i, j int) bool { return res[i].Name < res[j].Name })
	enc, err := json.Marshal(res)
	if err != nil {
		return fmt.Errorf("llm-fleet: marshal: %v", err)
	}
	fmt.Println(string(enc))
	return nil
}

// commandTokens turns a compose `command` (string or token-list, per version)
// into a token slice; nil when absent.
func commandTokens(raw json.RawMessage) []string {
	if len(raw) == 0 || string(raw) == "null" {
		return nil
	}
	var tokens []string
	if err := json.Unmarshal(raw, &tokens); err == nil {
		return tokens
	}
	var s string
	if err := json.Unmarshal(raw, &s); err == nil {
		return strings.Fields(s)
	}
	return nil
}

// tensorParallel reads the --tp value out of a compose command (a token list); 1
// when the flag is absent (a single card).
func tensorParallel(cmd []string) int {
	for i := 0; i+1 < len(cmd); i++ {
		if cmd[i] == "--tp" {
			if n, err := strconv.Atoi(cmd[i+1]); err == nil && n > 0 {
				return n
			}
			return 1
		}
	}
	return 1
}

// llmLive reports the LIVE GPU holds of the fleet: for every llm-* container
// (any state) the compose service, docker state, and the cards its
// ROCR_VISIBLE_DEVICES pins it to (null when unset). The pool's held column
// and every conflict check derive from this + the container state — a
// stopped instance holds nothing. Read-only, no args.
// NOTE: docker inspect's Config.Env also carries RADIANCE_API_KEY — never
// log or forward the env list; extract ONLY the ROCR entry.
func llmLive(ctx context.Context, args []string, logf func(string, ...any)) error {
	// KEY-ONLY label filter: the engine matches label VALUES by exact string
	// equality (regex is for name/id/status only — moby's MatchKVList), so a
	// pattern like ^llm- matches nothing and the pool sees an empty world
	// (conflict detection silently off). Key-only matches every compose
	// container; the llmServiceRe check after inspect keeps just the fleet.
	out, err := runCmd(ctx, "/usr/bin/docker", "ps", "-a",
		"--filter", "label=com.docker.compose.service", "--format", "{{.ID}}")
	if err != nil {
		return fmt.Errorf("docker ps: %v: %s", err, out)
	}
	ids := strings.Fields(strings.TrimSpace(out))
	if len(ids) == 0 {
		fmt.Println("[]")
		return nil
	}
	// ids come from docker itself (12-hex), not the caller — safe tokens.
	inspect := append([]string{"inspect"}, ids...)
	out, err = runCmd(ctx, "/usr/bin/docker", inspect...)
	if err != nil {
		return fmt.Errorf("docker inspect: %v: %s", err, out)
	}
	var data []struct {
		Name  string `json:"Name"`
		State struct {
			Status string `json:"Status"`
		} `json:"State"`
		Config struct {
			Env    []string          `json:"Env"`
			Labels map[string]string `json:"Labels"`
		} `json:"Config"`
	}
	if err := json.Unmarshal([]byte(out), &data); err != nil {
		return fmt.Errorf("docker inspect: bad JSON: %v", err)
	}
	type inst struct {
		Service   string `json:"service"`
		Container string `json:"container"`
		State     string `json:"state"`
		Gpus      []int  `json:"gpus"`
	}
	res := []inst{}
	for _, c := range data {
		svc := c.Config.Labels["com.docker.compose.service"]
		if !llmServiceRe.MatchString(svc) {
			continue
		}
		var gpus []int
		for _, kv := range c.Config.Env {
			if k, v, ok := strings.Cut(kv, "="); ok && k == "ROCR_VISIBLE_DEVICES" {
				for _, part := range strings.Split(v, ",") {
					if n, err := strconv.Atoi(strings.TrimSpace(part)); err == nil {
						gpus = append(gpus, n)
					}
				}
			}
		}
		res = append(res, inst{svc, strings.TrimPrefix(c.Name, "/"), c.State.Status, gpus})
	}
	sort.Slice(res, func(i, j int) bool { return res[i].Container < res[j].Container })
	enc, err := json.Marshal(res)
	if err != nil {
		return fmt.Errorf("llm-live: marshal: %v", err)
	}
	fmt.Println(string(enc))
	return nil
}

// llmApply (re)creates + starts one fleet instance on the CURRENT GPU
// override: panel-api has already written /opt/panel-api/llm-gpu-override.yml
// (the ledger's set for this service) — this action only converges the
// container onto it. `up -d` creates a missing container, recreates one
// whose env drifted (the GPU change), and is a no-op when already converged.
func llmApply(ctx context.Context, args []string, logf func(string, ...any)) error {
	out, err := runCmd(ctx, "/usr/bin/docker", "compose", "--project-directory", "/opt/panel",
		"-f", "/opt/panel/docker-compose.yml", "-f", "/opt/panel-api/llm-gpu-override.yml",
		"up", "-d", "--", args[0])
	if err != nil {
		return fmt.Errorf("up -d %s: %v: %s", args[0], err, out)
	}
	logf("fleet %s: up -d with gpu override ok", args[0])
	return nil
}

// llmStop stops one fleet instance's container (compose stop: the container
// is KEPT — the panel starts it again — and its cards are released the
// moment it exits).
func llmStop(ctx context.Context, args []string, logf func(string, ...any)) error {
	out, err := runCmd(ctx, "/usr/bin/docker", "compose", "--project-directory", "/opt/panel",
		"-f", "/opt/panel/docker-compose.yml", "-f", "/opt/panel-api/llm-gpu-override.yml",
		"stop", "--", args[0])
	if err != nil {
		return fmt.Errorf("stop %s: %v: %s", args[0], err, out)
	}
	logf("fleet %s: stopped", args[0])
	return nil
}

func restartService(ctx context.Context, args []string, logf func(string, ...any)) error {
	if out, err := runCmd(ctx, "/usr/bin/systemctl", "restart", "--", args[0]); err != nil {
		return fmt.Errorf("restart %s: %v: %s", args[0], err, out)
	}
	// A oneshot unit is *inactive* after a clean run (unless RemainAfterExit),
	// so is-active would report success as a failure. start/restart blocks
	// until a oneshot completes, so its Result is final here.
	unitType, err := runCmd(ctx, "/usr/bin/systemctl", "show", "-p", "Type", "--value", "--", args[0])
	if err != nil {
		return fmt.Errorf("restart %s: cannot read unit Type: %v: %s", args[0], err, unitType)
	}
	if strings.TrimSpace(unitType) == "oneshot" {
		out, err := runCmd(ctx, "/usr/bin/systemctl", "show", "-p", "Result", "--value", "--", args[0])
		if err != nil {
			return fmt.Errorf("restart %s: cannot read unit Result: %v: %s", args[0], err, out)
		}
		result := strings.TrimSpace(out)
		logf("unit %s (oneshot) result: %s", args[0], result)
		if result != "success" {
			return fmt.Errorf("restart %s: oneshot run finished with Result=%s", args[0], result)
		}
		return nil
	}
	out, err := runCmd(ctx, "/usr/bin/systemctl", "is-active", "--", args[0])
	logf("unit %s state: %s", args[0], strings.TrimSpace(out))
	return err
}

// pruneDocker reclaims disk from dangling images and unused build cache.
// Deliberately NOT `docker system prune`: it also removes stopped containers
// (whose locally-built image then dangles and is swept in the same run) and
// networks unused by any container — with all edge containers stopped from
// the panel, the EXTERNAL `edge` network is deleted and `up -d` afterwards
// fails. Containers and networks are never touched here.
func pruneDocker(ctx context.Context, args []string, logf func(string, ...any)) error {
	out, err := runCmd(ctx, "/usr/bin/docker", "image", "prune", "-f")
	logf("image prune: %s", strings.TrimSpace(out))
	if err != nil {
		return fmt.Errorf("image prune: %v: %s", err, out)
	}
	out, err = runCmd(ctx, "/usr/bin/docker", "builder", "prune", "-f")
	logf("builder prune: %s", strings.TrimSpace(out))
	if err != nil {
		return fmt.Errorf("builder prune: %v: %s", err, out)
	}
	return nil
}

func listContainers(ctx context.Context, args []string, logf func(string, ...any)) error {
	out, err := runCmd(ctx, "/usr/bin/docker", "ps", "-a", "--format", "{{json .}}")
	if err != nil {
		return fmt.Errorf("docker ps: %v: %s", err, out)
	}
	// JSON Lines (one object per container) on stdout; panel-api parses this.
	fmt.Print(out)
	return nil
}

// edgeNetworkIPs lists the edge network's endpoints (containerID → {Name,
// IPv4Address, …} JSON on stdout). The LLM fallback router (panel-api) uses
// it to map a configured instance NAME to its bridge IP — `docker ps` only
// exposes network names, not per-network IPs. Read-only, no args.
func edgeNetworkIPs(ctx context.Context, args []string, logf func(string, ...any)) error {
	out, err := runCmd(ctx, "/usr/bin/docker", "network", "inspect", "edge", "--format", "{{json .Containers}}")
	if err != nil {
		return fmt.Errorf("docker network inspect edge: %v: %s", err, out)
	}
	// JSON object on stdout; panel-api parses it (and degrades to "no fleet"
	// when the edge network itself is absent — that's a FAIL here, so the
	// router reports every instance down instead of guessing).
	fmt.Print(out)
	return nil
}

func containerAction(verb string) RunFunc {
	return func(ctx context.Context, args []string, logf func(string, ...any)) error {
		out, err := runCmd(ctx, "/usr/bin/docker", verb, "--", args[0])
		if err != nil {
			return fmt.Errorf("%s %s: %v: %s", verb, args[0], err, out)
		}
		logf("container %s: %s", args[0], verb)
		return nil
	}
}

// startService brings a deployed-stack service up with `docker compose up -d
// <svc>` against /opt/panel: creates the container if it does not exist
// (pruned, or never created) and starts it, recreating it when its recorded
// config has drifted from the compose file. The service name is the only arg,
// pre-validated as an opaque token; the project paths are hardcoded.
//
// The render includes the GPU pool override, like EVERY other invocation that
// can create/recreate a fleet container (llm-apply, llm-stop, COMPOSE_STACK in
// setup/common.sh): without it, reviving an llm-* instance would pin the
// base-file fallback cards instead of its assigned set. The file always exists
// (make deploy/api seed it, panel-api re-renders it) — a no-op for non-llm-*
// services, whose names never appear in it.
func startService(ctx context.Context, args []string, logf func(string, ...any)) error {
	out, err := runCmd(ctx, "/usr/bin/docker", "compose", "--project-directory", "/opt/panel",
		"-f", "/opt/panel/docker-compose.yml", "-f", "/opt/panel-api/llm-gpu-override.yml",
		"up", "-d", "--", args[0])
	if err != nil {
		return fmt.Errorf("up -d %s: %v: %s", args[0], err, out)
	}
	logf("service %s: up -d ok", args[0])
	return nil
}

// listServices emits the deployed stack's services carrying the panel.manage
// label — the Services view's contract — as a JSON array of {name, image,
// display, restart, category}. category is the panel.category label (the
// view's collapsible group; "" = uncategorized, rendered as "Other").
// Read-only, no args; the frontend diffs this against the container list to
// offer create+start for services whose container is gone.
func listServices(ctx context.Context, args []string, logf func(string, ...any)) error {
	out, err := runCmd(ctx, "/usr/bin/docker", "compose", "--project-directory", "/opt/panel",
		"-f", "/opt/panel/docker-compose.yml", "config", "--format", "json")
	if err != nil {
		return fmt.Errorf("compose config: %v: %s", err, out)
	}
	var cfg struct {
		Services map[string]struct {
			Image   string          `json:"image"`
			Restart string          `json:"restart"`
			Labels  json.RawMessage `json:"labels"`
		} `json:"services"`
	}
	if err := json.Unmarshal([]byte(out), &cfg); err != nil {
		return fmt.Errorf("compose config: bad JSON: %v", err)
	}
	type svc struct {
		Name     string `json:"name"`
		Image    string `json:"image"`
		Display  string `json:"display"`
		Restart  string `json:"restart"`
		Category string `json:"category"`
	}
	svcs := []svc{}
	for name, s := range cfg.Services {
		display := composeLabel(s.Labels, "panel.manage")
		if display == "" {
			continue // not in the Services view
		}
		if display == "true" {
			display = name
		}
		category := composeLabel(s.Labels, "panel.category")
		svcs = append(svcs, svc{name, s.Image, display, s.Restart, category})
	}
	sort.Slice(svcs, func(i, j int) bool { return svcs[i].Name < svcs[j].Name })
	enc, err := json.Marshal(svcs)
	if err != nil {
		return fmt.Errorf("list-services: marshal: %v", err)
	}
	fmt.Println(string(enc))
	return nil
}

// composeLabel reads one label from `compose config` JSON output. CLI versions
// have shipped the labels field as either an object or a list of "k=v"
// strings; accept both, return "" when absent or unparseable (callers treat
// that as "not managed" — the safe direction).
func composeLabel(raw json.RawMessage, key string) string {
	var obj map[string]any
	if err := json.Unmarshal(raw, &obj); err == nil {
		if s, ok := obj[key].(string); ok {
			return s
		}
		return ""
	}
	var list []string
	if err := json.Unmarshal(raw, &list); err == nil {
		for _, kv := range list {
			if k, v, ok := strings.Cut(kv, "="); ok && k == key {
				return v
			}
		}
	}
	return ""
}

// ── auto-update (make update, on demand and on a schedule) ────────────────────
//
// What runs is the SAME 08/09 scripts a manual `make update` / `make rollback`
// runs — doctor precondition, snapshot, gates, auto-rollback, lock — so the
// unattended path has no second safety net to drift. Two things differ:
//
//   - the long work is detached. 08 restarts panel-api (the caller for
//     panel-initiated runs) mid-run and takes tens of minutes, so
//     update-run / update-run-force / update-rollback launch the script with
//     --detach (update_maybe_detach in common.sh: re-exec in a new session,
//     log to /opt/panel/.update-run.log) and return within seconds.
//   - the clone to run from is the one that last recorded itself: every
//     `make update` writes repo_root into the state file, and every auto-exec
//     re-validates it below (a swappable clone = root code execution on the
//     next timer fire). No git is involved in the auto path: it floats the
//     lines, and the clone only moves when the admin edits it.

const (
	updateStatePath  = "/opt/panel/.update-state"
	updateRunLogPath = "/opt/panel/.update-run.log"
)

// repo_root as recorded in the state file: absolute, sane characters, no
// empty/`.`/`..` segments (the checks below stat the resolved path, but the
// input stays strict and simple — no "//", no trailing slash).
var updatePathRe = regexp.MustCompile(`^/[A-Za-z0-9._/-]{1,254}$`)

func validUpdatePath(p string) bool {
	if !updatePathRe.MatchString(p) {
		return false
	}
	for _, seg := range strings.Split(p[1:], "/") {
		if seg == "" || seg == "." || seg == ".." {
			return false
		}
	}
	return true
}

// ANSI colour escapes (the update scripts print coloured info/warn lines —
// the dashboard's <pre> must not receive escape sequences).
var ansiRe = regexp.MustCompile(`\x1b\[[0-9;]*m`)

func stripANSI(s string) string { return ansiRe.ReplaceAllString(s, "") }

// tailStr — last n bytes of a (trimmed) string: git/docker error output
// appends the useful line ("fatal: …") to the end.
func tailStr(s string, n int) string {
	s = strings.TrimSpace(s)
	if len(s) > n {
		s = "…" + s[len(s)-n:]
	}
	return s
}

// tailLines — last n lines of a file (reading at most maxBytes from the end);
// "" when the file is absent (the dashboard renders nothing).
func tailLines(path string, n, maxBytes int) string {
	data, err := os.ReadFile(path)
	if err != nil {
		return ""
	}
	if len(data) > maxBytes {
		data = data[len(data)-maxBytes:]
	}
	lines := strings.Split(strings.TrimSpace(string(data)), "\n")
	if len(lines) > n {
		lines = lines[len(lines)-n:]
	}
	return strings.Join(lines, "\n")
}

// updateStateRaw reads the update state file (root-written, 0644 — the
// dashboard's control plane). Absent before the first update.
func updateStateRaw() (map[string]any, error) {
	raw, err := os.ReadFile(updateStatePath)
	if err != nil {
		return nil, err
	}
	var st map[string]any
	if err := json.Unmarshal(raw, &st); err != nil {
		return nil, fmt.Errorf("update state unreadable: %v", err)
	}
	return st, nil
}

func panelUserID() (uint32, error) {
	u, err := user.Lookup("panel")
	if err != nil {
		return 0, err
	}
	n, err := strconv.Atoi(u.Uid)
	if err != nil {
		return 0, err
	}
	return uint32(n), nil
}

func firstLine(p string) (string, error) {
	f, err := os.Open(p)
	if err != nil {
		return "", err
	}
	defer f.Close()
	br := bufio.NewReader(f)
	l, err := br.ReadString('\n')
	if err != nil && err != io.EOF {
		return "", err
	}
	return strings.TrimRight(l, "\r\n"), nil
}

// checkUpdateScript validates <repo>/setup/<name> as a script root may
// auto-exec: regular executable file with the repo's bash shebang, and the
// file, its setup/ directory, and every ancestor directory up to / not
// group/other-writable and not owned by the panel user — the same standard
// as this binary's self-check. A group/other-writable DIRECTORY anywhere in
// the chain would let a local user delete-and-replace the file (a writable
// setup/) or the clone directory itself (a writable ancestor), regardless of
// the file's own mode — and every check below would pass on the stand-in —
// so the WHOLE chain is checked, not just the leaf directories.
func checkUpdateScript(repo, name string) (string, error) {
	script := filepath.Join(repo, "setup", name)
	panelUID, panelErr := panelUserID()
	okOwner := func(inf os.FileInfo, label string) error {
		if panelErr == nil {
			if sys, ok := inf.Sys().(*syscall.Stat_t); ok && sys.Uid == panelUID {
				return fmt.Errorf("%s is owned by the panel user", label)
			}
		}
		return nil
	}
	checkDir := func(d string) error {
		inf, err := os.Stat(d)
		if err != nil {
			return fmt.Errorf("%s: %v", d, err)
		}
		if !inf.IsDir() {
			return fmt.Errorf("%s is not a directory", d)
		}
		if inf.Mode()&0o022 != 0 {
			return fmt.Errorf("%s is group/other-writable — the clone must live somewhere only you (and root) can write", d)
		}
		return okOwner(inf, d)
	}
	// The ancestor walk starts at the repo and ends at / (Dir("/")=="/" is
	// the fixed point): a writable directory ABOVE the clone lets a local
	// user rm-and-replace the clone dir itself, so it is checked too.
	for d := repo; ; d = filepath.Dir(d) {
		if err := checkDir(d); err != nil {
			return "", err
		}
		if d == "/" {
			break
		}
	}
	if err := checkDir(filepath.Dir(script)); err != nil {
		return "", err
	}
	inf, err := os.Stat(script)
	if err != nil {
		return "", fmt.Errorf("%s: %v", script, err)
	}
	if inf.IsDir() {
		return "", fmt.Errorf("%s is a directory", script)
	}
	if inf.Mode()&0o111 == 0 {
		return "", fmt.Errorf("%s is not executable — re-pull the repo (the exec bit is committed)", script)
	}
	if inf.Mode()&0o022 != 0 {
		return "", fmt.Errorf("%s is group/other-writable", script)
	}
	if err := okOwner(inf, script); err != nil {
		return "", err
	}
	// The kernel picks the interpreter from the shebang; refuse to exec
	// anything that does not start the way the repo's scripts do.
	line, err := firstLine(script)
	if err != nil {
		return "", fmt.Errorf("%s: %v", script, err)
	}
	if line != "#!/usr/bin/env bash" {
		return "", fmt.Errorf("%s has no bash shebang (first line %q) — not the repo's update script", script, line)
	}
	return script, nil
}

// updateClonePath — the registered server clone (the state's repo_root): the
// checkout every `make update` last ran from.
func updateClonePath() (string, error) {
	st, err := updateStateRaw()
	if err != nil {
		return "", fmt.Errorf("no update state at %s — run `make update` manually once (from the server clone) so the auto-update knows which checkout to use", updateStatePath)
	}
	p, _ := st["repo_root"].(string)
	if !validUpdatePath(p) {
		return "", fmt.Errorf("update state holds an invalid repo_root %q (want an absolute path, plain characters, no '.'/'..' segments) — if the state was hand-edited: sudo rm %s; if the clone path is odd: move the clone, then re-run: make update", p, updateStatePath)
	}
	return p, nil
}

// updateRun — the auto path: the daily timer (root → panel-do) and the
// dashboard button. With force=false, uncommitted changes in the clone skip
// the run: the admin edits the clone only when present, and a half-finished
// edit belongs to a manual `make update` (or update-run-force). A git
// checkout whose status CANNOT be read refuses the run (fail closed — see
// the dirty check below). The launch itself is fast: --detach re-execs the
// script in a new session and returns, so this action finishes in seconds no
// matter how long the update takes.
func updateRun(force bool) RunFunc {
	return func(ctx context.Context, args []string, logf func(string, ...any)) error {
		repo, err := updateClonePath()
		if err != nil {
			return err
		}
		script, err := checkUpdateScript(repo, "08-update.sh")
		if err != nil {
			return err
		}
		if !force {
			// -c safe.directory: this runs as ROOT in the ADMIN-OWNED clone with
			// a scrubbed env (no SUDO_UID/HOME) — without it, git ≥2.35.2 dies
			// with "detected dubious ownership" and the dirty check below could
			// not run at all.
			//
			// The dirty-skip is a GATE, so "cannot tell if the clone is clean"
			// must fail closed, not open. The two "can't tell" cases split
			// first (best effort — a .git too corrupt even to probe reads as
			// "not a checkout" and proceeds, same as the non-git path):
			//   - no git / not a git checkout → proceed (supported: repo_head
			//     is simply absent, the script's doctor precondition is the
			//     last gate);
			//   - a checkout whose status we FAILED to read (a corrupt index —
			//     e.g. power loss or an interrupted git write) → REFUSE: applying
			//     an unverifiable working tree unattended is exactly what the skip
			//     exists to prevent; the tick fails visibly in the unit's
			//     journal instead of silently applying. (A mere leftover
			//     .git/index.lock does NOT hit this: status still exits 0 and the
			//     working tree reads dirty → the skip path above.)
			isRepo, _ := runCmd(ctx, "/usr/bin/git", "-c", "safe.directory="+repo, "-C", repo, "rev-parse", "--is-inside-work-tree")
			if strings.TrimSpace(isRepo) == "true" {
				out, gerr := runCmd(ctx, "/usr/bin/git", "-c", "safe.directory="+repo, "-C", repo, "status", "--porcelain")
				switch {
				case gerr != nil:
					return fmt.Errorf("update-run: clone %s is a git checkout but git status failed (%v: %s) — refusing to apply an unverifiable working tree unattended; inspect the clone, then re-run (or use update-run-force deliberately)", repo, gerr, strings.TrimSpace(out))
				case strings.TrimSpace(out) != "":
					logf("update-run: SKIPPED — uncommitted changes in %s", repo)
					return skipResult{fmt.Sprintf("uncommitted changes in the clone (%s) — commit them, or use update-run-force", repo)}
				}
			} else {
				logf("update-run: %s is not a git checkout (or git unavailable) — no dirty check, proceeding", repo)
			}
		}
		out, err := runCmd(ctx, script, "--detach")
		if err != nil {
			// The pre-detach lock probe's die — an update is already running.
			// Coupled to update_maybe_detach's message in common.sh.
			if strings.Contains(out, "already running") {
				logf("update-run: SKIPPED — another update/rollback is already running")
				return skipResult{"another update/rollback is already running"}
			}
			return fmt.Errorf("update-run: %v: %s", err, out)
		}
		logf("update-run: %s", strings.TrimSpace(out))
		return nil
	}
}

// updateRollback — a full `make rollback` (same safety: local snapshots,
// re-gated, PARTIAL reporting), detached like update-run.
func updateRollback(ctx context.Context, args []string, logf func(string, ...any)) error {
	repo, err := updateClonePath()
	if err != nil {
		return err
	}
	script, err := checkUpdateScript(repo, "09-rollback.sh")
	if err != nil {
		return err
	}
	out, err := runCmd(ctx, script, "--detach")
	if err != nil {
		// The pre-detach lock probe's die — an update is already running.
		// Coupled to update_maybe_detach's message in common.sh.
		if strings.Contains(out, "already running") {
			logf("update-rollback: SKIPPED — another update/rollback is already running")
			return skipResult{"another update/rollback is already running"}
		}
		return fmt.Errorf("update-rollback: %v: %s", err, out)
	}
	logf("update-rollback: %s", strings.TrimSpace(out))
	return nil
}

// updateReport — what the last update changed, live: the state file (the
// pre-update image per service) joined with the currently running image, both
// with their org.opencontainers.image.version labels — panel-api classifies
// major/minor. Read-only, no args; valid JSON even before the first update
// (state null). Also reports the timer's state for the Settings card and the
// run-log tail for the in-progress banner.
func updateReport(ctx context.Context, args []string, logf func(string, ...any)) error {
	type row struct {
		Service     string  `json:"service"`
		PreImage    string  `json:"pre_image"`
		PreTags     string  `json:"pre_tags"`
		PreVersion  *string `json:"pre_version"`
		PostImage   *string `json:"post_image"`
		PostVersion *string `json:"post_version"`
		State       string  `json:"state"`
	}
	state, _ := updateStateRaw() // absent before the first update — fine
	rows := []row{}
	imageIDs := []string{}
	if state != nil {
		type pre struct {
			ImageID   string `json:"image_id"`
			ImageTags string `json:"image_tags"`
		}
		pres := map[string]pre{}
		if raw, ok := state["services"].(map[string]any); ok {
			for name, v := range raw {
				if m, ok := v.(map[string]any); ok {
					id, _ := m["image_id"].(string)
					tags, _ := m["image_tags"].(string)
					if id != "" {
						pres[name] = pre{id, tags}
						imageIDs = append(imageIDs, id)
					}
				}
			}
		}
		// The container per compose service, any state (label join — the same
		// join panel-api's container view uses).
		bySvc := map[string][2]string{} // service → {containerID, state}
		if ps, err := runCmd(ctx, "/usr/bin/docker", "ps", "-a",
			"--filter", "label=com.docker.compose.project=panel",
			"--format", "{{.Label \"com.docker.compose.service\"}}|{{.ID}}|{{.State}}"); err == nil {
			for _, line := range strings.Split(ps, "\n") {
				svc, rest, ok1 := strings.Cut(strings.TrimSpace(line), "|")
				cid, st, ok2 := strings.Cut(rest, "|")
				if !ok1 || !ok2 || svc == "" || cid == "" {
					continue
				}
				if _, seen := bySvc[svc]; !seen {
					bySvc[svc] = [2]string{cid, st}
				}
			}
		}
		// containerID (the ps short ID) → imageID. One inspect per container:
		// the join key (the ps short ID) exists in NO inspect template output,
		// so a batch --format call cannot be keyed reliably — and these are
		// ~10 fast local round-trips on a 10s dashboard poll.
		imageOf := map[string]string{}
		for _, c := range bySvc {
			if img, ierr := runCmd(ctx, "/usr/bin/docker", "inspect", "--format", "{{.Image}}", c[0]); ierr == nil {
				if img = strings.TrimSpace(img); img != "" {
					imageOf[c[0]] = img
				}
			}
		}
		postIDs := []string{}
		for _, c := range bySvc {
			if img := imageOf[c[0]]; img != "" {
				postIDs = append(postIDs, img)
			}
		}
		imageIDs = append(imageIDs, postIDs...)
		// Version labels for every image ID in ONE inspect. A missing one — a
		// pre-update image GC'd past the 14-day retention — simply comes back
		// without a label (docker prints the found ones, exits 1: tolerated).
		ver := map[string]*string{}
		if len(imageIDs) > 0 {
			uniq := map[string]bool{}
			dedup := []string{}
			for _, id := range imageIDs {
				if !uniq[id] {
					uniq[id] = true
					dedup = append(dedup, id)
				}
			}
			inspect := append([]string{"inspect", "--format",
				"{{.Id}} {{index .Config.Labels \"org.opencontainers.image.version\"}}"}, dedup...)
			if bi, _ := runCmd(ctx, "/usr/bin/docker", inspect...); bi != "" {
				for _, line := range strings.Split(bi, "\n") {
					// A GC'd image makes docker print "Error: No such object: …"
					// into the combined output — only real image IDs are rows.
					id, v, ok := strings.Cut(strings.TrimSpace(line), " ")
					if !ok || !strings.HasPrefix(id, "sha256:") {
						continue
					}
					// An image WITHOUT the label makes the Go template render the
					// nil *string as the literal text "<nil>" (modern docker) or
					// "<no value>" (older) — not an empty field. Treat both as
					// "no version": those images classify as `moved`, and the
					// dashboard's before→after column must not show the sentinel.
					if v != "" && v != "<nil>" && v != "<no value>" {
						ver[id] = &v
					}
				}
			}
		}
		names := make([]string, 0, len(pres))
		for n := range pres {
			names = append(names, n)
		}
		sort.Strings(names)
		for _, name := range names {
			p := pres[name]
			r := row{Service: name, PreImage: p.ImageID, PreTags: p.ImageTags, PreVersion: ver[p.ImageID]}
			if cur, ok := bySvc[name]; ok {
				r.State = cur[1]
				if img := imageOf[cur[0]]; img != "" {
					r.PostImage = &img
					r.PostVersion = ver[img]
				}
			}
			rows = append(rows, r)
		}
	}
	// The timer's state for the Settings card: enabled/disabled, or nil when
	// the unit file isn't installed (`make autoupdate` fixes that) — for a
	// missing unit, is-enabled prints "not-found" (systemd ≥ 250), which maps
	// to nil like anything that isn't enabled/disabled.
	timerOut, _ := runCmd(ctx, "/usr/bin/systemctl", "is-enabled", "--", "panel-autoupdate.timer")
	var timer *bool
	switch t := strings.TrimSpace(timerOut); t {
	case "enabled", "disabled":
		b := t == "enabled"
		timer = &b
	}
	res := map[string]any{
		"state":         state,
		"services":      rows,
		"timer_enabled": timer,
		"log_tail":      stripANSI(tailLines(updateRunLogPath, 40, 8192)),
	}
	enc, err := json.Marshal(res)
	if err != nil {
		return fmt.Errorf("update-report: marshal: %v", err)
	}
	fmt.Println(string(enc))
	return nil
}

// updatePreview — what the NEXT update would apply, fully local (no network,
// no worktree changes): the clone's git facts (dirty files, commits since the
// deployed head) plus the raw "line" files the dashboard compares (compose
// image lines, panel-web's FROM bases, npm ranges, pip ranges). The DEPLOYED
// copies of those files are readable by panel-api itself (0644 under
// /opt/panel + /opt/panel-api) — only the clone side needs root, which is
// what this action is for. Read-only, no args.
func updatePreview(ctx context.Context, args []string, logf func(string, ...any)) error {
	repo, err := updateClonePath()
	if err != nil {
		return err
	}
	res := map[string]any{
		"repo_root":     repo,
		"deployed_head": nil,
		"clone_head":    nil,
		"deployed_at":   nil,
		"status":        nil,
		"commits":       []string{},
		"commit_count":  0,
		"dirty":         []string{},
		"git_error":     nil,
		"files":         map[string]any{},
	}
	if state, err := updateStateRaw(); err == nil {
		if h, ok := state["repo_head"].(string); ok && h != "" {
			res["deployed_head"] = h
		}
		res["deployed_at"] = state["snapshot"]
		res["status"] = state["status"]
	}
	// -c safe.directory: root + admin-owned clone + scrubbed env — see updateRun.
	git := func(gargs ...string) (string, error) {
		return runCmd(ctx, "/usr/bin/git", append([]string{"-c", "safe.directory=" + repo, "-C", repo}, gargs...)...)
	}
	// Git facts only for a REAL checkout: a non-git clone is supported
	// (update-run skips the dirty check for it — repo_head is simply
	// absent), so "not a checkout" is NOT an error here. A checkout whose
	// status we CANNOT read is: update-run refuses it (fail closed), and the
	// dashboard must not offer a plain "run now" for a clone it cannot
	// verify — git_error carries the reason (corrupt index, …) and the
	// panel's "run anyway" is the deliberate path, as for a dirty clone.
	isRepo, _ := git("rev-parse", "--is-inside-work-tree")
	if strings.TrimSpace(isRepo) == "true" {
		if head, herr := git("rev-parse", "HEAD"); herr == nil {
			res["clone_head"] = strings.TrimSpace(head)
			if dh, ok := res["deployed_head"].(string); ok && dh != "" && dh != strings.TrimSpace(head) {
				// A sha the clone no longer has (re-cloned tree) → git fails →
				// the commit list stays empty; the line-file diff below still
				// holds. commit_count is the FULL count — the list is capped
				// at the last 30 for display. An EMPTY range (clone checked out
				// BEHIND the deployed head) is "nothing pending" — without the
				// non-empty guard, Split("") would yield a phantom [""] commit.
				if log, lerr := git("log", "--oneline", dh+"..HEAD"); lerr == nil && strings.TrimSpace(log) != "" {
					lines := strings.Split(strings.TrimSpace(log), "\n")
					res["commit_count"] = len(lines)
					if len(lines) > 30 {
						lines = lines[len(lines)-30:]
					}
					res["commits"] = lines
				}
			}
		} else {
			res["git_error"] = fmt.Sprintf("clone HEAD unreadable (%s) — the auto-update refuses it; inspect the clone, then re-run (or deliberately run anyway)", tailStr(head, 200))
		}
		if out, gerr := git("status", "--porcelain"); gerr != nil {
			res["git_error"] = fmt.Sprintf("git status failed on the clone (%s) — the auto-update refuses an unverifiable working tree; inspect the clone, then re-run (or deliberately run anyway)", tailStr(out, 200))
		} else if strings.TrimSpace(out) != "" {
			res["dirty"] = strings.Split(strings.TrimSpace(out), "\n")
		}
	}
	files := res["files"].(map[string]any)
	for name, p := range map[string]string{
		"compose":      filepath.Join(repo, "compose", "docker-compose.yml"),
		"dockerfile":   filepath.Join(repo, "compose", "panel-frontend", "Dockerfile"),
		"package_json": filepath.Join(repo, "compose", "panel-frontend", "package.json"),
		"requirements": filepath.Join(repo, "panel-api", "requirements.txt"),
	} {
		if b, rerr := os.ReadFile(p); rerr == nil {
			files[name] = string(b)
		}
	}
	enc, err := json.Marshal(res)
	if err != nil {
		return fmt.Errorf("update-preview: marshal: %v", err)
	}
	fmt.Println(string(enc))
	return nil
}

// autoupdateToggle — enable (on) / disable (off) the daily timer. The timer's
// oneshot calls this binary directly (already root) — panel-do stays the only
// privileged path even for the unattended trigger.
func autoupdateToggle(ctx context.Context, args []string, logf func(string, ...any)) error {
	verb := "disable"
	if args[0] == "on" {
		verb = "enable"
	}
	out, err := runCmd(ctx, "/usr/bin/systemctl", verb, "--now", "--", "panel-autoupdate.timer")
	if err != nil {
		return fmt.Errorf("autoupdate %s: %v: %s", verb, err, out)
	}
	logf("autoupdate timer %sed", verb)
	return nil
}

var registry = map[string]Action{
	"restart-service": {
		Summary: "Restart a systemd service",
		Args:    []*regexp.Regexp{unitNameRe},
		Timeout: 30 * time.Second,
		Confirm: true,
		Run:     restartService,
	},
	"prune-docker": {
		Summary: "Prune dangling images + build cache (never containers or networks)",
		Args:    nil,
		Timeout: 5 * time.Minute,
		Confirm: true,
		Run:     pruneDocker,
	},
	"list-containers": {
		Summary: "List all containers (JSON Lines on stdout)",
		Args:    nil,
		Timeout: 15 * time.Second,
		Confirm: false,
		Run:     listContainers,
	},
	"list-services": {
		Summary: "Deployed compose services with the panel.manage label (JSON on stdout)",
		Args:    nil,
		Timeout: 15 * time.Second,
		Confirm: false,
		Run:     listServices,
	},
	"start-service": {
		Summary: "Create + start a deployed compose service (revives a pruned one)",
		Args:    []*regexp.Regexp{serviceNameRe},
		Timeout: 5 * time.Minute, // detached start is seconds; 5m only covers a missing-image pull
		Confirm: true,
		Run:     startService,
	},
	"edge-ips": {
		Summary: "edge-network endpoints: containerID → {Name, IPv4Address} (JSON on stdout)",
		Args:    nil,
		Timeout: 15 * time.Second,
		Confirm: false,
		Run:     edgeNetworkIPs,
	},
	"start-container": {
		Summary: "Start a stopped container (12-hex ID)",
		Args:    []*regexp.Regexp{containerIDRe},
		Timeout: 60 * time.Second,
		Confirm: false,
		Run:     containerAction("start"),
	},
	"stop-container": {
		Summary: "Stop a running container (12-hex ID)",
		Args:    []*regexp.Regexp{containerIDRe},
		Timeout: 60 * time.Second,
		Confirm: true,
		Run:     containerAction("stop"),
	},
	"restart-container": {
		Summary: "Restart a container (12-hex ID)",
		Args:    []*regexp.Regexp{containerIDRe},
		Timeout: 90 * time.Second,
		Confirm: true,
		Run:     containerAction("restart"),
	},
	"llm-fleet": {
		Summary: "LLM fleet instances + cards needed (JSON on stdout)",
		Args:    nil,
		Timeout: 20 * time.Second,
		Confirm: false,
		Run:     llmFleet,
	},
	"llm-live": {
		Summary: "Live fleet containers: state + ROCR_VISIBLE_DEVICES holds (JSON on stdout)",
		Args:    nil,
		Timeout: 20 * time.Second,
		Confirm: false,
		Run:     llmLive,
	},
	"llm-apply": {
		Summary: "(Re)create + start an llm-* instance on the override's GPU set",
		Args:    []*regexp.Regexp{llmServiceRe},
		Timeout: 5 * time.Minute, // detached start is seconds; 5m covers a missing-image pull
		Confirm: true,
		Run:     llmApply,
	},
	"llm-stop": {
		Summary: "Stop an llm-* instance (container kept, cards released)",
		Args:    []*regexp.Regexp{llmServiceRe},
		Timeout: 90 * time.Second,
		Confirm: true,
		Run:     llmStop,
	},
	"update-run": {
		Summary: "Launch the gated stack update (detached; skips when the clone has uncommitted changes)",
		Args:    nil,
		Timeout: 5 * time.Minute,
		Confirm: true,
		Run:     updateRun(false),
	},
	"update-run-force": {
		Summary: "Launch the gated stack update even with uncommitted clone changes (detached)",
		Args:    nil,
		Timeout: 5 * time.Minute,
		Confirm: true,
		Run:     updateRun(true),
	},
	"update-report": {
		Summary: "Last update's before→after: state + live images + version labels + log tail (JSON)",
		Args:    nil,
		Timeout: 30 * time.Second,
		Confirm: false,
		Run:     updateReport,
	},
	"update-preview": {
		Summary: "What the next update would apply: clone git facts + line files (JSON)",
		Args:    nil,
		Timeout: 30 * time.Second,
		Confirm: false,
		Run:     updatePreview,
	},
	"update-rollback": {
		Summary: "Full rollback to the last update's snapshot (detached; re-gated)",
		Args:    nil,
		Timeout: 5 * time.Minute,
		Confirm: true,
		Run:     updateRollback,
	},
	"autoupdate-toggle": {
		Summary: "Enable (on) or disable (off) the daily auto-update timer",
		Args:    []*regexp.Regexp{regexp.MustCompile(`^(on|off)$`)},
		Timeout: 30 * time.Second,
		Confirm: true,
		Run:     autoupdateToggle,
	},
}
