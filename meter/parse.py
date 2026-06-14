"""Pure parsing of a Bedrock invocation-log record into an authoritative spend row.

A Bedrock model-invocation log record carries the authoritative token counts (the
counts the service billed), the model id, the caller identity, and a timestamp.
This module turns one record into a `SpendRecord` — the dollars (via the shared
`cost` engine) keyed by `{tenant}#{user}#{period}` for the spend table (§13.6).

Pure and AWS-free. The tenant/user come from the caller's assumed-role session: the
broker sets RoleSessionName to the federated subject and the session carries the
`agate:tenant` tag, both of which appear in the record's `identity.arn`
(`.../agate-authenticated/<RoleSessionName>`). The period is derived from the
record timestamp (year-month) so the key rolls over automatically.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from agate.tags import subject_from_session_name, tenant_from_session_name
from cost.pricing import PriceBook, default_pricebook

# assumed-role ARN: arn:aws:sts::<acct>:assumed-role/<role>/<RoleSessionName>
_ASSUMED_ROLE = re.compile(r"assumed-role/(?P<role>[^/]+)/(?P<session>.+)$")
# The spend key is `tenant#user#period`; `#` (and whitespace) in a part would split
# the key wrong and silently drop the row. Strip key-breaking chars defensively.
_KEY_UNSAFE = re.compile(r"[#\s]")


def _key_safe(part: str) -> str:
    """Make a spend-key component safe: no `#` (the delimiter) or whitespace."""
    return _KEY_UNSAFE.sub("-", part or "unknown") or "unknown"


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


def _identity(record: dict) -> tuple[str, str | None]:
    """Derive (user, tenant) from the record's assumed-role ARN — UNFORGEABLY.

    The broker sets RoleSessionName to `<tenant>@<subject>` (`agate.tags.
    role_session_name`), so both the user and the tenant are recoverable from the
    ARN, which Bedrock records on every invocation. This is the authoritative source
    (#79): it cannot be forged by a client choosing its own requestMetadata. Returns
    (user, tenant); tenant is None for a legacy/un-encoded session name, so the
    caller can fall back rather than mis-attribute.
    """
    arn = (record.get("identity") or {}).get("arn", "")
    m = _ASSUMED_ROLE.search(arn)
    if not m:
        return "unknown", None
    session = m.group("session")
    return subject_from_session_name(session), tenant_from_session_name(session)


def parse_invocation_record(
    record: dict,
    *,
    pricebook: PriceBook | None = None,
    tenant: str | None = None,
) -> SpendRecord:
    """Translate one Bedrock invocation-log record into a priced `SpendRecord`.

    `tenant` may be passed explicitly (e.g. from the record's `requestMetadata`
    carrying the `agate:tenant` tag, which the caller extracts); if absent we read
    `record['requestMetadata']['agate:tenant']`, falling back to "unknown". Raises
    RecordError if token counts or timestamp are missing — such a record cannot be
    metered and should be skipped, not guessed.
    """
    pb = pricebook or default_pricebook()

    model_id = record.get("modelId")
    if not model_id:
        raise RecordError("record has no modelId")

    period = _period_from_timestamp(record.get("timestamp", ""))
    user, arn_tenant = _identity(record)

    # Tenant precedence (#79): an explicit caller arg (tests) > the UNFORGEABLE tenant
    # encoded in the assumed-role session name > a non-authoritative requestMetadata
    # hint (client-controlled — kept only as a last resort for legacy/un-encoded
    # sessions) > "unknown". A `#` in any value would corrupt the spend key, so we
    # sanitise the resolved tenant to the key-safe grammar before use.
    metadata = record.get("requestMetadata") or {}
    resolved_tenant = tenant or arn_tenant or metadata.get("agate:tenant") or "unknown"
    resolved_tenant = _key_safe(resolved_tenant)
    user = _key_safe(user)

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


def scope_pk(tenant: str, node: str, period: str) -> str:
    """Per-scope-node spend/budget key: `tenant#scope#<node>#period` (#81 cascade).

    The literal `scope` segment discriminates these from the 3-part `tenant#user#period`
    and 2-part `tenant#period` keys. `node` is one ancestor scope path (e.g. `chemistry`
    or `chemistry/chem-101`); scope paths contain `/` but never `#`, so the key parses
    unambiguously. Used by the Tier-1 choke point for BOTH the budget and spend tables.
    """
    return f"{tenant}#scope#{node}#{period}"


def read_scope_spend_item(table, tenant: str, node: str, period: str) -> float:
    """Read a per-scope-node spend row (mirrors read_spend_item). Absent row → 0.0."""
    item = table.get_item(Key={"pk": scope_pk(tenant, node, period)}).get("Item")
    return float(item["spend_usd"]) if item and "spend_usd" in item else 0.0


def read_spend_item(table, tenant: str, user: str, period: str) -> float:
    """Read authoritative per-user spend from a DynamoDB spend table.

    Takes the table resource (not a module global) so both the spend Lambda and the
    Tier 1 choke point share one accessor + key format without coupling their
    Lambda-scoped clients. Absent row → 0.0.
    """
    item = table.get_item(Key={"pk": spend_key(tenant, user, period)}).get("Item")
    return float(item["spend_usd"]) if item and "spend_usd" in item else 0.0
