"""Shared Lambda-asset packaging config.

Several stacks bundle the repo root as a Lambda asset (the broker, ingest, LTI,
chokepoint functions all ship the pure `agate`/`policy` packages plus their handler).
They MUST exclude build/cache output — most importantly `cdk.out`, which lives at
the repo root and, if included, recursively copies the asset into itself until the
path length blows the filesystem limit. Keep the exclude list here so every stack
shares one correct definition.

`pip_bundled_code()` additionally pip-installs runtime deps (e.g. PyJWT for the
shared `agate.jwt_verify`) into the asset, via a Docker-free local bundler with a
Docker-image fallback — so handlers that import third-party packages work at
runtime. Centralised here so the broker/chokepoint/LTI bundlers can't drift.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import aws_cdk as cdk
import jsii
from aws_cdk import aws_lambda as lambda_

# Repo root (two levels up from infra/assets.py).
_ROOT = Path(__file__).resolve().parent.parent

# Excluded from every repo-root Lambda asset bundle. Globs are matched by CDK's
# asset copier; list both the root and nested forms to be safe across stacks.
LAMBDA_ASSET_EXCLUDES: list[str] = [
    # Build / synth output — the critical one (root-level cdk.out caused an
    # ENAMETOOLONG self-copy loop when omitted).
    "cdk.out",
    "infra/cdk.out",
    "**/cdk.out",
    # Other language trees + their build output.
    "web",
    "cli",
    "node_modules",
    "**/node_modules",
    # Docs + tests don't belong in a runtime artifact.
    "docs",
    "tests",
    # VCS + Python/tool caches.
    ".git",
    ".venv",
    "**/__pycache__",
    ".ruff_cache",
    ".pytest_cache",
    ".mypy_cache",
    ".coverage",
    "**/*.pyc",
]


# Runtime requirements pip-installed into a bundled asset. Pinned to match the
# project's pyproject (PyJWT[crypto] for agate.jwt_verify / lti.handler).
_PIP_REQUIREMENTS: list[str] = ["pyjwt[crypto]>=2.8"]

# The local bundler runs pip on the DEV host (often macOS/arm64), but the asset
# runs on the Lambda runtime (Linux/x86_64). pyjwt[crypto] pulls `cryptography`,
# which ships native wheels — a host-platform wheel fails on Lambda with
# "invalid ELF header". Force pip to fetch wheels for the Lambda target instead.
# (Pure-Python deps like PyJWT match the `any` wheel and are unaffected.)
_LAMBDA_PIP_PLATFORM_ARGS: list[str] = [
    "--platform",
    "manylinux2014_x86_64",
    "--implementation",
    "cp",
    "--python-version",
    "3.13",
    "--only-binary=:all:",
]


@jsii.implements(cdk.ILocalBundling)
class _LocalPipBundler:
    """Bundle a Lambda asset locally (no Docker): pip-install the runtime deps into
    the output dir, then copy the named source packages next to them. Falls back to
    the Docker image (returns False) when pip isn't available."""

    def __init__(self, packages: tuple[str, ...]):
        self._packages = packages

    def try_bundle(self, output_dir: str, options) -> bool:  # noqa: ARG002
        pip = shutil.which("pip3") or shutil.which("pip")
        if pip is None:
            return False
        try:
            subprocess.run(
                [
                    pip,
                    "install",
                    *_PIP_REQUIREMENTS,
                    *_LAMBDA_PIP_PLATFORM_ARGS,
                    "-t",
                    output_dir,
                    "--quiet",
                ],
                check=True,
            )
            for pkg in self._packages:
                shutil.copytree(
                    _ROOT / pkg,
                    Path(output_dir) / pkg,
                    dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__"),
                )
        except (subprocess.CalledProcessError, OSError):
            return False
        return True


def pip_bundled_code(*packages: str) -> lambda_.Code:
    """A Lambda Code asset that pip-installs the runtime deps (PyJWT) AND copies the
    given source packages (e.g. "agate", "infra", "chokepoint"). Used by any handler
    that imports agate.jwt_verify, so real JWT verification has its dependency at
    runtime. Docker-free locally; Docker-image fallback in CI without pip."""
    bundle_cmd = (
        "set -e; pip install "
        + " ".join(f"'{r}'" for r in _PIP_REQUIREMENTS)
        + " -t /asset-output >/dev/null; "
        + "; ".join(f"cp -r {p} /asset-output/" for p in packages)
    )
    return lambda_.Code.from_asset(
        ".",
        exclude=LAMBDA_ASSET_EXCLUDES,
        bundling=cdk.BundlingOptions(
            image=lambda_.Runtime.PYTHON_3_13.bundling_image,
            command=["bash", "-c", bundle_cmd],
            local=_LocalPipBundler(packages),
        ),
    )


def oidc_env_from_context(node) -> dict[str, str]:  # noqa: ANN001 — a constructs.Node
    """The OIDC verifier env (`AGATE_OIDC_*`) for any handler that calls
    `agate.jwt_verify.verify_token`, derived from the `cognito_discovery_url` + `cognito_audience`
    deploy context. `verify_token` needs the **JWKS URL** (not just the discovery URL) and the
    **issuer** — so this derives both from the Cognito discovery URL
    (`{issuer}/.well-known/openid-configuration`): issuer = the discovery URL minus that suffix,
    jwks = `{issuer}/.well-known/jwks.json`. Wiring only `AGATE_OIDC_ISSUER` (the bug this fixes)
    left `jwks_url` empty, so `verify_token` failed closed and every request 403'd.

    Returns all three keys empty when no discovery URL is configured (the handler then fails
    closed by design). Centralised so the drafting/deploy/authoring/rooms/memory endpoints can't
    drift from the broker's (identity stack) OIDC wiring."""
    discovery = (node.try_get_context("cognito_discovery_url") or "").strip()
    audience = node.try_get_context("cognito_audience") or ""
    issuer = discovery.removesuffix("/.well-known/openid-configuration") if discovery else ""
    jwks = f"{issuer}/.well-known/jwks.json" if issuer else ""
    return {
        "AGATE_OIDC_ISSUER": issuer,
        "AGATE_OIDC_JWKS_URL": jwks,
        "AGATE_OIDC_AUDIENCE": audience,
    }


def function_url_cors(node) -> lambda_.FunctionUrlCorsOptions:  # noqa: ANN001 — a constructs.Node
    """CORS for a Lambda Function URL the SPA calls cross-origin (the drafting/authoring/deploy/
    rooms endpoints). The SPA at the CloudFront origin POSTs a SigV4-signed request, so the
    browser preflight must be allowed AND the SigV4 headers echoed — without this the Function
    URL sends no `Access-Control-Allow-Origin` and the browser blocks it ("Failed to fetch").

    `allowed_origins` is the deployed `site_url` context when set (pin to the SPA origin), else
    `*` (demo default — mirrors the broker/retrieval HTTP APIs). The allowed headers are the
    SigV4 set the AWS SDK sends (`authorization` / `x-amz-date` / `x-amz-content-sha256` /
    `x-amz-security-token`) plus `content-type`."""
    site_url = (node.try_get_context("site_url") or "").strip().rstrip("/")
    origins = [site_url] if site_url else ["*"]
    return lambda_.FunctionUrlCorsOptions(
        allowed_origins=origins,
        allowed_methods=[lambda_.HttpMethod.POST],
        allowed_headers=[
            "content-type",
            "authorization",
            "x-amz-date",
            "x-amz-content-sha256",
            "x-amz-security-token",
        ],
    )
