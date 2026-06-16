"""Slurm HPC tool — the pure scope→allocation + budget-gate core (#136 / #114, §5).

The flagship "agent that acts": an agent with the `hpc-submit` capability (#114) submits a
job to the institution's Slurm cluster. The security model is the §5 split made concrete —
IAM (#113) fences WHICH agents may invoke the HPC gateway tool; this module enforces the
EFFECT: the caller's verified `agate:scope` (a lab/project) maps to exactly ONE Slurm
account/allocation (never a sibling lab's), and a `submit` (a WRITE) is gated on the budget
cascade (#81) before it ever reaches the scheduler — an over-allocation submit is rejected
pre-call, naming the breaching node.

This module is PURE and AWS-free: `slurm_account_for_scope` is a deterministic mapping and
`gate_submit` reuses `cost.precall.evaluate_cascade`. The handler
(`infra/functions/slurm/handler.py`) supplies the verified tags + the live spend/budget rows
and injects the actual scheduler transport (`_submit_job`), which is the deferred,
institution-wired boundary (no live cluster in agate itself). Identity/scope come ONLY from
the verified credential the Gateway passes — never the tool payload — exactly as the #84
retrieval proxy derives scope from the verified token.
"""

from __future__ import annotations

from dataclasses import dataclass

from cost.precall import CascadeResult, evaluate_cascade

from agate.budget import _clean_id, normalise_scope
from agate.rag import ancestors


class SlurmError(ValueError):
    """A scope that maps to no allocation, or a malformed submit. Fail closed: refuse to
    submit rather than guess an account."""


def slurm_account_for_scope(tenant: str, scope: str) -> str:
    """Map a verified `(tenant, scope)` to its Slurm account/allocation id — deterministic,
    injective, and CONFINED: `chem` + `lab/photonics` → `chem-lab_photonics`, never a sibling
    lab's account. The scope is normalised with the SAME grammar the #80 tags use
    (`normalise_scope` rejects `.`/`..` traversal), and `/` becomes `_` so the account id is a
    single Slurm-safe token. An empty scope after normalise is a tenant-wide allocation
    (`{tenant}-default`). The institution maps these ids to real allocations at deploy; this
    guarantees two distinct scopes never collide onto one account.

    SECURITY: `tenant`/`scope` MUST come from the verified credential (the Gateway-passed
    session tags), never the tool payload — so an agent can't name another lab's account.
    """
    t = _clean_id(tenant)
    if not t:
        raise SlurmError("slurm account needs a non-empty tenant")
    node = normalise_scope(scope)
    if not node:
        return f"{t}-default"
    # `/` -> `_` for a single Slurm-safe token; the scope grammar already excludes other
    # separators, so the result is injective per (tenant, scope).
    return f"{t}-{node.replace('/', '_')}"


@dataclass(frozen=True, slots=True)
class SubmitDecision:
    """The outcome of gating an `hpc-submit` before it reaches the scheduler."""

    allowed: bool
    account: str
    cascade: CascadeResult
    reason: str


def submit_cascade_nodes(
    tenant: str, scope: str, spend_lookup
) -> list[tuple[str, float, float | None]]:
    """The `evaluate_cascade` node-list for a submit: one `(label, spend, budget)` row per
    scope ancestor (broad→specific), so the submit must fit under EVERY allocation node above
    the caller's leaf — the same hierarchical rule the chat path uses (#81/#112).
    `spend_lookup(label) -> (spend, budget|None)` is injected (the handler reads the live
    spend/budget tables; tests fake it). An unscoped caller yields the tenant node only.
    """
    node = normalise_scope(scope)
    labels = ancestors(node) if node else [_clean_id(tenant)]
    rows: list[tuple[str, float, float | None]] = []
    for label in labels:
        spend, budget = spend_lookup(label)
        rows.append((label, spend, budget))
    return rows


def gate_submit(
    *,
    tenant: str,
    scope: str,
    model_id: str,
    input_tokens: int,
    max_tokens: int,
    spend_lookup,
) -> SubmitDecision:
    """Gate an `hpc-submit` on the budget cascade (#81) BEFORE the scheduler is touched. The
    submit's worst-case cost is checked against every allocation node above the caller; the
    first node to reject short-circuits and is named. Returns the resolved account + the
    decision so the handler submits only on `allowed`, then records the debit.

    The account is resolved from the VERIFIED `(tenant, scope)` — so even an allowed submit
    can only land on the caller's own allocation."""
    account = slurm_account_for_scope(tenant, scope)
    nodes = submit_cascade_nodes(tenant, scope, spend_lookup)
    result = evaluate_cascade(
        model_id=model_id, input_tokens=input_tokens, max_tokens=max_tokens, nodes=nodes
    )
    return SubmitDecision(
        allowed=result.decision == "allow",
        account=account,
        cascade=result,
        reason=result.reason,
    )
