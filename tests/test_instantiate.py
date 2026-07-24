"""Unit tests for per-invoker instantiation (#107). No AWS — pure."""

from __future__ import annotations

import pytest
from agate.agentspec import parse_spec
from agate.delegate import (
    DelegationError,
    InstantiatedAgent,
    instantiate_for_invoker,
    invoker_namespace,
    is_eligible_invoker,
)
from agate.tags import SessionTags


def _invoker(*, tenant="chem", courses=("chem-101",), scope="chemistry/chem-101", tier="oss"):
    return SessionTags(
        affiliation="student", tenant=tenant, courses=courses, tier=tier, scope=scope
    )


def _spec(*, invokers="roster:chem-101", scope="chemistry/chem-101"):
    d = {
        "agent": "chem101-ta",
        "description": "d",
        "role": "ta",
        "scope": scope,
        "reasoning": "lit-review",
    }
    if invokers is not None:
        d["invokers"] = invokers
    return parse_spec(d)


# --- eligibility ------------------------------------------------------------


def test_roster_eligible_iff_enrolled():
    spec = _spec(invokers="roster:chem-101")
    assert is_eligible_invoker(_invoker(courses=("chem-101",)), spec) is True
    assert is_eligible_invoker(_invoker(courses=("chem-202",)), spec) is False
    assert is_eligible_invoker(_invoker(courses=()), spec) is False


def test_scope_eligible_iff_invoker_scope_contains_ref():
    spec = _spec(invokers="scope:chemistry")
    # a chair scoped to chemistry (or above) may run it
    assert is_eligible_invoker(_invoker(scope="chemistry"), spec) is True
    assert is_eligible_invoker(_invoker(scope=""), spec) is True  # tenant-wide contains all
    # a student in a sibling course cannot
    assert is_eligible_invoker(_invoker(scope="physics/phys-101"), spec) is False
    # string-prefix trap: chem does not contain chemistry
    assert is_eligible_invoker(_invoker(scope="chem"), spec) is False


def test_tenant_invoker_always_eligible_within_tenant():
    spec = _spec(invokers="tenant")
    assert is_eligible_invoker(_invoker(), spec) is True


def test_no_invokers_restriction_is_eligible():
    spec = _spec(invokers=None)
    assert is_eligible_invoker(_invoker(courses=()), spec) is True


# --- instantiate ------------------------------------------------------------


def test_eligible_invoker_gets_child_bounded_by_delegate():
    spec = _spec()
    ia = instantiate_for_invoker(_invoker(), spec, subject="alice")
    assert isinstance(ia, InstantiatedAgent)
    assert ia.child_tags.scope == "chemistry/chem-101"
    assert ia.child_tags.tenant == "chem"
    assert ia.invoker_subject == "alice"


def test_ineligible_invoker_is_refused_no_child():
    spec = _spec(invokers="roster:chem-101")
    with pytest.raises(DelegationError, match="not eligible"):
        instantiate_for_invoker(_invoker(courses=("chem-202",)), spec, subject="bob")


def test_namespace_is_per_invoker_and_readable():
    spec = _spec()
    a = instantiate_for_invoker(_invoker(), spec, subject="alice")
    b = instantiate_for_invoker(_invoker(), spec, subject="bob")
    assert a.namespace.startswith("chem/alice-")
    assert b.namespace.startswith("chem/bob-")
    assert a.namespace != b.namespace  # two invokers never share a namespace


def test_namespace_is_injective_despite_lossy_id_cleaning():
    # SECURITY (#107 review): subjects that CLEAN to the same string must NOT collide —
    # the digest of the raw ids keeps the key injective (else cross-invoker memory leak).
    ns1 = invoker_namespace("chem", "a/b")
    ns2 = invoker_namespace("chem", "ab")
    assert ns1 != ns2  # would have collided to "chem/ab" without the digest
    # same raw inputs → same key (stable)
    assert invoker_namespace("chem", "a/b") == ns1


# --- the §2 headline: disjoint, own-scope-only credentials ------------------


def test_two_invokers_of_same_agent_get_disjoint_scopes():
    # An agent scoped per the invoker (spec scope contains both leaves) instantiated by
    # two students in different courses → disjoint child scopes.
    spec = _spec(invokers="scope:chemistry", scope="chemistry")
    a = instantiate_for_invoker(_invoker(scope="chemistry/chem-101"), spec, subject="alice")
    b = instantiate_for_invoker(_invoker(scope="chemistry/chem-202"), spec, subject="bob")
    assert a.child_tags.scope == "chemistry/chem-101"
    assert b.child_tags.scope == "chemistry/chem-202"
    # neither scope contains the other — disjoint by construction
    assert not b.child_tags.scope.startswith(a.child_tags.scope + "/")
    assert not a.child_tags.scope.startswith(b.child_tags.scope + "/")


def test_invoker_scope_narrower_than_spec_wins():
    # The child is bounded by the INVOKER's authority, not just the spec's: a spec scoped
    # to all of `chemistry` instantiated by a chem-101-only student → child is chem-101.
    spec = _spec(invokers="scope:chemistry", scope="chemistry")
    ia = instantiate_for_invoker(_invoker(scope="chemistry/chem-101"), spec, subject="s")
    assert ia.child_tags.scope == "chemistry/chem-101"
