// Command agg is the admin CLI for aws-genai-gateway.
//
// Phase 0 ships the skeleton: a dispatcher, `version`, and stubs for the
// commands fleshed out in Phase 7 (deploy / tenant / budget / ingest). Commands
// are small and verb-first, coreutils-style. Standard library only for now —
// no SDK dependency until a command needs one.
package main

import (
	"flag"
	"fmt"
	"os"
)

// Version is the agg release (SemVer). Override at build time with:
//
//	go build -ldflags "-X main.Version=0.1.0"
var Version = "0.1.0"

type command struct {
	name  string
	short string
	run   func(args []string) int
}

func main() {
	os.Exit(dispatch(os.Args[1:]))
}

func dispatch(args []string) int {
	commands := []command{
		{"version", "print the agg version", cmdVersion},
		{"deploy", "deploy/update the agg stacks (Phase 7)", stub("deploy")},
		{"tenant", "manage tenants/cost centers (Phase 7)", stub("tenant")},
		{"budget", "set per-tenant/user budgets (Phase 7)", stub("budget")},
		{"ingest", "ingest documents into a tenant index (Phase 7)", stub("ingest")},
	}

	if len(args) == 0 {
		usage(commands)
		return 2
	}
	name, rest := args[0], args[1:]
	if name == "-h" || name == "--help" || name == "help" {
		usage(commands)
		return 0
	}
	for _, c := range commands {
		if c.name == name {
			return c.run(rest)
		}
	}
	fmt.Fprintf(os.Stderr, "agg: unknown command %q\n", name)
	usage(commands)
	return 2
}

func cmdVersion(args []string) int {
	fs := flag.NewFlagSet("version", flag.ContinueOnError)
	if err := fs.Parse(args); err != nil {
		return 2
	}
	fmt.Printf("agg %s\n", Version)
	return 0
}

// stub returns a placeholder runner for commands implemented in a later phase.
func stub(name string) func([]string) int {
	return func([]string) int {
		fmt.Fprintf(os.Stderr, "agg %s: not implemented yet (Phase 7)\n", name)
		return 1
	}
}

func usage(commands []command) {
	fmt.Fprintf(os.Stderr, "agg — admin CLI for aws-genai-gateway (%s)\n\n", Version)
	fmt.Fprintln(os.Stderr, "usage: agg <command> [args]")
	fmt.Fprintln(os.Stderr, "\ncommands:")
	for _, c := range commands {
		fmt.Fprintf(os.Stderr, "  %-9s %s\n", c.name, c.short)
	}
}
