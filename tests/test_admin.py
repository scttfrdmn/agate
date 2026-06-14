"""Unit tests for the governed-access console: pure analytics + the admin gate.

No AWS. The analytics aggregation is pure; the handler's auth gate is tested with a
fake token verifier + a fake spend table.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agate import admin  # noqa: E402
from infra.functions.admin import handler as admin_handler  # noqa: E402

# --- pure analytics ----------------------------------------------------------

ITEMS = [
    {"pk": "chem#alice#2026-06", "spend_usd": 1.50},
    {"pk": "chem#bob#2026-06", "spend_usd": 0.50},
    {"pk": "kempner#carol#2026-06", "spend_usd": 4.00},
    {"pk": "chem#alice#2026-05", "spend_usd": 9.99},  # different period
    {"pk": "chem#2026-06", "spend_usd": 2.00},  # tenant-rollup row -> ignored
    {"pk": "garbage", "spend_usd": 1.0},  # malformed -> ignored
]


def test_rows_from_items_keeps_only_per_user_rows():
    rows = admin.rows_from_items(ITEMS)
    # 4 per-user rows (rollup + garbage dropped)
    assert len(rows) == 4
    assert all(r.user for r in rows)


def test_rollup_by_tenant_for_period():
    rows = admin.rows_from_items(ITEMS)
    rollups = admin.rollup_by_tenant(rows, period="2026-06")
    by_tenant = {r.tenant: r for r in rollups}
    assert by_tenant["kempner"].total_usd == 4.0
    assert by_tenant["chem"].total_usd == 2.0  # alice 1.5 + bob 0.5
    assert by_tenant["chem"].user_count == 2
    # sorted by spend desc -> kempner first
    assert rollups[0].tenant == "kempner"


def test_top_users():
    rows = admin.rows_from_items(ITEMS)
    top = admin.top_users(rows, period="2026-06", limit=2)
    assert top[0] == ("kempner/carol", 4.0)
    assert len(top) == 2


def test_to_console_payload_grand_total_excludes_rollup_and_garbage():
    payload = admin.to_console_payload(ITEMS, period="2026-06")
    # 1.5 + 0.5 + 4.0 = 6.0 (the #-rollup 2.0 and garbage 1.0 are NOT counted)
    assert payload["grand_total_usd"] == 6.0
    assert payload["tenant_count"] == 2


# --- admin gate (fail-closed) ------------------------------------------------


class _FakeTable:
    def __init__(self, items):
        self._items = items

    def scan(self, **_kw):
        return {"Items": self._items}


@pytest.fixture
def admin_token(monkeypatch):
    # Fake the verifier: the token string IS the JSON claims (as in the broker tests).
    def fake_verify(token, **_cfg):
        if not token:
            from agate.jwt_verify import TokenError

            raise TokenError("empty")
        return json.loads(token)

    monkeypatch.setattr(admin_handler, "verify_token", fake_verify)
    monkeypatch.setattr(admin_handler, "config_from_env", lambda: {})
    monkeypatch.setattr(admin_handler, "SPEND_TABLE", "agate-spend")
    monkeypatch.setattr(
        admin_handler._ddb, "Table", lambda _n: _FakeTable(ITEMS), raising=False
    )


def _event(claims: dict, period: str | None = None) -> dict:
    body: dict = {"idp_token": json.dumps(claims)}
    if period is not None:
        body["period"] = period
    return {"body": json.dumps(body)}


def test_admin_role_gets_analytics(admin_token):
    resp = admin_handler.handler(
        _event({"sub": "u", "tenant": "chem", "role": "admin"}, period="2026-06"), None
    )
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["grand_total_usd"] == 6.0  # period-scoped: 1.5 + 0.5 + 4.0
    assert body["tenant_count"] == 2


def test_non_admin_is_forbidden(admin_token):
    resp = admin_handler.handler(_event({"sub": "u", "tenant": "chem", "role": "student"}), None)
    assert resp["statusCode"] == 403
    assert "credentials" not in resp["body"]


def test_missing_token_is_forbidden(admin_token):
    resp = admin_handler.handler({"body": json.dumps({"idp_token": ""})}, None)
    assert resp["statusCode"] == 403


def test_role_absent_is_forbidden(admin_token):
    # No role claim -> claims_to_tags defaults to member -> 403 (fail-closed).
    resp = admin_handler.handler(_event({"sub": "u", "tenant": "chem"}), None)
    assert resp["statusCode"] == 403


def test_missing_spend_table_degrades_to_empty(admin_token, monkeypatch):
    # agate-audit not deployed -> table scan raises ResourceNotFoundException ->
    # admin returns empty analytics (200), not a 500.
    class _MissingTable:
        def scan(self, **_kw):
            raise admin_handler._ddb.meta.client.exceptions.ResourceNotFoundException(
                {"Error": {"Code": "ResourceNotFoundException", "Message": "missing"}},
                "Scan",
            )

    monkeypatch.setattr(admin_handler._ddb, "Table", lambda _n: _MissingTable(), raising=False)
    resp = admin_handler.handler(
        _event({"sub": "u", "tenant": "chem", "role": "admin"}, period="2026-06"), None
    )
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body["grand_total_usd"] == 0
    assert body["tenants"] == []
