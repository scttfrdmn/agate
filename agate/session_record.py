"""Saved sessions — first-class, scope-tagged objects (#109, vision §3).

A session becomes a persisted object: the rendered transcript PLUS the
**server-authoritative** receipt (every model call's cost, citations, tool uses, and the
scope it ran under). It can be resumed, forked, or replayed, and it is the honest **audit
record** — "prove what happened, under whose authority" — because the receipt is computed
server-side (#79 spend, server-emitted events), never asserted by the client.

The load-bearing simplification: a saved session is **just another scope-tagged S3
object**, stored at `{tenant}/{scope}/_sessions/{id}.json`. So its access control is
ALREADY the #80 `data_scope_policy` GetObject fence — a session resumes only if the
resumer's credential authorizes that scope; cross-scope / cross-tenant is denied, already
proven. There is NO new IAM here: the key's prefix IS the boundary.

This module is PURE (no boto3): it builds/serialises the record and derives the
scope-confined S3 key. The persist/resume Lambda + SPA UI are deferred wiring (like #110
deferred the AgentCore Memory resource). The audit-bearing `receipt` is passed in by the
SERVER caller (the meter's authoritative numbers); this record never trusts a client total.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Literal

from agate.budget import _clean_id, normalise_scope
from agate.delegate import subject_key

# Where saved sessions live within a tenant/scope subtree. The `_sessions/` segment keeps
# them out of the document namespace while staying UNDER the {tenant}/{scope}/ prefix the
# #80 data-scope policy fences (so resume inherits that fence).
_SESSIONS_SEGMENT = "_sessions"

ReceiptKind = Literal["llm", "compute", "retrieval"]


class SessionRecordError(ValueError):
    """A saved session that cannot be built/parsed safely (bad scope, total mismatch)."""


@dataclass(frozen=True, slots=True)
class ReceiptRow:
    """One server-computed line of the audit receipt (mirrors the SPA ReceiptRow)."""

    label: str
    kind: ReceiptKind
    cost: float


@dataclass(frozen=True, slots=True)
class Receipt:
    """The authoritative receipt closing a run — server-sourced (#79), never client-claimed.
    `total` is validated against the rows on EVERY construction (`__post_init__`), so a
    forged/mismatched total can't slip in by ANY path — not just the factories below."""

    rows: tuple[ReceiptRow, ...]
    total: float

    def __post_init__(self) -> None:
        # Round both sides to the meter's 6dp before comparing — float repr (0.1+0.2) must
        # not wrongly reject a legitimate receipt, and a forged total still can't pass.
        if round(self.total, 6) != self.rows_total():
            raise SessionRecordError(
                f"receipt total {self.total} != sum of rows {self.rows_total()}"
            )

    def rows_total(self) -> float:
        return round(sum(r.cost for r in self.rows), 6)


@dataclass(frozen=True, slots=True)
class SavedSession:
    """A persisted, scope-tagged session. `transcript` is display text; `receipt` is the
    audit-bearing, server-authoritative record. `subject` is the injective provenance key
    of who ran it (from the verified RoleSessionName)."""

    id: str
    tenant: str
    scope: str  # "" == tenant-wide (stored at the tenant root)
    subject: str  # subject_key provenance — who this session belongs to
    created: str  # ISO timestamp, stamped by the server caller (NO CLOCKS in this module)
    mode: str
    transcript: tuple[dict, ...]
    receipt: Receipt
    citations: tuple[dict, ...] = field(default_factory=tuple)

    def to_json(self) -> str:
        return json.dumps(
            {
                "id": self.id,
                "tenant": self.tenant,
                "scope": self.scope,
                "subject": self.subject,
                "created": self.created,
                "mode": self.mode,
                "transcript": list(self.transcript),
                "receipt": {
                    "rows": [
                        {"label": r.label, "kind": r.kind, "cost": r.cost}
                        for r in self.receipt.rows
                    ],
                    "total": self.receipt.total,
                },
                "citations": list(self.citations),
            },
            indent=2,
            sort_keys=True,
        )


def _receipt_from_dict(d: dict) -> Receipt:
    rows = tuple(
        ReceiptRow(label=str(r["label"]), kind=r["kind"], cost=float(r["cost"]))
        for r in d.get("rows", [])
    )
    # Receipt.__post_init__ re-validates total == sum(rows) — a tampered total raises.
    return Receipt(rows=rows, total=float(d.get("total", 0.0)))


def from_json(text: str) -> SavedSession:
    """Parse a saved-session JSON object, re-validating the receipt total against its
    rows (a tampered total is rejected — the audit record stays self-consistent)."""
    d = json.loads(text)
    return SavedSession(
        id=str(d["id"]),
        tenant=str(d["tenant"]),
        scope=str(d.get("scope", "")),
        subject=str(d.get("subject", "")),
        created=str(d.get("created", "")),
        mode=str(d.get("mode", "")),
        transcript=tuple(d.get("transcript", [])),
        receipt=_receipt_from_dict(d.get("receipt", {})),
        citations=tuple(d.get("citations", [])),
    )


def build_saved_session(
    *,
    session_id: str,
    tenant: str,
    scope: str,
    subject: str,
    created: str,
    mode: str,
    transcript: list[dict],
    receipt: Receipt,
    citations: list[dict] | None = None,
) -> SavedSession:
    """Assemble a SavedSession from SERVER-provided pieces. `receipt` is the meter's
    authoritative receipt (#79); a `Receipt` self-validates (total == sum of rows) on
    construction, so a client-forged number can't become the audit record. `subject` is
    the verified subject, stored as an injective `subject_key` provenance segment."""
    return SavedSession(
        id=str(session_id),
        tenant=tenant,
        scope=scope,
        subject=subject_key(subject),
        created=created,
        mode=mode,
        transcript=tuple(transcript),
        receipt=receipt,
        citations=tuple(citations or ()),
    )


def session_object_key(tenant: str, scope: str, session_id: str) -> str:
    """The S3 key for a saved session: `{tenant}/{scope}/_sessions/{id}.json`, or
    `{tenant}/_sessions/{id}.json` when unscoped (tenant root).

    Every segment is sanitised to the key grammar so a crafted scope/id can't inject `/`
    levels or `..` that escape the `{tenant}/{scope}/` prefix the #80 data-scope policy
    fences — the prefix IS the access boundary. A scope that garbles to empty is treated
    as unscoped (tenant root), never silently widened past the tenant."""
    t = _clean_id(tenant)
    if not t:
        raise SessionRecordError("tenant is required for a session key")
    sid = _clean_id(session_id)
    if not sid:
        raise SessionRecordError("session_id did not normalise to a valid id")
    norm_scope = normalise_scope(scope) if scope else ""
    prefix = f"{t}/{norm_scope}" if norm_scope else t
    return f"{prefix}/{_SESSIONS_SEGMENT}/{sid}.json"
