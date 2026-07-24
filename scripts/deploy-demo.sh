#!/usr/bin/env bash
# deploy-demo.sh — one command to stand up the smallest coherent agate demo and print how to
# use (and tear down) it. This is the EVALUATOR path; the README's step-by-step remains the
# reference for a real/campus deploy.
#
# It deploys only Agate Core's browser path + a throwaway demo IdP:
#   agate-identity  agate-data  agate-audit  agate-chokepoint  agate-web  agate-demo-idp
#
# It encodes the footguns learned the hard way:
#   - uses the REPO-PINNED cdk CLI (never a global npx cdk — schema-mismatch trap)
#   - derives every VITE_* from stack outputs and REBUILDS the web bundle before publishing
#     (a config-less bundle silently breaks login/chat)
#   - two-pass demo-idp so the Hosted-UI callback matches the real CloudFront URL
#
# Real AWS resources are created. Read the plan it prints; it asks before deploying.

set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
CDK="./node_modules/.bin/cdk"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

CORE_STACKS=(agate-identity agate-data agate-audit agate-chokepoint agate-web)

say()  { printf '\n\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# --- 1. validate prerequisites ---------------------------------------------
say "Checking prerequisites"
command -v uv   >/dev/null || die "uv not found — https://docs.astral.sh/uv/"
command -v node >/dev/null || die "node not found (needed for the aws-cdk CLI + jsii)"
command -v aws  >/dev/null || die "aws CLI not found"
command -v jq   >/dev/null || die "jq not found (used to read stack outputs)"
aws sts get-caller-identity >/dev/null 2>&1 || die "no AWS credentials (set AWS_PROFILE or env creds)"
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
echo "  AWS account $ACCOUNT / region $REGION"

say "Installing pinned toolchain (uv sync + the repo-pinned aws-cdk CLI)"
uv sync >/dev/null
npm install >/dev/null
[ -x "$CDK" ] || die "pinned cdk CLI missing after npm install"
echo "  cdk $($CDK --version)"

# --- 2. plan + confirm ------------------------------------------------------
cat <<PLAN

This will deploy the demo path to account $ACCOUNT ($REGION):
  ${CORE_STACKS[*]}  agate-demo-idp

Idle cost: ~\$0 (NO CLOCKS — no NAT/OpenSearch/always-on compute in this path).
Per-use cost: Bedrock tokens + S3/CloudFront bytes + Lambda invocations only.
Storage: S3 (docs, vectors, web bucket) bills per-GB while it exists — teardown removes it.
PLAN
read -r -p "Proceed? [y/N] " ok
[[ "$ok" == "y" || "$ok" == "Y" ]] || { echo "Aborted."; exit 0; }

out() { # out <stack> <OutputKey>
  aws cloudformation describe-stacks --stack-name "$1" --region "$REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='$2'].OutputValue | [0]" --output text 2>/dev/null
}

# --- 3. bootstrap (idempotent) + deploy core --------------------------------
say "Bootstrapping CDK (idempotent) and deploying core stacks"
AWS_REGION="$REGION" $CDK bootstrap "aws://$ACCOUNT/$REGION" >/dev/null 2>&1 || true
AWS_REGION="$REGION" $CDK deploy "${CORE_STACKS[@]}" --require-approval never

# --- 4. demo IdP, two-pass so the callback matches the deployed site --------
SITE_URL="$(out agate-web SiteUrl)"
[ -n "$SITE_URL" ] && [ "$SITE_URL" != "None" ] || die "agate-web produced no SiteUrl"
say "Deploying demo IdP with callback $SITE_URL"
AWS_REGION="$REGION" $CDK deploy agate-demo-idp -c "site_url=$SITE_URL" --require-approval never

# --- 5. rebuild the web bundle with real endpoints, then republish ----------
say "Building the web bundle with endpoints derived from stack outputs"
# Assign then export separately so a failed `out` lookup surfaces (SC2155): a config-less
# bundle silently breaks login/chat, so empty endpoints must not slip through unnoticed.
VITE_AWS_REGION="$REGION"
VITE_BROKER_URL="$(out agate-identity BrokerUrl)"
VITE_RETRIEVAL_URL="$(out agate-identity RetrievalUrl)"
VITE_CHOKEPOINT_URL="$(out agate-chokepoint ChokepointUrl)"
VITE_COGNITO_DOMAIN="$(out agate-demo-idp HostedUiDomain)"
VITE_COGNITO_CLIENT_ID="$(out agate-demo-idp OidcAudience)"
export VITE_AWS_REGION VITE_BROKER_URL VITE_RETRIEVAL_URL VITE_CHOKEPOINT_URL \
  VITE_COGNITO_DOMAIN VITE_COGNITO_CLIENT_ID
[ -n "$VITE_BROKER_URL" ] && [ "$VITE_BROKER_URL" != "None" ] || die "no BrokerUrl — is agate-identity deployed?"
(
  cd web
  npm ci >/dev/null 2>&1 || npm install >/dev/null
  npm run build >/dev/null
)
say "Republishing agate-web with the configured bundle"
AWS_REGION="$REGION" $CDK deploy agate-web --require-approval never

# --- 6. smoke test ----------------------------------------------------------
say "Smoke test"
code="$(curl -s -o /dev/null -w '%{http_code}' "$SITE_URL")"
[ "$code" = "200" ] && echo "  SPA responds 200 at $SITE_URL" || warn "SPA returned HTTP $code (CloudFront may still be propagating)"
JS="$(curl -s "$SITE_URL" | grep -o 'assets/index-[A-Za-z0-9_-]*\.js' | head -1)"
if [ -n "$JS" ] && curl -s "$SITE_URL/$JS" | grep -q "amazoncognito"; then
  echo "  bundle has login config baked in (good)"
else
  warn "could not confirm config in the live bundle yet (CloudFront cache?)"
fi

# --- 7. what to do next -----------------------------------------------------
cat <<DONE

$(say "Demo is up")
  URL:        $SITE_URL
  Log in:     the demo IdP has no users yet. Create one, e.g.:
    aws cognito-idp admin-create-user --user-pool-id $(out agate-demo-idp UserPoolId) \\
      --username demo --message-action SUPPRESS --region $REGION
    aws cognito-idp admin-set-user-password --user-pool-id $(out agate-demo-idp UserPoolId) \\
      --username demo --password '<StrongPass!1>' --permanent --region $REGION
    (set custom:affiliation / custom:tenant / custom:courses to scope the session)

  Resources created: ${CORE_STACKS[*]} agate-demo-idp
  Idle cost: ~\$0. Storage (S3) bills per-GB until teardown.

  Tear down everything:
    $CDK destroy ${CORE_STACKS[*]} agate-demo-idp
DONE
