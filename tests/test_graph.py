"""Unit tests for agent graphs + cascade budget/attribution (#111 + #112). No AWS."""

from __future__ import annotations

import pytest
from agate.agentspec import parse_spec
from agate.graph import (
    GraphError,
    attribution_chain,
    build_graph,
    cascade_nodes,
    flatten,
)
from agate.tags import SessionTags
from cost import evaluate_cascade


def _node(name, role, scope, *, agents=(), **caps):
    d = {
        "agent": name,
        "description": "d",
        "role": role,
        "scope": scope,
        "reasoning": "lit-review",
    }
    if agents:
        d["agents"] = agents
    d.update(caps)
    return d


def _root_tags(*, tier="frontier", scope="lab"):
    return SessionTags(affiliation="faculty", tenant="uni", courses=(), tier=tier, scope=scope)


def _graph():
    spec = parse_spec(
        _node(
            "grant-writer",
            "researcher",
            "lab",
            agents=[
                _node(
                    "lit",
                    "researcher",
                    "lab/photonics",
                    agents=[_node("cite-check", "student", "lab/photonics")],
                ),
                _node("budget", "staff", "lab/photonics"),
            ],
        )
    )
    return build_graph(spec, _root_tags(), subject="prof")


# --- structure + monotonic narrowing (#111) ---------------------------------


def test_graph_builds_with_expected_topology():
    g = _graph()
    nodes = flatten(g)
    assert [n.path[-1] for n in nodes] == ["grant-writer", "lit", "cite-check", "budget"]


def test_each_child_credential_is_delegated_from_parent():
    g = _graph()
    nodes = {"/".join(n.path): n for n in flatten(g)}
    root = nodes["grant-writer"]
    lit = nodes["grant-writer/lit"]
    gc = nodes["grant-writer/lit/cite-check"]
    # tier narrows monotonically: researcher(frontier) -> researcher(frontier) -> student(oss)
    assert root.tags.tier == "frontier"
    assert lit.tags.tier == "frontier"
    assert gc.tags.tier == "oss"
    # scope narrows: lab -> lab/photonics -> lab/photonics (never widens)
    assert root.tags.scope == "lab"
    assert lit.tags.scope == "lab/photonics"
    assert gc.tags.scope == "lab/photonics"


def test_grandchild_subset_of_child_subset_of_root():
    g = _graph()
    nodes = {"/".join(n.path): n for n in flatten(g)}
    root, lit, gc = (
        nodes["grant-writer"],
        nodes["grant-writer/lit"],
        nodes["grant-writer/lit/cite-check"],
    )
    # scope containment: gc ⊆ lit ⊆ root
    assert gc.tags.scope.startswith(lit.tags.scope) or gc.tags.scope == lit.tags.scope
    assert lit.tags.scope.startswith(root.tags.scope + "/") or lit.tags.scope == root.tags.scope
    # tier rank only descends
    from agate.entitlements import TIER_RANK

    assert TIER_RANK[gc.tags.tier] <= TIER_RANK[lit.tags.tier] <= TIER_RANK[root.tags.tier]


def test_disjoint_scope_child_refuses_to_build():
    spec = parse_spec(
        _node(
            "root",
            "researcher",
            "chemistry",
            agents=[_node("rogue", "researcher", "physics/phys-101")],
        )  # disjoint
    )
    with pytest.raises(GraphError, match="cannot delegate"):
        build_graph(spec, _root_tags(scope="chemistry"), subject="p")


# --- caps (#111: no infinite recursion / fan-out bomb) ----------------------


def test_max_fanout_breach_refused_at_parse():
    from agate.agentspec import SpecError

    with pytest.raises(SpecError, match="max_fanout"):
        parse_spec(
            _node(
                "root",
                "staff",
                "lab",
                max_fanout=1,
                agents=[_node("a", "staff", "lab"), _node("b", "staff", "lab")],
            )
        )


def test_parse_rejects_deep_chain_without_stack_overflow():
    # SECURITY (#111 review): parse_spec recurses into children, so a deep chain must be
    # rejected at PARSE time (before build) — else it stack-overflows. No RecursionError.
    from agate.agentspec import SpecError

    base = {"agent": "x", "description": "d", "role": "staff", "reasoning": "lit-review"}

    def chain(d):
        return base if d == 0 else {**base, "agents": [chain(d - 1)]}

    with pytest.raises(SpecError, match="maximum depth"):
        parse_spec(chain(800))  # would overflow if unbounded


def test_parse_rejects_wide_tree_by_node_budget():
    # A wide tree (fanout^depth) must be bounded at parse by the total-node ceiling.
    from agate.agentspec import SpecError

    base = {"agent": "x", "description": "d", "role": "staff", "reasoning": "lit-review"}

    def wide(d, f):
        if d == 0:
            return base
        return {**base, "max_fanout": f, "agents": [wide(d - 1, f) for _ in range(f)]}

    with pytest.raises(SpecError, match="total node count"):
        parse_spec(wide(8, 8))


def test_max_depth_breach_refused_at_build():
    # depth 2 chain with the root capped at max_depth=1 -> refused.
    spec = parse_spec(
        _node(
            "root",
            "staff",
            "lab",
            max_depth=1,
            agents=[_node("c1", "staff", "lab", agents=[_node("c2", "staff", "lab")])],
        )
    )
    with pytest.raises(GraphError, match="max_depth"):
        build_graph(spec, _root_tags(scope="lab"), subject="p")


# --- attribution (#112: the call graph is the audit graph) ------------------


def test_attribution_chain_traces_root_to_node():
    g = _graph()
    gc = flatten(g)[2]  # cite-check
    chain = attribution_chain(gc, subject="prof")
    assert chain.startswith("uni@")  # tenant-encoded (#79)
    assert chain.endswith("grant-writer/lit/cite-check")


# --- family budget (#112): a call must fit under EVERY ancestor -------------


def test_cascade_nodes_cover_full_ancestry():
    g = _graph()
    gc = flatten(g)[2]
    labels = [r[0] for r in cascade_nodes(gc, lambda n, i, lbl: (0.0, 100.0))]
    assert labels == ["grant-writer", "lit", "cite-check"]


def test_call_allowed_when_it_fits_every_ancestor_budget():
    g = _graph()
    gc = flatten(g)[2]
    # every ancestor has $100 budget, $0 spent -> a cheap call is allowed
    nodes = cascade_nodes(gc, lambda n, i, lbl: (0.0, 100.0))
    res = evaluate_cascade(
        model_id="openai.gpt-oss-20b-1:0", input_tokens=1000, max_tokens=500, nodes=nodes
    )
    assert res.decision == "allow"


def test_call_rejected_when_an_ancestor_budget_is_exhausted():
    g = _graph()
    gc = flatten(g)[2]

    # the MIDDLE ancestor (lit) has only $0.000001 left -> the family ceiling rejects,
    # naming the breaching node (the #112 guarantee).
    def lookup(node, i, label):
        return (0.0, 0.000001) if label == "lit" else (0.0, 100.0)

    nodes = cascade_nodes(gc, lookup)
    res = evaluate_cascade(
        model_id="us.anthropic.claude-opus-4-1-20250805-v1:0",
        input_tokens=100000,
        max_tokens=4000,
        nodes=nodes,
    )
    assert res.decision == "reject"
    assert res.breaching_node == "lit"
