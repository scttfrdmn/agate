# Security Policy

agate is security-critical infrastructure: it brokers scoped, short-lived AWS credentials from a
verified campus identity and fences both model access and data scope with one ABAC tag scheme.
We take vulnerabilities seriously. The security rationale is documented in
[`docs/agate-security-ciso.md`](docs/agate-security-ciso.md).

## Reporting a vulnerability

**Please do not report security vulnerabilities through public GitHub Issues, Discussions, or
pull requests.**

Instead, use GitHub's private vulnerability reporting:

1. Go to the repository's **Security** tab → **Report a vulnerability**
   (**[Private Vulnerability Reporting](https://github.com/scttfrdmn/agate/security/advisories/new)**).
2. Describe the issue, the affected component, and reproduction steps.

We aim to acknowledge a report within **5 business days** and to keep you updated as we
investigate and remediate. Please give us reasonable time to fix an issue before any public
disclosure; we're happy to credit reporters who wish to be named.

## What to include

- The component (broker, identity/ABAC, a Lambda handler, the SPA transport, the CLI, IaC).
- Impact — what an attacker could do (privilege escalation, cross-tenant data access, credential
  leakage, scope widening, etc.).
- A concrete reproduction or proof-of-concept where possible.

## Scope

In scope: anything that could break the security model — credential handling, the
claims→scoped-STS broker, the ABAC session-tag scheme, cross-tenant/cross-scope data access,
IAM policy scope, token verification, or client-side handling of credentials.

Out of scope: findings that require already-privileged AWS access you were legitimately granted,
denial-of-service by resource exhaustion, and issues in third-party dependencies (report those
upstream; we'll bump the pin).

## Supported versions

agate is pre-1.0 (`0.y.z`); security fixes land on the latest release and `main`.
