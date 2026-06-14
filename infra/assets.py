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
                [pip, "install", *_PIP_REQUIREMENTS, "-t", output_dir, "--quiet"],
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
