package main

import "testing"

func TestDispatchVersion(t *testing.T) {
	if got := dispatch([]string{"version"}); got != 0 {
		t.Fatalf("version exit = %d, want 0", got)
	}
}

func TestDispatchUnknownCommand(t *testing.T) {
	if got := dispatch([]string{"bogus"}); got != 2 {
		t.Fatalf("unknown command exit = %d, want 2", got)
	}
}

func TestDispatchNoArgsShowsUsage(t *testing.T) {
	if got := dispatch(nil); got != 2 {
		t.Fatalf("no-args exit = %d, want 2", got)
	}
}

func TestDispatchHelp(t *testing.T) {
	if got := dispatch([]string{"help"}); got != 0 {
		t.Fatalf("help exit = %d, want 0", got)
	}
}

func TestTenantListOnMissingConfigIsEmptyOK(t *testing.T) {
	// A missing config is treated as empty (not an error): `tenant list` exits 0.
	if got := dispatch([]string{"tenant", "--config", t.TempDir() + "/.agate.json", "list"}); got != 0 {
		t.Fatalf("tenant list exit = %d, want 0", got)
	}
}

func TestTenantUsageWithoutSubcommand(t *testing.T) {
	if got := dispatch([]string{"tenant"}); got != 2 {
		t.Fatalf("tenant (no subcommand) exit = %d, want 2", got)
	}
}

func TestDeployWithoutTenantsFails(t *testing.T) {
	// Plan-only, but no tenants in a fresh config -> error exit 1 (never deploys).
	if got := dispatch([]string{"deploy", "--config", t.TempDir() + "/.agate.json"}); got != 1 {
		t.Fatalf("deploy with no tenants exit = %d, want 1", got)
	}
}

func TestIngestUsageWithoutTenant(t *testing.T) {
	if got := dispatch([]string{"ingest", "somefile.txt"}); got != 2 {
		t.Fatalf("ingest without --tenant exit = %d, want 2", got)
	}
}
