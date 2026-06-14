"""Pure budget-authoring core for the governed-access console (#87, splits from #81).

The #81 cascade READS budget rows from the `agate-budget` table; nothing in agate
WROTE them (they were seeded by hand). This module is the pure, AWS-free core of the
admin write path: it validates an authenticated admin's budget-set request, decides
whether that admin is ALLOWED to set the requested scope, and computes the exact
DynamoDB primary key to write. The admin Lambda (`infra/functions/admin`) is a thin
I/O shim around it — same split as `agate.admin` (read analytics) ↔ that Lambda.

Two load-bearing rules, enforced here so they can't be bypassed at the I/O layer:

  1. **Key parity with the reader.** The chokepoint reads budgets at three key shapes
     (`meter.scope_pk` / `spend_key` / `spend_rollup_key`). A writer that drifts by one
     character writes a row the cascade never reads — a silently ineffective budget on
     a real-money path. We rebuild the SAME shapes here and a parity test
     (`test_budget.py`) asserts they equal `meter`'s, since `agate` cannot import
     `meter` (meter imports `agate.entitlements` — that would cycle).

  2. **A scoped admin can only set budgets inside their own subtree.** A tenant-wide
     admin (no `admin_scope`) may set any node in their tenant; a scoped admin
     (`admin_scope=("chemistry",)`) may set only `chemistry` and its descendants —
     never a sibling, the tenant root, or another tenant. This mirrors the read path's
     tenant confinement (`handler.only_tenant`) and the #80 S3 containment rule.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

# Scope-path grammar — MUST match agate.tags._SCOPE_RE (segments + `/` separators).
_SCOPE_RE = re.compile(r"[^a-zA-Z0-9._/-]")
# Tenant/user id grammar — matches agate.tags._TENANT_RE (no `/`, no `#`).
_ID_RE = re.compile(r"[^a-zA-Z0-9._-]")
_PERIOD_RE = re.compile(r"^\d{4}-\d{2}$")  # YYYY-MM, the meter's period format


class BudgetError(Exception):
    """Reject a budget-set request (maps to a 4xx). Validation or authorization."""


def normalise_scope(raw: str) -> str:
    """Normalise ONE scope path to the tags grammar, or "" if it reduces to empty.

    Mirrors `agate.tags._normalise_data_scope` for a single value (no multi-value
    fail-open here — the writer takes exactly one explicit node). Strips disallowed
    chars and leading/trailing `/`. A `.`/`..` path segment → "" (no traversal: it
    would write an unreachable junk row the chokepoint never reads)."""
    cleaned = _SCOPE_RE.sub("", (raw or "").strip()).strip("/")
    if not cleaned:
        return ""
    if any(seg in ("", ".", "..") for seg in cleaned.split("/")):
        return ""
    return cleaned


def _clean_id(raw: str) -> str:
    return _ID_RE.sub("", (raw or "").strip())


def is_within_admin_scope(node: str, admin_scope: tuple[str, ...]) -> bool:
    """Whether a scoped admin governing `admin_scope` may write the scope `node`.

    Empty `admin_scope` == tenant-wide admin → any node in the tenant (True). Otherwise
    `node` must equal an admin_scope subtree root OR be a descendant of it. Containment
    is path-segment-wise, so `chem` does NOT contain `chemistry` (string-prefix bug),
    only `chemistry`/`chemistry/...`. An empty `node` (the tenant root) is governable
    ONLY by a tenant-wide admin."""
    if not admin_scope:
        return True
    if not node:
        return False  # tenant root — only a tenant-wide admin may set it
    for root in admin_scope:
        root = root.strip("/")
        if node == root or node.startswith(root + "/"):
            return True
    return False


@dataclass(frozen=True, slots=True)
class BudgetWrite:
    """A validated, authorized budget mutation ready to PUT. `pk` is the exact
    DynamoDB key the chokepoint reads; `budget_usd` the ceiling to store."""

    pk: str
    budget_usd: float
    tenant: str
    period: str
    scope: str = ""  # "" when this is a user/tenant-level (non-scope) budget
    user: str = ""  # "" when this is a tenant or scope budget


# --- key builders (PARITY with meter.parse — see test_budget.py) ----------------
# Duplicated, NOT imported, because agate cannot depend on meter (cycle). The parity
# test is the guard that these never drift from the reader's shapes.


def _scope_pk(tenant: str, node: str, period: str) -> str:
    return f"{tenant}#scope#{node}#{period}"


def _user_pk(tenant: str, user: str, period: str) -> str:
    return f"{tenant}#{user}#{period}"


def _tenant_pk(tenant: str, period: str) -> str:
    return f"{tenant}#{period}"


def plan_budget_write(
    *,
    actor_tenant: str,
    actor_admin_scope: tuple[str, ...],
    tenant: str,
    usd: float,
    period: str,
    scope: str | None = None,
    user: str | None = None,
) -> BudgetWrite:
    """Validate + authorize a budget-set request and return the row to write.

    `actor_tenant`/`actor_admin_scope` come from the VERIFIED admin token (never the
    request body). The request names the target `tenant`, `usd`, `period`, and exactly
    one of `scope` (a scope-node budget) or `user` (a per-user budget); neither → the
    tenant-level budget. Raises BudgetError on bad input or an out-of-scope target.

    Authorization (fail-closed):
      * target tenant MUST equal the admin's own tenant — no cross-tenant writes, ever.
      * a scoped admin may only target a node within their admin_scope subtree.
      * a scoped admin may NOT set the tenant-level or a per-user budget (those span
        the whole tenant, outside their subtree) — only a tenant-wide admin may.
    """
    tenant = _clean_id(tenant)
    if not tenant:
        raise BudgetError("tenant is required")
    if tenant != _clean_id(actor_tenant):
        raise BudgetError("cannot set a budget for another tenant")
    if not _PERIOD_RE.match(period or ""):
        raise BudgetError("period must be YYYY-MM")
    if not isinstance(usd, (int, float)) or isinstance(usd, bool):
        raise BudgetError("usd must be a number")
    # Reject NaN/inf BEFORE the range check: `nan < 0` is False, so a NaN would slip
    # through here AND through the chokepoint's `spend > budget` (always False) —
    # silently DISABLING enforcement on the very row meant to cap spend. Fail closed.
    if not math.isfinite(usd):
        raise BudgetError("usd must be a finite number")
    if usd < 0:
        raise BudgetError("usd must be >= 0")
    if scope and user:
        raise BudgetError("set either scope or user, not both")

    if scope:
        node = normalise_scope(scope)
        if not node:
            raise BudgetError("scope did not normalise to a valid path")
        if not is_within_admin_scope(node, actor_admin_scope):
            raise BudgetError("scope is outside your administrative subtree")
        return BudgetWrite(
            pk=_scope_pk(tenant, node, period),
            budget_usd=float(usd),
            tenant=tenant,
            period=period,
            scope=node,
        )

    # tenant-level or per-user budget — both span beyond any single subtree, so a
    # scoped admin can't author them.
    if actor_admin_scope:
        raise BudgetError("a scoped admin may only set scope-node budgets")

    if user:
        uid = _clean_id(user)
        if not uid:
            raise BudgetError("user did not normalise to a valid id")
        return BudgetWrite(
            pk=_user_pk(tenant, uid, period),
            budget_usd=float(usd),
            tenant=tenant,
            period=period,
            user=uid,
        )

    return BudgetWrite(
        pk=_tenant_pk(tenant, period),
        budget_usd=float(usd),
        tenant=tenant,
        period=period,
    )
