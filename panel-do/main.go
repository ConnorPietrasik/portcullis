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
//   1. Exec ONLY via runCmd(ctx, "/absolute/path", ...). Never bash/sh/zsh,
//      never direct exec.Command, never string-built command lines.
//   2. Args arrive pre-validated as OPAQUE TOKENS (no '/', no '-', no '..').
//      Never treat an arg as a file path. Never os.Open/os.Create by arg.
//   3. No os.Getenv, no network, no goroutines, no os.Exit, no panic.
//      Return errors. Keep each action under ~30 lines.
//   4. Separators like "--" belong hardcoded inside runCmd calls, never as args.
// ============================================================

var unitNameRe = regexp.MustCompile(`^[a-zA-Z0-9][a-zA-Z0-9._@-]*\.service$`)
var containerIDRe = regexp.MustCompile(`^[0-9a-f]{12}$`)

func restartService(ctx context.Context, args []string, logf func(string, ...any)) error {
	if out, err := runCmd(ctx, "/usr/bin/systemctl", "restart", "--", args[0]); err != nil {
		return fmt.Errorf("restart %s: %v: %s", args[0], err, out)
	}
	out, err := runCmd(ctx, "/usr/bin/systemctl", "is-active", "--", args[0])
	logf("unit %s state: %s", args[0], strings.TrimSpace(out))
	return err
}

func pruneDocker(ctx context.Context, args []string, logf func(string, ...any)) error {
	out, err := runCmd(ctx, "/usr/bin/docker", "system", "prune", "-f")
	logf("prune: %s", strings.TrimSpace(out))
	return err
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

var registry = map[string]Action{
	"restart-service": {
		Summary: "Restart a systemd service",
		Args:    []*regexp.Regexp{unitNameRe},
		Timeout: 30 * time.Second,
		Confirm: true,
		Run:     restartService,
	},
	"prune-docker": {
		Summary: "Prune unused docker data",
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
}
