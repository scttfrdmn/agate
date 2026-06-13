package config

import (
	"os"
	"path/filepath"
	"testing"
)

func TestAddTenantValidatesAndDedupes(t *testing.T) {
	c := New()
	added, err := c.AddTenant("chem")
	if err != nil || !added {
		t.Fatalf("first add: added=%v err=%v", added, err)
	}
	added, _ = c.AddTenant("chem")
	if added {
		t.Fatalf("duplicate add should be a no-op")
	}
	if _, err := c.AddTenant("bad tenant!"); err == nil {
		t.Fatalf("expected validation error for bad id")
	}
}

func TestAddTenantKeepsSorted(t *testing.T) {
	c := New()
	for _, id := range []string{"psych", "chem", "kempner"} {
		if _, err := c.AddTenant(id); err != nil {
			t.Fatal(err)
		}
	}
	want := []string{"chem", "kempner", "psych"}
	for i, v := range want {
		if c.Tenants[i] != v {
			t.Fatalf("tenants[%d]=%q want %q", i, c.Tenants[i], v)
		}
	}
}

func TestSetBudgetRequiresKnownTenant(t *testing.T) {
	c := New()
	if err := c.SetBudget("nope", 100, "2026-fall"); err == nil {
		t.Fatalf("budget for unknown tenant should error")
	}
	_, _ = c.AddTenant("chem")
	if err := c.SetBudget("chem", 100, "2026-fall"); err != nil {
		t.Fatal(err)
	}
	if c.Budgets["chem"].USD != 100 || c.Budgets["chem"].Period != "2026-fall" {
		t.Fatalf("budget not set: %+v", c.Budgets["chem"])
	}
}

func TestSetBudgetRejectsNegative(t *testing.T) {
	c := New()
	_, _ = c.AddTenant("chem")
	if err := c.SetBudget("chem", -1, ""); err == nil {
		t.Fatalf("negative budget should error")
	}
}

func TestTenantsArg(t *testing.T) {
	c := New()
	for _, id := range []string{"psych", "chem"} {
		_, _ = c.AddTenant(id)
	}
	if got := c.TenantsArg(); got != "chem,psych" {
		t.Fatalf("TenantsArg=%q want chem,psych", got)
	}
}

func TestLoadMissingFileReturnsDefault(t *testing.T) {
	c, err := Load(filepath.Join(t.TempDir(), "absent.json"))
	if err != nil {
		t.Fatalf("missing file should not error: %v", err)
	}
	if c.Region != "us-east-1" || len(c.Tenants) != 0 {
		t.Fatalf("unexpected default: %+v", c)
	}
}

func TestSaveLoadRoundTrip(t *testing.T) {
	path := filepath.Join(t.TempDir(), ".agg.json")
	c := New()
	_, _ = c.AddTenant("chem")
	_ = c.SetBudget("chem", 250, "2026-fall")
	if err := Save(path, c); err != nil {
		t.Fatal(err)
	}
	got, err := Load(path)
	if err != nil {
		t.Fatal(err)
	}
	if !got.HasTenant("chem") || got.Budgets["chem"].USD != 250 {
		t.Fatalf("round-trip lost data: %+v", got)
	}
}

func TestLoadRejectsMalformedJSON(t *testing.T) {
	path := filepath.Join(t.TempDir(), "bad.json")
	_ = os.WriteFile(path, []byte("{not json"), 0o644)
	if _, err := Load(path); err == nil {
		t.Fatalf("malformed json should error")
	}
}
