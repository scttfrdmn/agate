"""The one obvious place names live, so a global rename stays cheap (CLAUDE.md).

Everything else imports from here instead of scattering string literals.
"""

# Project / repo / package name and short handle (CLI binary).
NAME = "aws-genai-gateway"
HANDLE = "agate"

# ABAC session-tag namespace. Tag keys are f"{TAG_NAMESPACE}affiliation", etc.
TAG_NAMESPACE = "agate:"

# S3 prefix for tenant documents: s3://{DOCS_BUCKET_PREFIX}-<acct-suffix>/{tenant}/...
DOCS_BUCKET_PREFIX = "agate-docs"


def tag_key(name: str) -> str:
    """Fully-qualified session-tag key, e.g. tag_key("tenant") -> "agate:tenant"."""
    return f"{TAG_NAMESPACE}{name}"
