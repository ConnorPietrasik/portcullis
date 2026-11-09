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
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
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
//      Return errors. Keep each action under ~30 lines.
//   4. Separators like "--" belong hardcoded inside runCmd calls, never as args.
// ============================================================

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
// display, restart}. Read-only, no args; the frontend diffs this against the
// container list to offer create+start for services whose container is gone.
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
		Name    string `json:"name"`
		Image   string `json:"image"`
		Display string `json:"display"`
		Restart string `json:"restart"`
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
		svcs = append(svcs, svc{name, s.Image, display, s.Restart})
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
}
