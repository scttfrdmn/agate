"""Pure analytics aggregation for the governed-access console (Track 1, #63).

The admin Lambda scans the authoritative spend table and hands the raw rows here;
this module turns them into the rollups the console renders. Pure and AWS-free
(no boto3, no clock) so it's fully unit-tested — the Lambda is a thin I/O shim.

Spend rows are written by the spend meter (meter/parse.py) keyed
`{tenant}#{user}#{period}` with a `spend_usd` amount; tenant-rollup rows are keyed
`{tenant}#{period}`. We reconstruct the breakdown from the per-user rows so the
console never trusts a precomputed total it can't re-derive.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class SpendRow:
    """One per-user spend row from the table (the unit the meter writes)."""

    tenant: str
    user: str
    period: str  # YYYY-MM
    spend_usd: float


@dataclass(slots=True)
class TenantUsage:
    """Per-tenant rollup for a period: total + per-user breakdown."""

    tenant: str
    period: str
    total_usd: float = 0.0
    users: dict[str, float] = field(default_factory=dict)

    @property
    def user_count(self) -> int:
        return len(self.users)


def parse_spend_key(pk: str) -> SpendRow | None:
    """Parse a per-user spend key `{tenant}#{user}#{period}`. Returns None for a
    tenant-rollup key (`{tenant}#{period}`, two parts) or a malformed key, so the
    caller can scan a mixed table and keep only the authoritative per-user rows."""
    parts = pk.split("#")
    if len(parts) != 3:
        return None
    tenant, user, period = parts
    if not (tenant and user and period):
        return None
    return SpendRow(tenant=tenant, user=user, period=period, spend_usd=0.0)


def rows_from_items(items: list[dict]) -> list[SpendRow]:
    """Turn raw DynamoDB items ({"pk", "spend_usd"}) into per-user SpendRows.

    Skips tenant-rollup rows and anything malformed — the console derives tenant
    totals by summing the per-user rows, not by trusting a stored rollup.
    """
    rows: list[SpendRow] = []
    for item in items:
        parsed = parse_spend_key(str(item.get("pk", "")))
        if parsed is None:
            continue
        try:
            amount = float(item.get("spend_usd", 0.0))
        except (TypeError, ValueError):
            amount = 0.0
        rows.append(
            SpendRow(tenant=parsed.tenant, user=parsed.user, period=parsed.period, spend_usd=amount)
        )
    return rows


def rollup_by_tenant(rows: list[SpendRow], *, period: str | None = None) -> list[TenantUsage]:
    """Aggregate per-user rows into per-(tenant, period) usage, sorted by spend desc.

    `period` filters to one month when given; otherwise every period present is kept
    as its own rollup. Per-user breakdown sums duplicate (tenant,user,period) rows.
    """
    acc: dict[tuple[str, str], TenantUsage] = {}
    for r in rows:
        if period is not None and r.period != period:
            continue
        key = (r.tenant, r.period)
        usage = acc.get(key)
        if usage is None:
            usage = TenantUsage(tenant=r.tenant, period=r.period)
            acc[key] = usage
        usage.users[r.user] = round(usage.users.get(r.user, 0.0) + r.spend_usd, 6)
        usage.total_usd = round(usage.total_usd + r.spend_usd, 6)
    return sorted(acc.values(), key=lambda u: u.total_usd, reverse=True)


def top_users(
    rows: list[SpendRow], *, period: str | None = None, limit: int = 10
) -> list[tuple[str, float]]:
    """The highest-spend (user) pairs across all tenants for a period — the
    'who is driving cost' view. Returns [(\"tenant/user\", usd), ...] desc."""
    totals: dict[str, float] = defaultdict(float)
    for r in rows:
        if period is not None and r.period != period:
            continue
        totals[f"{r.tenant}/{r.user}"] += r.spend_usd
    ranked = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)
    return [(k, round(v, 6)) for k, v in ranked[:limit]]


def to_console_payload(
    items: list[dict], *, period: str | None = None, only_tenant: str | None = None
) -> dict:
    """The full analytics payload the admin SPA renders: per-tenant rollups + the
    top-spenders list + a grand total. Built from raw table items in one call.

    `only_tenant` restricts the whole payload to one tenant — used for a SCOPED admin
    (a dean/chair governs their own tenant, not the whole institution). A tenant-wide
    /global admin passes None and sees every tenant.
    """
    rows = rows_from_items(items)
    if only_tenant is not None:
        rows = [r for r in rows if r.tenant == only_tenant]
    tenants = rollup_by_tenant(rows, period=period)
    return {
        "period": period,
        "grand_total_usd": round(sum(t.total_usd for t in tenants), 6),
        "tenant_count": len(tenants),
        "tenants": [
            {
                "tenant": t.tenant,
                "period": t.period,
                "total_usd": t.total_usd,
                "user_count": t.user_count,
                "users": [
                    {"user": u, "spend_usd": s}
                    for u, s in sorted(t.users.items(), key=lambda kv: kv[1], reverse=True)
                ],
            }
            for t in tenants
        ],
        # rows is already tenant-filtered above, so top_users respects the scope too.
        "top_users": [{"id": k, "spend_usd": v} for k, v in top_users(rows, period=period)],
    }
