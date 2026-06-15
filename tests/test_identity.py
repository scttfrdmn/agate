"""Unit tests for agent identity & the OBO acting-as record (#137). No AWS."""

from __future__ import annotations

import pytest
from agate.agentcompile import acting_as, compile_agent
from agate.agentspec import parse_spec
from agate.graph import build_graph, flatten, node_acting_as
from agate.identity import (
    UNATTRIBUTED,
    ActingAs,
    acting_as_from_session,
    agent_id,
    spec_version,
)
from agate.tags import SessionTags


def _spec(**over):
    d = {
        "agent": "chem101-ta", "description": "d", "role": "ta",
        "scope": "chemistry/chem-101", "reasoning": "lit-review", "tools": ["hpc-monitor"],
    }
    d.update(over)
    return parse_spec(d)


# --- agent_id (the stable WHO) ----------------------------------------------


def test_agent_id_is_deterministic_and_stable():
    assert agent_id("chem", "chem101-ta") == "chem/chem101-ta"
    assert agent_id("chem", "chem101-ta") == agent_id("chem", "chem101-ta")


def test_agent_id_distinct_across_tenants_and_names():
    assert agent_id("chem", "ta") != agent_id("psych", "ta")  # tenant
    assert agent_id("chem", "ta") != agent_id("chem", "grader")  # name


def test_agent_id_cannot_inject_extra_path_segments():
    # `/`-laden parts collapse to one segment each — no escape past the {tenant}/ prefix.
    aid = agent_id("chem/../evil", "a/b")
    assert aid.count("/") == 1  # exactly tenant / name


def test_agent_id_requires_both_parts():
    with pytest.raises(ValueError):
        agent_id("", "ta")
    with pytest.raises(ValueError):
        agent_id("chem", "")


# --- spec_version (provenance) ----------------------------------------------


def test_spec_version_stable_for_same_spec():
    assert spec_version(_spec()) == spec_version(_spec())


def test_spec_version_changes_when_the_bound_changes():
    base = spec_version(_spec())
    assert spec_version(_spec(scope="chemistry")) != base  # scope
    assert spec_version(_spec(tools=[])) != base  # tools
    assert spec_version(_spec(role="researcher")) != base  # role/tier


# --- acting_as_from_session: OBO recovered, never fabricated ----------------


def test_recovers_obo_user_from_verified_session_name():
    aa = acting_as_from_session("chem@alice", agent="chem/chem101-ta")
    assert aa.tenant == "chem"
    assert aa.subject == "alice"
    assert aa.on_behalf_of == "chem@alice"
    assert aa.attributed is True


def test_legacy_session_is_unattributed_never_fabricated():
    # No `<tenant>@` -> fail-closed: explicitly unattributed, NOT a guessed user.
    aa = acting_as_from_session("just-a-name", agent="x")
    assert aa.subject == UNATTRIBUTED
    assert aa.on_behalf_of == UNATTRIBUTED
    assert aa.attributed is False


def test_to_dict_carries_all_three_answers():
    aa = acting_as_from_session(
        "chem@alice", agent="chem/ta", agent_version="abc123",
        remit={"tier": "oss", "scope": "chemistry", "tools": []}, chain="ta",
    )
    d = aa.to_dict()
    assert d["agent"] == "chem/ta"  # WHO
    assert d["on_behalf_of"] == "chem@alice"  # WHOSE authority
    assert d["remit"]["tier"] == "oss"  # WHAT remit
    assert d["chain"] == "ta"
    assert d["attributed"] is True


# --- the OBO invariant: never half-attributed -------------------------------


def test_attributed_requires_both_agent_and_user():
    # agent + real user -> attributed
    assert ActingAs(agent="a", agent_version="", tenant="chem", subject="alice").attributed
    # missing user -> not attributed
    assert not ActingAs(agent="a", agent_version="", tenant="", subject=UNATTRIBUTED).attributed


# --- emitted by the compiler (#105) -----------------------------------------


def test_compile_acting_as_binds_verified_user():
    c = compile_agent(_spec())
    aa = acting_as(c, tenant="chem", subject="alice")
    assert aa.agent == "chem/chem101-ta"
    assert aa.on_behalf_of == "chem@alice"
    assert aa.agent_version == c.agent_version  # provenance carried
    assert aa.remit["scope"] == "chemistry/chem-101"
    assert aa.attributed is True


def test_compiled_agent_carries_version():
    c = compile_agent(_spec())
    assert c.agent_version  # non-empty provenance digest


# --- emitted per graph node (#112) ------------------------------------------


def _graph():
    root = parse_spec(
        {
            "agent": "root", "description": "d", "role": "researcher", "scope": "lab",
            "reasoning": "lit-review",
            "agents": [
                {"agent": "lit", "description": "d", "role": "student",
                 "scope": "lab/photonics", "reasoning": "lit-review",
                 "agents": [{"agent": "cite", "description": "d", "role": "student",
                             "scope": "lab/photonics", "reasoning": "lit-review"}]}
            ],
        }
    )
    tags = SessionTags(
        affiliation="faculty", tenant="uni", courses=(), tier="frontier", scope="lab"
    )
    return build_graph(root, tags, subject="prof")


def test_graph_node_acting_as_carries_chain_and_one_root_user():
    g = _graph()
    grandchild = flatten(g)[2]  # root/lit/cite
    aa = node_acting_as(grandchild, subject="prof")
    assert aa.agent == "uni/cite"  # this node's own identity
    assert aa.chain == "root/lit/cite"  # full ancestry
    assert aa.on_behalf_of == "uni@prof"  # the one authorizing user, every hop
    assert aa.remit["tier"] == "oss"  # the node's narrowed tier (student)
    assert aa.attributed is True
