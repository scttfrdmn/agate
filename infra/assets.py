"""Shared Lambda-asset packaging config.

Several stacks bundle the repo root as a Lambda asset (the broker, ingest, and LTI
functions all ship the pure `agg`/`policy` packages plus their handler). They MUST
exclude build/cache output — most importantly `cdk.out`, which lives at the repo
root and, if included, recursively copies the asset into itself until the path
length blows the filesystem limit. Keep the exclude list here so all three stacks
share one correct definition.
"""

from __future__ import annotations

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
