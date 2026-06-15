"""Unit tests for cross-session memory namespaces (#110). No AWS — pure."""

from __future__ import annotations

from agate.memory import (
    namespaces_for,
    personal_namespace,
    session_namespace,
    shared_namespace,
)
from agate.tags import SessionTags


def _tags(*, tenant="chem", scope="chemistry/chem-101"):
    return SessionTags(
        affiliation="student", tenant=tenant, courses=("chem-101",), tier="oss", scope=scope
    )


# --- shape: hierarchical, tenant-outermost, trailing slash ------------------


def test_personal_namespace_shape():
    ns = personal_namespace(_tags(), "alice")
    assert ns.startswith("agate/chem/personal/")
    assert ns.endswith("/")


def test_session_nests_under_personal():
    # Memory is part of the SESSION and the PRINCIPAL: the session namespace is a child
    # of the personal one (so a session's scratch memory lives under the principal tree).
    t = _tags()
    sess = session_namespace(t, "alice", "s-42")
    pers = personal_namespace(t, "alice")
    assert sess.startswith(pers)
    assert sess.endswith("/session/s-42/")


def test_shared_namespace_keyed_to_scope():
    ns = shared_namespace(_tags(scope="chemistry/chem-101"))
    assert ns == "agate/chem/shared/chemistry/chem-101/"


def test_tenant_is_always_the_outermost_fence():
    for ns in namespaces_for(_tags(), "alice", "s1").values():
        assert ns.split("/")[0] == "agate"
        assert ns.split("/")[1] == "chem"  # tenant immediately under the root


# --- fail-closed: no shared tier without a scope ----------------------------


def test_unscoped_session_has_no_shared_tier():
    u = _tags(scope="")
    assert shared_namespace(u) is None
    ns = namespaces_for(u, "bob", "s1")
    assert "shared" not in ns
    assert set(ns) == {"session", "personal"}


# --- injective subject (the #107 property, reused) --------------------------


def test_two_subjects_get_distinct_personal_namespaces():
    t = _tags()
    assert personal_namespace(t, "alice") != personal_namespace(t, "bob")
    # subjects that CLEAN to the same string must NOT collide (lossy _clean_id + digest)
    assert personal_namespace(t, "a/b") != personal_namespace(t, "ab")


def test_namespace_is_stable_for_same_inputs():
    t = _tags()
    assert personal_namespace(t, "alice") == personal_namespace(t, "alice")


# --- no fence-escaping injection --------------------------------------------


def test_slash_laden_ids_cannot_escape_their_fence():
    # A tenant/subject id containing `/` must NOT inject extra path levels that escape
    # the tenant fence — the `/` is stripped so the whole id collapses to ONE segment.
    t = SessionTags(
        affiliation="student", tenant="chem/../evil", courses=(), tier="oss", scope=""
    )
    ns = personal_namespace(t, "alice/../bob")
    # Exactly the fixed shape: agate / <one-tenant-seg> / personal / <one-subject-seg> /
    parts = ns.strip("/").split("/")
    assert parts[0] == "agate"
    assert parts[2] == "personal"
    assert len(parts) == 4  # no extra levels injected — fence intact


def test_all_dots_segment_is_dropped():
    # A pure `.`/`..` segment (vs a legitimate `a.b`) is stripped — hygiene, mirrors #107.
    t = _tags(scope="chemistry/../physics")  # the `..` level
    # scope is already normalised upstream, but the namespace builder is defensive too:
    ns = shared_namespace(t)
    assert ns is None or "/../" not in ns


def test_two_invokers_disjoint_across_all_tiers():
    t = _tags()
    a = namespaces_for(t, "alice", "s1")
    b = namespaces_for(t, "bob", "s1")
    # personal + session differ (subject-keyed); shared is the same (same scope) — by design
    assert a["personal"] != b["personal"]
    assert a["session"] != b["session"]
    assert a["shared"] == b["shared"]  # shared tier IS shared within a scope
