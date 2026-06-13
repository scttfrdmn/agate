"""Pure parsing of a Bedrock invocation-log record into an authoritative spend row.

A Bedrock model-invocation log record carries the authoritative token counts (the
counts the service billed), the model id, the caller identity, and a timestamp.
This module turns one record into a `SpendRecord` — the dollars (via the shared
`cost` engine) keyed by `{tenant}#{user}#{period}` for the spend table (§13.6).

Pure and AWS-free. The tenant/user come from the caller's assumed-role session: the
broker sets RoleSessionName to the federated subject and the session carries the
`agg:tenant` tag, both of which appear in the record's `identity.arn`
(`.../agg-authenticated/<RoleSessionName>`). The period is derived from the
record timestamp (year-month) so the key rolls over automatically.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from cost.pricing import PriceBook, default_pricebook

# assumed-role ARN: arn:aws:sts::<acct>:assumed-role/<role>/<RoleSessionName>
_ASSUMED_ROLE = re.compile(r"assumed-role/(?P<role>[^/]+)/(?P<session>.+)$")


class RecordError(ValueError):
    """The log record is missing fields needed to attribute spend — skip it."""


@dataclass(frozen=True, slots=True)
class SpendRecord:
    """One metered invocation, attributed and priced."""

    tenant: str
    user: str
    period: str  # YYYY-MM
    model_id: str
    input_tokens: int
    output_tokens: int
    cost_usd: float


def _period_from_timestamp(ts: str) -> str:
    """YYYY-MM from an ISO-8601 timestamp; the spend key rolls over by month."""
    # Tolerate trailing 'Z' and sub-second precision; we only need year-month.
    m = re.match(r"(?P<ym>\d{4}-\d{2})", ts or "")
    if not m:
        raise RecordError(f"unparseable timestamp: {ts!r}")
    return m.group("ym")


def _identity(record: dict) -> tuple[str, str]:
    """Derive (user, tenant_hint) from the record's identity ARN.

    The RoleSessionName (set by the broker to the federated subject) is the user.
    The tenant is carried as the `agg:tenant` session tag, surfaced in the record's
    requestMetadata when present; otherwise it must be supplied out-of-band.
    """
    arn = (record.get("identity") or {}).get("arn", "")
    m = _ASSUMED_ROLE.search(arn)
    user = m.group("session") if m else "unknown"
    return user, arn


def parse_invocation_record(
    record: dict,
    *,
    pricebook: PriceBook | None = None,
    tenant: str | None = None,
) -> SpendRecord:
    """Translate one Bedrock invocation-log record into a priced `SpendRecord`.

    `tenant` may be passed explicitly (e.g. from the record's `requestMetadata`
    carrying the `agg:tenant` tag, which the caller extracts); if absent we read
    `record['requestMetadata']['agg:tenant']`, falling back to "unknown". Raises
    RecordError if token counts or timestamp are missing — such a record cannot be
    metered and should be skipped, not guessed.
    """
    pb = pricebook or default_pricebook()

    model_id = record.get("modelId")
    if not model_id:
        raise RecordError("record has no modelId")

    period = _period_from_timestamp(record.get("timestamp", ""))
    user, _arn = _identity(record)

    resolved_tenant = tenant or (record.get("requestMetadata") or {}).get("agg:tenant") or "unknown"

    inp = record.get("input") or {}
    out = record.get("output") or {}
    in_tok = inp.get("inputTokenCount")
    out_tok = out.get("outputTokenCount")
    if in_tok is None and out_tok is None:
        raise RecordError("record has no token counts")
    in_tok = int(in_tok or 0)
    out_tok = int(out_tok or 0)

    rate = pb.llm_rate(_tier_key(model_id))
    cost = (in_tok / 1e6) * rate.input_per_mtok + (out_tok / 1e6) * rate.output_per_mtok

    return SpendRecord(
        tenant=resolved_tenant,
        user=user,
        period=period,
        model_id=model_id,
        input_tokens=in_tok,
        output_tokens=out_tok,
        cost_usd=round(cost, 6),
    )


def _tier_key(model_id: str) -> str:
    """Map a concrete model id to the PriceBook key.

    The PriceBook is keyed by logical tier/id; a concrete Bedrock model id (or
    inference-profile id) falls through to the PriceBook's own default resolution,
    so we pass it straight through (PriceBook.llm_rate never raises).
    """
    return model_id


def spend_key(tenant: str, user: str, period: str) -> str:
    """Primary spend-table key: per-user spend for the period (§13.6)."""
    return f"{tenant}#{user}#{period}"


def spend_rollup_key(tenant: str, period: str) -> str:
    """Tenant-level rollup key: aggregate spend for the period (§13.6)."""
    return f"{tenant}#{period}"
