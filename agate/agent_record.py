"""Saved agents — a created agent is a scope-tagged S3 object (#118 deploy-on-confirm).

When a user confirms a drafted+clamped agent (the #118/#118b/#118c flow), agate *creates*
it — and "create" means PERSIST THE GOVERNED SPEC, not vend a standing credential. Per §0.1
agate **governs** agents (compiles the scoped credential + records the spec); agenkit (or the
runtime) **runs** them, re-instantiating the spec per invoker (#107) under the credential the
compiler derives. So a created agent is just its validated spec + provenance, stored as a
scope-tagged object the runtime can later fetch.

The load-bearing simplification mirrors SavedSession (#109): the record lives at
`{tenant}/{scope}/_agents/{name}.json`, so its access control IS the `{tenant}/{scope}/`
prefix fence — a created agent is readable/writable only by a credential that authorizes
that scope; cross-scope / cross-tenant is denied by the same boundary as documents (#80) and
sessions (#109). The KEY's prefix is the boundary.

The spec is stored as the validated DRAFT DICT (the JSON `agentspec.parse_spec` accepts), so
reloading is `parse_spec(record["spec"])` — no bespoke serializer, and the stored form is the
auditable source. PROVENANCE the server supplies (never the client): the verified `created_by`
(`<tenant>@<subject>`), the `agent_id` (#137 stable WHO), and the `spec_version` digest (WHICH
version acted). The clamped `boundary` summary is stored as the human-legible record of what
the agent may do — the same lines the user confirmed.

This module is PURE (no boto3): it builds/serialises the record + derives the scope-confined
key. The deploy Lambda (which assumes a tenant-fenced role and PUTs the object) is the AWS
edge. The record never trusts a client-claimed scope/subject — the deploy endpoint re-derives
them from the verified token + the re-clamped spec before calling `build_agent_record`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from agate.budget import _clean_id, normalise_scope

# Where created agents live within a tenant/scope subtree. The `_agents/` segment keeps them
# out of the document namespace while staying UNDER the {tenant}/{scope}/ prefix the #80
# data-scope policy fences (so fetch/update inherit that fence) — the SavedSession pattern.
_AGENTS_SEGMENT = "_agents"


class AgentRecordError(ValueError):
    """A created-agent record that cannot be built/parsed safely (bad scope/name)."""


@dataclass(frozen=True, slots=True)
class SavedAgent:
    """A persisted, scope-tagged created agent. `spec` is the validated draft dict (reload via
    `agentspec.parse_spec`); `boundary` is the human-legible clamped plan the user confirmed;
    the rest is server-supplied provenance (#137) — never client-claimed."""

    name: str
    tenant: str
    scope: str  # "" == tenant-wide (stored at the tenant root)
    agent_id: str  # the stable WHO: `{tenant}/{name}` (identity.agent_id)
    spec_version: str  # content digest of the spec's identity-bearing fields
    created_by: str  # the verified `<tenant>@<subject>` who created it (NOT client-claimed)
    created: str  # ISO timestamp, stamped by the server caller (NO CLOCKS in this module)
    spec: dict  # the validated draft dict — parse_spec(spec) reloads the AgentSpec
    boundary: tuple[str, ...] = field(default_factory=tuple)  # the confirmed plan lines

    def to_json(self) -> str:
        return json.dumps(
            {
                "name": self.name,
                "tenant": self.tenant,
                "scope": self.scope,
                "agent_id": self.agent_id,
                "spec_version": self.spec_version,
                "created_by": self.created_by,
                "created": self.created,
                "spec": self.spec,
                "boundary": list(self.boundary),
            },
            indent=2,
            sort_keys=True,
        )


def from_json(text: str) -> SavedAgent:
    """Parse a saved-agent JSON object. The stored `spec` is re-parsed by the caller via
    `agentspec.parse_spec` before use — this just rehydrates the record envelope."""
    d = json.loads(text)
    spec = d.get("spec")
    if not isinstance(spec, dict):
        raise AgentRecordError("saved agent has no spec object")
    return SavedAgent(
        name=str(d["name"]),
        tenant=str(d["tenant"]),
        scope=str(d.get("scope", "")),
        agent_id=str(d.get("agent_id", "")),
        spec_version=str(d.get("spec_version", "")),
        created_by=str(d.get("created_by", "")),
        created=str(d.get("created", "")),
        spec=spec,
        boundary=tuple(str(b) for b in d.get("boundary", [])),
    )


def build_agent_record(
    *,
    name: str,
    tenant: str,
    scope: str,
    agent_id: str,
    spec_version: str,
    created_by: str,
    created: str,
    spec: dict,
    boundary: list[str] | None = None,
) -> SavedAgent:
    """Assemble a SavedAgent from SERVER-provided pieces. The caller (the deploy endpoint)
    supplies the VERIFIED tenant/scope (from the re-clamped credential, never the client) and
    the `created_by` from the verified RoleSessionName. `spec` is the validated draft dict the
    server re-ran through `dispose_draft` — so what is persisted is exactly what was clamped."""
    if not isinstance(spec, dict) or not spec:
        raise AgentRecordError("a created agent needs a non-empty spec object")
    return SavedAgent(
        name=name,
        tenant=tenant,
        scope=scope,
        agent_id=agent_id,
        spec_version=spec_version,
        created_by=created_by,
        created=created,
        spec=spec,
        boundary=tuple(boundary or ()),
    )


def agent_object_key(tenant: str, scope: str, agent_name: str) -> str:
    """The S3 key for a created agent: `{tenant}/{scope}/_agents/{name}.json`, or
    `{tenant}/_agents/{name}.json` when unscoped (tenant root).

    Every segment is sanitised to the key grammar so a crafted scope/name can't inject `/`
    levels or `..` that escape the `{tenant}/{scope}/` prefix the #80 data-scope policy fences
    — the prefix IS the access boundary (mirrors `session_object_key`). A scope that garbles to
    empty is treated as unscoped (tenant root), never silently widened past the tenant."""
    t = _clean_id(tenant)
    if not t:
        raise AgentRecordError("tenant is required for an agent key")
    n = _clean_id(agent_name)
    if not n:
        raise AgentRecordError("agent_name did not normalise to a valid name")
    norm_scope = normalise_scope(scope) if scope else ""
    prefix = f"{t}/{norm_scope}" if norm_scope else t
    return f"{prefix}/{_AGENTS_SEGMENT}/{n}.json"
