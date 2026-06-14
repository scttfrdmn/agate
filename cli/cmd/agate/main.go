// Command agate is the admin CLI for agate.
//
// Commands are small and verb-first, coreutils-style. The cloud-mutating commands
// (deploy, ingest) build and PRINT a plan by default and run it only with an
// explicit --confirm — agate never changes cloud state implicitly.
package main

import (
	"flag"
	"fmt"
	"os"
	"os/exec"

	"github.com/scttfrdmn/agate/cli/internal/commands"
	"github.com/scttfrdmn/agate/cli/internal/config"
)

// Version is the agate release (SemVer). Override at build time with:
//
//	go build -ldflags "-X main.Version=0.1.0"
var Version = "0.1.0"

func main() {
	os.Exit(dispatch(os.Args[1:]))
}

type command struct {
	name  string
	short string
	run   func(args []string) int
}

func commandSet() []command {
	return []command{
		{"version", "print the agate version", cmdVersion},
		{"tenant", "manage tenants (add/list)", cmdTenant},
		{"budget", "set/show a tenant's budget", cmdBudget},
		{"deploy", "plan/deploy the CDK stacks", cmdDeploy},
		{"ingest", "plan/upload a document into a tenant corpus", cmdIngest},
	}
}

func dispatch(args []string) int {
	cmds := commandSet()
	if len(args) == 0 {
		usage(cmds)
		return 2
	}
	name, rest := args[0], args[1:]
	if name == "-h" || name == "--help" || name == "help" {
		usage(cmds)
		return 0
	}
	for _, c := range cmds {
		if c.name == name {
			return c.run(rest)
		}
	}
	fmt.Fprintf(os.Stderr, "agate: unknown command %q\n", name)
	usage(cmds)
	return 2
}

func cmdVersion(args []string) int {
	if err := flag.NewFlagSet("version", flag.ContinueOnError).Parse(args); err != nil {
		return 2
	}
	fmt.Printf("agate %s\n", Version)
	return 0
}

// --- tenant ----------------------------------------------------------------

func cmdTenant(args []string) int {
	fs := flag.NewFlagSet("tenant", flag.ContinueOnError)
	path := fs.String("config", config.DefaultPath, "config file")
	if err := fs.Parse(args); err != nil {
		return 2
	}
	rest := fs.Args()
	if len(rest) == 0 {
		fmt.Fprintln(os.Stderr, "usage: agate tenant <add|list> [id]")
		return 2
	}
	c, err := config.Load(*path)
	if err != nil {
		return fail(err)
	}
	switch rest[0] {
	case "list":
		for _, t := range c.Tenants {
			b := c.Budgets[t]
			if b.USD > 0 {
				fmt.Printf("%s\t$%.2f %s\n", t, b.USD, b.Period)
			} else {
				fmt.Println(t)
			}
		}
		return 0
	case "add":
		if len(rest) < 2 {
			fmt.Fprintln(os.Stderr, "usage: agate tenant add <id>")
			return 2
		}
		added, err := c.AddTenant(rest[1])
		if err != nil {
			return fail(err)
		}
		if !added {
			fmt.Printf("tenant %q already present\n", rest[1])
			return 0
		}
		if err := config.Save(*path, c); err != nil {
			return fail(err)
		}
		fmt.Printf("added tenant %q\n", rest[1])
		return 0
	default:
		fmt.Fprintf(os.Stderr, "agate tenant: unknown subcommand %q\n", rest[0])
		return 2
	}
}

// --- budget ----------------------------------------------------------------

