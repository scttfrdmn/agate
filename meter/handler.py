"""Authoritative spend Lambda (design §7.2, §13.6).

Triggered when Bedrock writes a model-invocation log object to the audit bucket.
For each record it: parses + prices it (pure `meter.parse`), then atomically
increments the per-user spend row AND the tenant rollup row in the `spend` table.
The soft cap reads `read_spend()` at credential refresh; the enforcement number is
therefore log-derived, never client-reported.

Tenant isolation note: spend is attributed from the record's own identity/tags, so
one tenant's usage can never inflate another's counter.
"""

from __future__ import annotations

import gzip
import json
import os
from decimal import Decimal

import boto3

from meter.parse import (
    RecordError,
    parse_invocation_record,
    read_spend_item,
    spend_key,
    spend_rollup_key,
)

SPEND_TABLE = os.environ.get("AGG_SPEND_TABLE", "")

_s3 = boto3.client("s3")
_ddb = boto3.resource("dynamodb")


def _read_log_object(bucket: str, key: str) -> list[dict]:
    """Read a Bedrock invocation-log object (one JSON record per line, maybe gzip)."""
    body = _s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    if key.endswith(".gz"):
        body = gzip.decompress(body)
    text = body.decode("utf-8")
    records = []
    for line in text.splitlines():
        line = line.strip()
        if line:
            records.append(json.loads(line))
    return records


def _increment(pk: str, amount: float) -> None:
    """Atomically add `amount` USD to a spend row (created on first write)."""
    _ddb.Table(SPEND_TABLE).update_item(
        Key={"pk": pk},
        UpdateExpression="ADD spend_usd :a",
        ExpressionAttributeValues={":a": Decimal(str(round(amount, 6)))},
    )


def record_spend(spend) -> None:
    """Upsert both the per-user row and the tenant rollup row for one SpendRecord."""
    _increment(spend_key(spend.tenant, spend.user, spend.period), spend.cost_usd)
    _increment(spend_rollup_key(spend.tenant, spend.period), spend.cost_usd)


def read_spend(tenant: str, user: str, period: str) -> float:
    """Authoritative spend for the soft cap (broker reads this at creds refresh)."""
    return read_spend_item(_ddb.Table(SPEND_TABLE), tenant, user, period)


def handler(event: dict, context: object) -> dict:
    """S3 event entry point. One bad record never aborts the batch."""
    metered = 0
    skipped = 0
    for rec in event.get("Records", []):
        s3 = rec.get("s3", {})
        bucket = s3.get("bucket", {}).get("name", "")
        from urllib.parse import unquote_plus

        key = unquote_plus(s3.get("object", {}).get("key", ""))
        try:
            for log_rec in _read_log_object(bucket, key):
                try:
                    record_spend(parse_invocation_record(log_rec))
                    metered += 1
                except RecordError:
                    skipped += 1
        except Exception:  # noqa: BLE001 — isolate per-object failures
            skipped += 1
    return {"metered": metered, "skipped": skipped}
