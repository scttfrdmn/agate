"""Generated IAM/ABAC policy documents — the enforcement side of the tag scheme.

These functions turn the pure entitlement table (agate.entitlements) and the tag
scheme (agate.names) into IAM policy JSON. The CDK identity stack consumes them so
that "tier -> entitled models" has exactly ONE source of truth (design §13.2),
never inline branches.
"""
