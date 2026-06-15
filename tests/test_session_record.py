"""Unit tests for saved sessions (#109). No AWS — pure."""

from __future__ import annotations

import pytest
from agate.session_record import (
    Receipt,
    ReceiptRow,
    SavedSession,
    SessionRecordError,
    build_saved_session,
    from_json,
    session_object_key,
)


def _receipt(total=0.00055):
    return Receipt(
        rows=(ReceiptRow("panel", "llm", 0.0003), ReceiptRow("rag", "retrieval", 0.00025)),
        total=total,
    )


def _build(**over):
    kw = {
        "session_id": "s-1",
        "tenant": "chem",
        "scope": "chemistry/chem-101",
        "subject": "alice",
        "created": "2026-06-14T12:00:00Z",
        "mode": "DEBATE",
        "transcript": [{"role": "user", "text": "hi"}],
        "receipt": _receipt(),
    }
    kw.update(over)
    return build_saved_session(**kw)


# --- serialization round-trip -----------------------------------------------


def test_round_trips_through_json():
    s = _build()
    back = from_json(s.to_json())
    assert isinstance(back, SavedSession)
    assert back.id == "s-1"
    assert back.tenant == "chem"
    assert back.scope == "chemistry/chem-101"
    assert back.mode == "DEBATE"
    assert back.receipt.total == pytest.approx(0.00055)


def test_subject_stored_as_injective_provenance_key():
    a = _build(subject="alice")
    b = _build(subject="bob")
    assert a.subject != b.subject  # subject_key provenance
    # subjects that clean to the same string don't collide
    assert _build(subject="a/b").subject != _build(subject="ab").subject


# --- the S3 key IS the access fence -----------------------------------------


def test_key_lands_under_tenant_scope_sessions_prefix():
    assert session_object_key("chem", "chemistry/chem-101", "s-1") == (
        "chem/chemistry/chem-101/_sessions/s-1.json"
    )


def test_unscoped_key_is_tenant_root():
    assert session_object_key("chem", "", "s-1") == "chem/_sessions/s-1.json"


def test_scope_traversal_cannot_escape_prefix():
    # A `..` scope normalises to empty (the budget.normalise_scope guard) -> tenant root,
    # never `physics`. The key can't escape the {tenant}/ prefix.
    key = session_object_key("chem", "chemistry/../physics", "s-1")
    assert key == "chem/_sessions/s-1.json"
    assert "physics" not in key
    assert ".." not in key


def test_slash_laden_id_cannot_inject_levels():
    # session_id with `/` is cleaned to one segment — no extra path levels.
    key = session_object_key("chem", "chemistry/chem-101", "a/b/evil")
    assert key.count("/") == 4  # chem / chemistry / chem-101 / _sessions / <id>.json
    assert key.endswith("/_sessions/abevil.json")


def test_missing_tenant_or_id_rejected():
    with pytest.raises(SessionRecordError):
        session_object_key("", "chemistry", "s-1")
    with pytest.raises(SessionRecordError):
        session_object_key("chem", "chemistry", "")


# --- receipt fidelity (the audit claim) -------------------------------------


def test_forged_receipt_total_rejected_at_construction():
    # A Receipt self-validates on construction (every path), so a forged total can't even
    # be built — not just blocked at build_saved_session/from_json.
    with pytest.raises(SessionRecordError, match="!= sum of rows"):
        Receipt(rows=(ReceiptRow("x", "llm", 0.01),), total=99.0)


def test_float_precision_receipt_is_accepted():
    # 0.1 + 0.2 == 0.30000000000000004 in float; the receipt must NOT be wrongly rejected
    # (both sides rounded to 6dp). A correctness guard, per the #109 security review.
    r = Receipt(rows=(ReceiptRow("a", "llm", 0.1), ReceiptRow("b", "llm", 0.2)), total=0.1 + 0.2)
    assert r.rows_total() == pytest.approx(0.3)


def test_forged_receipt_total_rejected_at_parse():
    s = _build()
    tampered = s.to_json().replace('"total": 0.00055', '"total": 42.0')
    with pytest.raises(SessionRecordError, match="!= sum of rows"):
        from_json(tampered)


def test_saved_receipt_total_equals_sum_of_rows():
    s = _build()
    assert s.receipt.total == pytest.approx(s.receipt.rows_total())


# --- fork (new id, same scope + transcript prefix) --------------------------


def test_fork_keeps_scope_and_transcript_new_id():
    s = _build(session_id="s-1")
    forked = _build(session_id="s-2", transcript=s.transcript)
    assert forked.id != s.id
    assert forked.scope == s.scope  # a fork stays in the same scope (resume fence holds)
    assert forked.transcript == s.transcript
