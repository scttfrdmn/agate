"""Connectors — the bounded ingestion-target core (#133, vision §8.6).

A **connector** is the DATA plane (split from #113's tool/action plane): a standing
integration to a content system (Google Drive / Box / MS Teams / Discord / S3 / NFS) whose
content flows INTO agate as ingestion under the tenant/scope corpus + per-tenant vector index.
A connector answers *"what can the agent ground in"* — a noun/data concern — distinct from an
MCP tool, which is a verb/action the agent invokes.

The load-bearing property (the whole reason this is the data plane): a connector adds **NO new
access boundary**. Ingested content lands under `{tenant}/{scope}/` in the docs bucket and the
`agate-{tenant}` index, fenced EXACTLY like an uploaded document by the #80 data-scope IAM
policy (which Denies reads outside `{tenant}/{scope}/`) and the #84 retrieval proxy's scope
filter — both already proven against live IAM / S3 Vectors. So the connector's only
security-critical job is to choose a destination KEY that is provably confined to its
connecting user's subtree; the existing fence does the rest.

This module is PURE and AWS-free:
  * the connector source registry (WHICH sources exist + HOW each authenticates), and
  * `connector_dest_key` — build a destination S3 key that CANNOT escape `{tenant}/{scope}/`
    for any adversarial source item path, with `confine_dest_key` as the round-trip proof.

A `user-oauth` connector reads at the SOURCE as the verified user (the source ACL composes
with agate's scope = defense in depth), but even a mis-scoped fetch can't widen agate-side:
the dest key is confined here, and the #80 IAM Deny confines the read. The live OAuth vending,
AgentCore Gateway targets, per-source fetchers, and sync/refresh (a #115 trigger) are deferred
to the #136 deploy follow-up.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from agate.budget import _clean_id, normalise_scope
from agate.delegate import _contains
from agate.rag import scope_path_from_s3_key, tenant_from_s3_key

# The content systems agate can connect. Nouns/data — kept separate from the tool catalog.
ConnectorKind = Literal["gdrive", "box", "teams", "discord", "s3", "nfs"]
# How a connector authenticates to read its source:
#   user-oauth   — reads ONLY what the verified user can (the source ACL composes with agate's
#                  scope; defense in depth). Drive/Box/Teams/Discord, via AgentCore Gateway.
#   scoped-role  — direct via the existing #80 tenant/scope role (S3; no Gateway).
#   ingest-lambda— not a managed Gateway target; wrapped in an ingestion Lambda (NFS).
AuthMode = Literal["user-oauth", "scoped-role", "ingest-lambda"]

# The infix that namespaces connector-ingested content under a scope, so it is visually
# distinct from user uploads and auditable. A real scope path segment can't collide: the
# leading `_` is stripped by neither normaliser, but no course/scope id starts with `_`.
_CONNECTOR_INFIX = "_connectors"


class ConnectorError(ValueError):
    """An unknown connector, or a dest key that would escape its tenant/scope. Fail closed."""


@dataclass(frozen=True, slots=True)
class Connector:
    """A registered content source. `gateway_target` marks the ones reached via an AgentCore
    Gateway OpenAPI target (the live wiring is deferred to #136); pure metadata here."""

    kind: ConnectorKind
    title: str
    auth_mode: AuthMode
    gateway_target: bool = False


_CONNECTORS: dict[str, Connector] = {}


def register_connector(c: Connector) -> Connector:
    if c.kind in _CONNECTORS:
        raise ConnectorError(f"duplicate connector: {c.kind!r}")
    _CONNECTORS[c.kind] = c
    return c


def get_connector(kind: str) -> Connector:
    try:
        return _CONNECTORS[kind]
    except KeyError as exc:
        raise ConnectorError(f"unknown connector: {kind!r}") from exc


def all_connectors() -> tuple[Connector, ...]:
    """Every registered connector (stable order: registration order)."""
    return tuple(_CONNECTORS.values())


# The six sources from #133. user-oauth sources reach via AgentCore Gateway (→ #136).
register_connector(Connector("gdrive", "Google Drive", "user-oauth", gateway_target=True))
register_connector(Connector("box", "Box", "user-oauth", gateway_target=True))
register_connector(Connector("teams", "Microsoft Teams (Graph)", "user-oauth", gateway_target=True))
register_connector(Connector("discord", "Discord", "user-oauth", gateway_target=True))
register_connector(Connector("s3", "Amazon S3 (scoped role)", "scoped-role"))
register_connector(Connector("nfs", "NFS / file share (ingest Lambda)", "ingest-lambda"))


def _safe_item_segments(item_path: str) -> list[str]:
    """Sanitise a SOURCE-supplied item path into safe single key segments.

    A source names its items (a Drive filename, a Box path); that string is UNTRUSTED. Split
    on `/`, drop empty/`.`/`..` segments (no traversal), and `_clean_id` each remaining
    segment — which strips `/` and anything outside `[A-Za-z0-9._-]`, so a segment can never
    re-introduce a separator or walk up. The result is a list of safe, single-level segments.
    A path that sanitises to nothing yields `["item"]` so the key always has a leaf.
    """
    segs: list[str] = []
    for raw in str(item_path or "").split("/"):
        if raw in ("", ".", ".."):
            continue
        cleaned = _clean_id(raw)
        if cleaned and cleaned not in (".", ".."):
            segs.append(cleaned)
    return segs or ["item"]


def connector_dest_key(*, tenant: str, scope: str, connector: str, item_path: str) -> str:
    """Build the destination S3 key for one connector-ingested item, PROVABLY confined to
    `{tenant}/{scope}/`. The existing ingest pipeline + #80 IAM fence + #84 scope filter then
    govern it exactly like an uploaded document — no new boundary.

    Layout: `{tenant}/{scope}/_connectors/{connector}/{safe item path}` (the `scope` segment
    is omitted when empty → tenant-wide, still inside the tenant fence). Every component is
    sanitised with the SAME normalisers the #80 tags use (`_clean_id` for single segments,
    `normalise_scope` for the scope path), so no adversarial `scope`/`item_path` can escape:
    `normalise_scope` rejects `.`/`..` traversal, and `_safe_item_segments` strips separators
    from item segments. `confine_dest_key` is the round-trip proof.
    """
    t = _clean_id(tenant)
    if not t:
        raise ConnectorError("connector_dest_key needs a non-empty tenant")
    conn = get_connector(connector).kind  # validates the connector; fail closed on unknown
    sc = normalise_scope(scope)  # "" if empty or traversal — tenant-wide fallback
    item = "/".join(_safe_item_segments(item_path))
    parts = [t]
    if sc:
        parts.append(sc)
    parts += [_CONNECTOR_INFIX, conn, item]
    return "/".join(parts)


def confine_dest_key(tenant: str, scope: str, key: str) -> bool:
    """The confinement proof: does `key` parse back to the connector's OWN tenant AND a scope
    path within (ancestor-or-self of) the connector's scope subtree? Reuses the SAME parsers
    the ingest pipeline (#84) keys off — `tenant_from_s3_key` / `scope_path_from_s3_key` — and
    `delegate._contains` for subtree containment, so this asserts exactly what the live IAM
    Deny + retrieval filter will enforce. Used by the tests to hammer adversarial inputs.
    """
    try:
        key_tenant = tenant_from_s3_key(key)
    except ValueError:
        return False
    if key_tenant != _clean_id(tenant):
        return False
    want_scope = normalise_scope(scope)
    key_scope = scope_path_from_s3_key(key) or ""
    if not want_scope:
        # tenant-wide connector: any in-tenant scope is fine (still inside the tenant fence).
        return True
    # the ingested key's scope path must sit within the connector's authorized subtree.
    return _contains(want_scope, key_scope)