func cmdBudget(args []string) int {
	// Pull the leading positionals (`set <tenant>`) before flag parsing so the
	// natural `agate budget set chem --usd 250` ordering works (Go's flag package
	// otherwise stops at the first positional).
	sub, tenant, flags, ok := splitBudgetArgs(args)
	if !ok {
		fmt.Fprintln(os.Stderr, "usage: agate budget set <tenant> --usd <amount> [--period <label>]")
		return 2
	}
	fs := flag.NewFlagSet("budget", flag.ContinueOnError)
	path := fs.String("config", config.DefaultPath, "config file")
	usd := fs.Float64("usd", -1, "budget ceiling in USD")
	period := fs.String("period", "", "budget period label (e.g. 2026-fall)")
	if err := fs.Parse(flags); err != nil {
		return 2
	}
	if sub != "set" {
		fmt.Fprintln(os.Stderr, "usage: agate budget set <tenant> --usd <amount> [--period <label>]")
		return 2
	}
	if *usd < 0 {
		fmt.Fprintln(os.Stderr, "agate budget set: --usd is required and must be >= 0")
		return 2
	}
	c, err := config.Load(*path)
	if err != nil {
		return fail(err)
	}
	if err := c.SetBudget(tenant, *usd, *period); err != nil {
		return fail(err)
	}
	if err := config.Save(*path, c); err != nil {
		return fail(err)
	}
	fmt.Printf("set budget for %q: $%.2f %s\n", tenant, *usd, *period)
	return 0
}

// splitBudgetArgs separates the leading `set <tenant>` positionals from the flags
// that follow, so flags may appear after the positionals.
func splitBudgetArgs(args []string) (sub, tenant string, flags []string, ok bool) {
	if len(args) < 2 {
		return "", "", nil, false
	}
	return args[0], args[1], args[2:], true
}

// --- deploy ----------------------------------------------------------------

func cmdDeploy(args []string) int {
	fs := flag.NewFlagSet("deploy", flag.ContinueOnError)
	path := fs.String("config", config.DefaultPath, "config file")
	confirm := fs.Bool("confirm", false, "actually run the deploy (default: plan only)")
	if err := fs.Parse(args); err != nil {
		return 2
	}
	c, err := config.Load(*path)
	if err != nil {
		return fail(err)
	}
	plan, err := commands.DeployPlan(c, fs.Args())
	if err != nil {
		return fail(err)
	}
	return runOrPlan(plan, *confirm)
}

// --- ingest ----------------------------------------------------------------

func cmdIngest(args []string) int {
	fs := flag.NewFlagSet("ingest", flag.ContinueOnError)
	path := fs.String("config", config.DefaultPath, "config file")
	tenant := fs.String("tenant", "", "destination tenant")
	bucket := fs.String("bucket", "", "docs bucket (agate-docs-<acct>-<region>)")
	confirm := fs.Bool("confirm", false, "actually upload (default: plan only)")
	if err := fs.Parse(args); err != nil {
		return 2
	}
	if *tenant == "" || len(fs.Args()) < 1 {
		fmt.Fprintln(os.Stderr, "usage: agate ingest --tenant <id> [--bucket <name>] <file> [--confirm]")
		return 2
	}
	c, err := config.Load(*path)
	if err != nil {
		return fail(err)
	}
	plan, _, err := commands.IngestPlan(c, *tenant, *bucket, fs.Args()[0])
	if err != nil {
		return fail(err)
	}
	return runOrPlan(plan, *confirm)
}

// --- shared helpers --------------------------------------------------------

// runOrPlan prints the plan; with confirm it executes it, streaming output.
func runOrPlan(plan commands.Plan, confirm bool) int {
	fmt.Printf("plan: %s\n  %s\n", plan.Summary, plan.String())
	if !confirm {
		fmt.Println("(dry run — re-run with --confirm to execute)")
		return 0
	}
	cmd := exec.Command(plan.Argv[0], plan.Argv[1:]...) //nolint:gosec // argv built from validated config
	cmd.Stdout, cmd.Stderr, cmd.Stdin = os.Stdout, os.Stderr, os.Stdin
	if err := cmd.Run(); err != nil {
		return fail(err)
	}
	return 0
}

func fail(err error) int {
	fmt.Fprintf(os.Stderr, "agate: %v\n", err)
	return 1
}

func usage(cmds []command) {
	fmt.Fprintf(os.Stderr, "agate — admin CLI for agate (%s)\n\n", Version)
	fmt.Fprintln(os.Stderr, "usage: agate <command> [args]")
	fmt.Fprintln(os.Stderr, "\ncommands:")
	for _, c := range cmds {
		fmt.Fprintf(os.Stderr, "  %-9s %s\n", c.name, c.short)
	}
	fmt.Fprintln(os.Stderr, "\ncloud-mutating commands (deploy, ingest) plan by default; pass --confirm to run.")
}
