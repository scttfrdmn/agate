// Package config is the pure, side-effect-light model behind the agate CLI's
// tenant and budget commands. The on-disk form (.agate.json) is the operator's
// declaration of which tenants exist and their per-period budgets; the deploy
// command turns the tenant set into `cdk -c tenants=...`, and the budgets feed
// the soft-cap the broker reads (design §7.1). Load/Save touch the filesystem;
// everything else is pure and table-tested.
package config

import (
	"encoding/json"
	"fmt"
	"os"
	"regexp"
	"sort"
)

// DefaultPath is the config file the CLI reads/writes when --config is unset.
const DefaultPath = ".agate.json"

// tenantID must match the agate:tenant tag charset (the ABAC isolation key); keep
// it in lockstep with the Python claims_to_tags sanitiser.
var tenantID = regexp.MustCompile(`^[a-zA-Z0-9._-]+$`)

// Budget is a per-tenant spend allocation the soft cap enforces.
type Budget struct {
	// USD ceiling for the period. Zero means "no allocation" (deny), nil-via-absence
	// means "no cap configured" (allow) — mirrors cost.softcap semantics.
	USD float64 `json:"usd"`
	// Period label carried into the spend-table key (e.g. "2026-fall").
	Period string `json:"period,omitempty"`
}

// Config is the whole declared state: tenants and their budgets, plus the AWS
// region the stacks target.
type Config struct {
	Region  string            `json:"region"`
	Tenants []string          `json:"tenants"`
	Budgets map[string]Budget `json:"budgets,omitempty"`
}

// New returns an empty config with sensible defaults.
func New() *Config {
	return &Config{Region: "us-east-1", Tenants: []string{}, Budgets: map[string]Budget{}}
}

// Load reads a config file; a missing file yields a fresh default (not an error),
// so first-run commands work without a pre-existing file.
func Load(path string) (*Config, error) {
	data, err := os.ReadFile(path)
	if os.IsNotExist(err) {
		return New(), nil
	}
	if err != nil {
		return nil, err
	}
	c := New()
	if err := json.Unmarshal(data, c); err != nil {
		return nil, fmt.Errorf("parse %s: %w", path, err)
	}
	if c.Budgets == nil {
		c.Budgets = map[string]Budget{}
	}
	return c, nil
}

// Save writes the config as indented JSON.
func Save(path string, c *Config) error {
	data, err := json.MarshalIndent(c, "", "  ")
	if err != nil {
		return err
	}
	return os.WriteFile(path, append(data, '\n'), 0o644)
}

// ValidateTenant checks an id against the ABAC tag charset.
func ValidateTenant(id string) error {
	if id == "" {
		return fmt.Errorf("tenant id is empty")
	}
	if !tenantID.MatchString(id) {
		return fmt.Errorf("tenant id %q has characters outside [A-Za-z0-9._-]", id)
	}
	return nil
}

// AddTenant adds a tenant (idempotent, validated, kept sorted). Returns whether it
// was newly added.
func (c *Config) AddTenant(id string) (bool, error) {
	if err := ValidateTenant(id); err != nil {
		return false, err
	}
	if c.HasTenant(id) {
		return false, nil
	}
	c.Tenants = append(c.Tenants, id)
	sort.Strings(c.Tenants)
	return true, nil
}

// HasTenant reports whether the tenant is declared.
func (c *Config) HasTenant(id string) bool {
	for _, t := range c.Tenants {
		if t == id {
			return true
		}
	}
	return false
}

// SetBudget sets a tenant's budget. The tenant must already exist (a budget for an
// unknown tenant is almost always a typo). Negative USD is rejected.
func (c *Config) SetBudget(tenant string, usd float64, period string) error {
	if !c.HasTenant(tenant) {
		return fmt.Errorf("unknown tenant %q; add it first", tenant)
	}
	if usd < 0 {
		return fmt.Errorf("budget must be >= 0, got %v", usd)
	}
	if c.Budgets == nil {
		c.Budgets = map[string]Budget{}
	}
	c.Budgets[tenant] = Budget{USD: usd, Period: period}
	return nil
}

// TenantsArg renders the tenant set as the value for `cdk -c tenants=...`.
func (c *Config) TenantsArg() string {
	out := make([]string, len(c.Tenants))
	copy(out, c.Tenants)
	sort.Strings(out)
	return joinComma(out)
}

func joinComma(xs []string) string {
	s := ""
	for i, x := range xs {
		if i > 0 {
			s += ","
		}
		s += x
	}
	return s
}
