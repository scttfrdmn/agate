"""Unit tests for bounded delegation (#106). No live AWS (STS is faked)."""

from __future__ import annotations

import pytest
from agate.agentspec import BudgetSpec, parse_spec
from agate.delegate import (
    DelegationError,
    delegate,
    delegate_budget,
    scope_intersect,
    spawn_child,
)
from agate.tags import ROLE_ADMIN, ROLE_MEMBER, SessionTags


def _spawner(*, tier="frontier", scope="chemistry", courses=("chem-101",), role=ROLE_MEMBER):
    return SessionTags(
        affiliation="faculty",
        tenant="chem",
        courses=courses,
        tier=tier,
        role=role,
        scope=scope,
    )


def _spec(*, role="ta", scope="chemistry/chem-101"):
    d = {
        "agent": "a",
        "description": "d",
        "role": role,
        "reasoning": "lit-review",
    }
    if scope is not None:
        d["scope"] = scope
    return parse_spec(d)


# --- scope_intersect (the heart of the proof) -------------------------------


def test_scope_intersect_picks_more_specific():
    assert scope_intersect("chemistry", "chemistry/chem-101") == "chemistry/chem-101"
    assert scope_intersect("chemistry/chem-101", "chemistry") == "chemistry/chem-101"
    assert scope_intersect("chemistry", "chemistry") == "chemistry"


def test_scope_intersect_unscoped_is_whole_tenant():
    assert scope_intersect("", "chemistry") == "chemistry"
    assert scope_intersect("chemistry", "") == "chemistry"
    assert scope_intersect("", "") == ""


def test_scope_intersect_disjoint_is_none():
    assert scope_intersect("chemistry", "physics/phys-101") is None
    assert scope_intersect("chemistry", "physics") is None


def test_scope_intersect_no_string_prefix_bug():
    # `chem` must NOT be treated as containing `chemistry`.
    assert scope_intersect("chem", "chemistry") is None
    assert scope_intersect("chemistry", "chemistry-annex") is None


# --- delegate: tier = min ---------------------------------------------------


def test_tier_is_min_of_spawner_and_spec():
    # spawner frontier + spec ta(oss) -> oss
    child = delegate(_spawner(tier="frontier"), _spec(role="ta"))
    assert child.tier == "oss"
    # spawner mid + spec researcher(frontier) -> mid (spawner is the tighter bound)
    child2 = delegate(_spawner(tier="mid"), _spec(role="researcher", scope="chemistry/chem-101"))
    assert child2.tier == "mid"


def test_child_never_exceeds_spawner_tier_even_if_spec_higher():
    child = delegate(_spawner(tier="oss"), _spec(role="researcher", scope="chemistry/chem-101"))
    assert child.tier == "oss"


# --- delegate: scope intersection + fail-closed -----------------------------


def test_scope_narrows_to_more_specific():
    child = delegate(_spawner(scope="chemistry"), _spec(scope="chemistry/chem-101"))
    assert child.scope == "chemistry/chem-101"


def test_spawner_deeper_than_spec_keeps_spawner_scope():
    child = delegate(_spawner(scope="chemistry/chem-101"), _spec(scope="chemistry"))
    assert child.scope == "chemistry/chem-101"


def test_unscoped_spawner_takes_spec_scope():
    child = delegate(_spawner(scope=""), _spec(scope="chemistry"))
    assert child.scope == "chemistry"


def test_disjoint_scope_refuses_to_spawn():
    with pytest.raises(DelegationError, match="disjoint|outside"):
        delegate(_spawner(scope="chemistry"), _spec(scope="physics/phys-101"))


# --- delegate: courses / role / tenant --------------------------------------


def test_courses_inherited_from_spawner():
    child = delegate(_spawner(courses=("chem-101", "chem-202")), _spec())
    assert child.courses == ("chem-101", "chem-202")


def test_role_forced_to_member_even_if_spawner_admin():
    # An agent is never an admin (admin gates the console, not delegable).
    child = delegate(_spawner(role=ROLE_ADMIN), _spec())
    assert child.role == ROLE_MEMBER


def test_tenant_held_fixed():
    child = delegate(_spawner(), _spec())
    assert child.tenant == "chem"


# --- transitivity (sets up agent graphs #111) -------------------------------


def test_two_hop_chain_only_narrows():
    root = _spawner(tier="frontier", scope="chemistry")
    hop1 = delegate(root, _spec(role="instructor", scope="chemistry/chem-101"))  # mid, chem-101
    hop2 = delegate(hop1, _spec(role="ta", scope="chemistry/chem-101"))  # oss, chem-101
    assert hop2.tier == "oss"  # never climbs back up
    assert hop2.scope == "chemistry/chem-101"


def test_second_hop_at_broader_scope_still_confined_to_first_hop():
    # hop1 is confined to chem-101; a second-hop spec naming the broader "chemistry"
    # cannot widen — the intersection stays at the deeper chem-101.
    hop1 = delegate(_spawner(scope="chemistry"), _spec(scope="chemistry/chem-101"))
    hop2 = delegate(hop1, _spec(scope="chemistry"))
    assert hop2.scope == "chemistry/chem-101"


def test_second_hop_cannot_widen_to_sibling():
    hop1 = delegate(_spawner(scope="chemistry"), _spec(scope="chemistry/chem-101"))
    with pytest.raises(DelegationError):
        delegate(hop1, _spec(scope="chemistry/chem-202"))  # sibling of chem-101 -> disjoint


# --- delegate_budget --------------------------------------------------------


def test_budget_is_min_of_spec_and_remaining():
    assert delegate_budget(100.0, BudgetSpec(usd=20.0, per="student", period_kind="term")) == 20.0
    assert delegate_budget(5.0, BudgetSpec(usd=20.0, per="student", period_kind="term")) == 5.0


def test_budget_none_handling():
    assert delegate_budget(None, None) is None
    assert delegate_budget(None, BudgetSpec(usd=20.0, per="user", period_kind="month")) == 20.0
    assert delegate_budget(50.0, None) == 50.0


def test_budget_never_negative():
    assert delegate_budget(-5.0, None) == 0.0


# --- spawn_child (fake STS, like test_broker.py) ----------------------------


class _FakeSts:
    def __init__(self):
        self.last_call = None

    def assume_role(self, **kwargs):
        self.last_call = kwargs
        return {"Credentials": {"AccessKeyId": "ASIA", "SecretAccessKey": "s", "SessionToken": "t"}}


def test_spawn_child_passes_child_tags_and_encoded_session_name():
    sts = _FakeSts()
    child = delegate(_spawner(scope="chemistry"), _spec(scope="chemistry/chem-101"))
    creds = spawn_child(
        child, role_arn="arn:aws:iam::123:role/agate-agent", subject="u9", sts_client=sts
    )
    assert creds["AccessKeyId"] == "ASIA"
    sent = {t["Key"]: t["Value"] for t in sts.last_call["Tags"]}
    assert sent["agate:tenant"] == "chem"
    assert sent["agate:scope"] == "chemistry/chem-101"  # the narrowed scope
    assert sent["agate:tier"] == "oss"
    # #79: tenant encoded in the session name; tags transitive down the chain
    assert sts.last_call["RoleSessionName"] == "chem@u9"
    assert set(sts.last_call["TransitiveTagKeys"]) == set(sent.keys())
