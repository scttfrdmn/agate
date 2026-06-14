package commands

import (
	"strings"
	"testing"

	"github.com/scttfrdmn/aws-genai-gateway/cli/internal/config"
)

func cfg(tenants ...string) *config.Config {
	c := config.New()
	for _, t := range tenants {
		_, _ = c.AddTenant(t)
	}
	return c
}

func TestDeployPlanBuildsCdkCommand(t *testing.T) {
	p, err := DeployPlan(cfg("chem", "psych"), nil)
	if err != nil {
		t.Fatal(err)
	}
	got := p.String()
	for _, want := range []string{"npx cdk deploy", "--all", "-c tenants=chem,psych", "--require-approval never"} {
		if !strings.Contains(got, want) {
			t.Fatalf("plan %q missing %q", got, want)
		}
	}
}

func TestDeployPlanNamedStacks(t *testing.T) {
	p, _ := DeployPlan(cfg("chem"), []string{"agate-identity", "agate-data"})
	got := p.String()
	if !strings.Contains(got, "agate-identity agate-data") {
		t.Fatalf("named stacks not in plan: %q", got)
	}
	if strings.Contains(got, "--all") {
		t.Fatalf("should not use --all when stacks named: %q", got)
	}
}

func TestDeployPlanRequiresTenants(t *testing.T) {
	if _, err := DeployPlan(config.New(), nil); err == nil {
		t.Fatalf("deploy with no tenants should error")
	}
}

func TestIngestPlanTargetsTenantPrefix(t *testing.T) {
	c := cfg("chem")
	p, target, err := IngestPlan(c, "chem", "agate-docs-123-us-east-1", "/local/syllabus.pdf")
	if err != nil {
		t.Fatal(err)
	}
	if target.Key != "chem/syllabus.pdf" {
		t.Fatalf("key=%q want chem/syllabus.pdf", target.Key)
	}
	if target.S3URI() != "s3://agate-docs-123-us-east-1/chem/syllabus.pdf" {
		t.Fatalf("uri=%q", target.S3URI())
	}
	if !strings.Contains(p.String(), "aws s3 cp /local/syllabus.pdf s3://agate-docs-123-us-east-1/chem/syllabus.pdf") {
		t.Fatalf("unexpected plan: %q", p.String())
	}
}

func TestIngestPlanRejectsUnknownTenant(t *testing.T) {
	if _, _, err := IngestPlan(cfg("chem"), "psych", "b", "/f.txt"); err == nil {
		t.Fatalf("unknown tenant should error")
	}
}

func TestIngestPlanRequiresBucket(t *testing.T) {
	if _, _, err := IngestPlan(cfg("chem"), "chem", "", "/f.txt"); err == nil {
		t.Fatalf("missing bucket should error")
	}
}

func TestIngestPlanBaseNameVariants(t *testing.T) {
	c := cfg("chem")
	for path, want := range map[string]string{
		"/a/b/c.txt": "chem/c.txt",
		"flat.md":    "chem/flat.md",
		"/trailing/": "chem/trailing",
	} {
		_, target, err := IngestPlan(c, "chem", "bkt", path)
		if err != nil {
			t.Fatalf("%s: %v", path, err)
		}
		if target.Key != want {
			t.Fatalf("path %q -> key %q want %q", path, target.Key, want)
		}
	}
}
