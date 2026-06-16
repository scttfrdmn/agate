"""Unit tests for AG-UI — the event-stream governor (#119 slice). No AWS.

The §8.6/§10 invariant: an AG-UI event stream carries only what the session's credential
authorized — every event is stamped with its scope + the #137 attribution, and an event whose
explicit scope the session doesn't contain is DROPPED before it reaches the wire.
"""

from __future__ import annotations

from agate.agui import govern_event, governed_emit
from agate.identity import acting_as_from_session
from agate.tags import SessionTags, role_session_name


def _tags(scope="chemistry", tenant="uni", tier="frontier"):
    return SessionTags(
        affiliation="researcher", tenant=tenant, courses=(), tier=tier, role="member", scope=scope
    )


def _aa(tenant="uni", subject="prof", agent="uni/lab-agent"):
    return acting_as_from_session(role_session_name(tenant, subject), agent=agent)


# --- stamp (attribution) -----------------------------------------------------


def test_event_is_stamped_with_scope_and_attribution():
    e = govern_event({"type": "answer", "text": "hi"}, tags=_tags("chemistry"), acting_as=_aa())
    assert e["scope"] == "chemistry"
    assert e["actingAs"]["on_behalf_of"] == "uni@prof"
    assert e["text"] == "hi"  # payload preserved


def test_original_event_is_not_mutated():
    orig = {"type": "answer", "text": "x"}
    govern_event(orig, tags=_tags())
    assert "scope" not in orig and "actingAs" not in orig


def test_in_scope_by_construction_is_kept():
    # no explicit scope -> the session's own run -> kept, stamped with the session scope
    e = govern_event({"type": "model", "label": "reader"}, tags=_tags("chemistry/chem-101"))
    assert e is not None
    assert e["scope"] == "chemistry/chem-101"


# --- THE HEADLINE: drop out-of-scope ----------------------------------------


def test_disjoint_explicit_scope_is_dropped():
    # an event tagged for a scope the session doesn't contain -> never streamed
    assert govern_event({"type": "answer", "scope": "physics"}, tags=_tags("chemistry")) is None


def test_nested_explicit_scope_is_kept_and_stamped_with_the_deeper_node():
    # the session contains the event's nested scope -> kept, stamped with that nested scope
    e = govern_event({"type": "chart", "scope": "chemistry/chem-101"}, tags=_tags("chemistry"))
    assert e is not None
    assert e["scope"] == "chemistry/chem-101"


def test_parent_scope_is_dropped_event_cannot_widen():
    # an event tagged the BROADER parent scope is NOT contained by a narrower session -> dropped
    assert (
        govern_event({"type": "answer", "scope": "chemistry"}, tags=_tags("chemistry/chem-101"))
        is None
    )


def test_traversal_event_scope_fails_closed():
    assert (
        govern_event({"type": "x", "scope": "chemistry/../physics"}, tags=_tags("chemistry"))
        is None
    )


def test_sibling_prefix_scope_is_dropped():
    # chemistry-annex is NOT contained by chemistry (no string-prefix bug) -> dropped
    assert (
        govern_event({"type": "x", "scope": "chemistry-annex"}, tags=_tags("chemistry")) is None
    )


def test_non_canonical_scope_segments_are_dropped():
    # `.` and empty (`//`) segments are rejected so a stamped scope is always canonical
    # (no audit ambiguity), matching the tags scope grammar.
    assert govern_event({"type": "x", "scope": "chemistry/."}, tags=_tags("chemistry")) is None
    assert (
        govern_event({"type": "x", "scope": "chemistry//chem-101"}, tags=_tags("chemistry"))
        is None
    )


def test_tenant_wide_session_keeps_any_scope():
    # an unscoped (tenant-wide) session contains every scope in its tenant
    e = govern_event({"type": "answer", "scope": "physics"}, tags=_tags(""))
    assert e is not None and e["scope"] == "physics"


# --- attribution fail-closed -------------------------------------------------


def test_no_attribution_record_means_no_actingAs_key():
    # govern_event with acting_as=None stamps no actingAs — never fabricates a user (#137)
    e = govern_event({"type": "answer"}, tags=_tags())
    assert "actingAs" not in e
    assert e["scope"] == "chemistry"  # scope filter still applies


# --- the wrapper composes at the choke point --------------------------------


def test_governed_emit_forwards_kept_and_drops_out_of_scope():
    sink: list[dict] = []
    emit = governed_emit(sink.append, tags=_tags("chemistry"), acting_as=_aa())
    for ev in [
        {"type": "a"},  # in-scope by construction
        {"type": "b", "scope": "chemistry/chem-101"},  # nested -> kept
        {"type": "c", "scope": "physics"},  # disjoint -> dropped
    ]:
        emit(ev)
    assert [x["type"] for x in sink] == ["a", "b"]  # c dropped
    assert all("scope" in x and "actingAs" in x for x in sink)  # every streamed event stamped


def test_governed_emit_preserves_the_emit_signature():
    # the wrapped sink is a plain Callable[[dict], None] — composes anywhere an Emit is wanted
    sink: list[dict] = []
    emit = governed_emit(sink.append, tags=_tags())
    emit({"type": "cost", "total": 0.01})
    assert sink and sink[0]["type"] == "cost"
