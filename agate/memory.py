"""Cross-session memory namespaces — 3-tier, ABAC-namespaced (#110, vision §3).

Memory is where chatbots get privacy *dangerously* wrong (one global blob). agate gets
it right by construction: a memory record lives under a NAMESPACE derived from the
session's verified `agate:` tags, and AgentCore Memory namespaces are IAM-enforceable
(the `bedrock-agentcore:namespacePath` condition key) — so memory is fenced by the SAME
credential model as documents (#80) and vectors (#84). It can never leak across tenant,
principal, or scope (invariant §10.3).

Three tiers, each a namespace template (AgentCore's hierarchical `/.../`-with-trailing-
slash form; the trailing slash prevents prefix collisions in multi-tenant use):
  * session  — one conversation:        `agate/{tenant}/personal/{subject}/session/{sid}/`
  * personal — one principal, all sessions: `agate/{tenant}/personal/{subject}/`
  * shared   — a scope node's members:   `agate/{tenant}/shared/{scope}/`  (None if unscoped)

This module is PURE (no boto3): it derives the namespace strings from VERIFIED tags +
subject (never a request field) and `policy.generate.memory_access_policy` emits the IAM
that fences them. A live proof (`tests/test_proof_memory.py`) shows a principal reads only
its own. The deferred read/write path (a follow-up) MUST use `namespaces_for` — it never
takes a namespace from the client.

Boundary split (flagged, like #84): tenant + scope are IAM-enforced (principal-tag
interpolation in the namespacePath); the per-principal `subject` segment is server-derived
from the verified RoleSessionName (subject isn't an STS principal tag) and made injective
by `delegate.subject_key`, so two principals can never share a personal namespace.
"""

from __future__ import annotations

import re
from typing import Literal

from agate.delegate import subject_key
from agate.tags import SessionTags

MemoryTier = Literal["session", "personal", "shared"]

# Namespace-segment grammar: a single path segment may not contain `/` (which would
# escape its fence) — strip everything else to the id grammar. Tenant/scope are already
# normalised upstream (tags), but sanitise defensively here too.
_SEG_RE = re.compile(r"[^a-zA-Z0-9._-]")

# Root prefix so agate's namespaces never collide with another app's in a shared memory.
_ROOT = "agate"


def _seg(raw: str) -> str:
    """Sanitise one namespace path segment: strip `/` (which would escape the fence) and
    everything outside the id grammar. An all-dots result (`.`/`..` — e.g. from
    `chem/../evil`, where the `/`-strip already flattened it) → "" so no traversal-looking
    segment survives (mirrors the #107 scope hardening; legitimate dots like `a.b` are
    kept). The `/`-strip is what actually prevents fence escape; this is hygiene."""
    cleaned = _SEG_RE.sub("", (raw or "").strip())
    return "" if set(cleaned) <= {"."} else cleaned


def _scope_path(scope: str) -> str:
    """A scope path (`chemistry/chem-101`) sanitised level-by-level, `/` preserved as the
    level separator. Empty levels dropped. The scope is already normalised by
    `tags._normalise_data_scope` (rejects `..`); this is defence in depth."""
    levels = [_seg(level) for level in scope.strip("/").split("/")]
    return "/".join(level for level in levels if level)


def personal_namespace(tags: SessionTags, subject: str) -> str:
    """The per-principal store, across sessions: `agate/{tenant}/personal/{subject}/`.
    `subject` comes from the verified RoleSessionName, made injective by `subject_key`."""
    return f"{_ROOT}/{_seg(tags.tenant)}/personal/{subject_key(subject)}/"


def session_namespace(tags: SessionTags, subject: str, session_id: str) -> str:
    """One conversation, under the principal's personal tree:
    `agate/{tenant}/personal/{subject}/session/{sid}/`."""
    return f"{personal_namespace(tags, subject)}session/{_seg(session_id)}/"


def shared_namespace(tags: SessionTags) -> str | None:
    """A scope node's collective memory: `agate/{tenant}/shared/{scope}/`. Readable by
    every member of that scope, fenced by the SAME `agate:scope` tag as docs/vectors.
    Returns None when the session is unscoped — fail-closed: no shared tier without a
    scope (an unscoped session has no sub-tenant group to share with)."""
    if not tags.scope:
        return None
    return f"{_ROOT}/{_seg(tags.tenant)}/shared/{_scope_path(tags.scope)}/"


def namespaces_for(tags: SessionTags, subject: str, session_id: str) -> dict[MemoryTier, str]:
    """Every memory namespace this session may touch. `shared` is omitted when the
    session is unscoped (fail-closed). The (deferred) read/write path uses ONLY these —
    it must never accept a namespace from the client."""
    out: dict[MemoryTier, str] = {
        "session": session_namespace(tags, subject, session_id),
        "personal": personal_namespace(tags, subject),
    }
    shared = shared_namespace(tags)
    if shared is not None:
        out["shared"] = shared
    return out
