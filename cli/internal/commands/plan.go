// Package commands holds the pure command-construction logic for the agate CLI:
// turning config + args into the exact external command (cdk / aws) that would
// run. Building the plan is pure and tested; executing it is a thin, explicitly
// confirmed step in the CLI layer. Nothing here runs a deploy or an upload —
// agate never mutates cloud state without an explicit --confirm.
package commands

import (
	"fmt"
	"strings"

	"github.com/scttfrdmn/agate/cli/internal/config"
)

// Plan is an external command the CLI would run, with a human-readable summary.
type Plan struct {
	Summary string   // one-line description shown before execution
	Argv    []string // the command + args, ready for exec
}

// String renders the plan as a shell-ish line for display.
func (p Plan) String() string {
	return strings.Join(p.Argv, " ")
}

// DeployPlan builds the `cdk deploy` invocation from config: the tenant set
// becomes `-c tenants=...`, and the region is passed through. Stacks default to
// all four when none are named.
func DeployPlan(c *config.Config, stacks []string) (Plan, error) {
	if len(c.Tenants) == 0 {
		return Plan{}, fmt.Errorf("no tenants configured; run `agate tenant add <id>` first")
	}
	if len(stacks) == 0 {
		stacks = []string{"--all"}
	}
	argv := []string{"npx", "cdk", "deploy"}
	argv = append(argv, stacks...)
	argv = append(argv, "-c", "tenants="+c.TenantsArg())
	argv = append(argv, "--require-approval", "never")
	summary := fmt.Sprintf(
		"deploy %s to region %s for tenants [%s]",
		strings.Join(stacks, ","), c.Region, c.TenantsArg(),
	)
	return Plan{Summary: summary, Argv: argv}, nil
}

// IngestTarget is where an ingested file lands: the tenant-prefixed S3 key under
// the docs bucket. The bucket name follows the data stack's convention.
type IngestTarget struct {
	Bucket string
	Key    string
}

// S3URI renders the full destination.
func (t IngestTarget) S3URI() string {
	return fmt.Sprintf("s3://%s/%s", t.Bucket, t.Key)
}

// IngestPlan builds the S3 upload destination for a local file into a tenant's
// prefix. The tenant MUST be configured (so a typo can't silently create a new,
// unscoped prefix). docsBucket is the deployed `agate-docs-<acct>-<region>` name.
func IngestPlan(c *config.Config, tenant, docsBucket, localPath string) (Plan, IngestTarget, error) {
	if !c.HasTenant(tenant) {
		return Plan{}, IngestTarget{}, fmt.Errorf("unknown tenant %q; add it first", tenant)
	}
	if docsBucket == "" {
		return Plan{}, IngestTarget{}, fmt.Errorf("docs bucket is required (deploy the data stack first)")
	}
	base := baseName(localPath)
	if base == "" {
		return Plan{}, IngestTarget{}, fmt.Errorf("local path %q has no file name", localPath)
	}
	// Key lands under {tenant}/... so the ingest Lambda derives the tenant from the
	// prefix (the FERPA-critical invariant in agate.rag.tenant_from_s3_key).
	target := IngestTarget{Bucket: docsBucket, Key: tenant + "/" + base}
	argv := []string{"aws", "s3", "cp", localPath, target.S3URI(), "--region", c.Region}
	summary := fmt.Sprintf("upload %s -> %s", localPath, target.S3URI())
	return Plan{Summary: summary, Argv: argv}, target, nil
}

func baseName(path string) string {
	// Trim a trailing slash, then take the last path segment.
	path = strings.TrimRight(path, "/")
	if i := strings.LastIndex(path, "/"); i >= 0 {
		return path[i+1:]
	}
	return path
}
