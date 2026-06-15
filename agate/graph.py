"""Agent graphs — governed composition of agents (#111 + #112, vision §4).

Generalizes the Panel/Analyze roster into a graph where a node may be a model or
another AGENT. Three hard rules make "agents calling agents" safe rather than a
privilege-escalation / runaway-cost engine:
  1. **Monotonic narrowing** — every node's credential is `delegate`d from its parent
     (#106), so a grandchild's authority ⊆ child ⊆ root on tier and scope, transitively.
     A child whose declared scope is disjoint from its parent refuses to build.
  2. **Family budget** — a node's call must fit under EVERY ancestor's remaining budget;
     `cascade_nodes` builds the node-list `cost.evaluate_cascade` (#81) already gates on.
  3. **Attribution** — each node carries a `root@…/child@…` chain (#79 session-name
     encoding), so the call graph IS the audit graph, unforgeable.

This module is PURE: `build_graph` narrows the credentials and enforces the structural
caps; the actual execution (assume each role, run its reasoning, debit real spend) is the
deferred executor. Budget numbers come from an injected `spend_lookup`, so no AWS/cost
import here — `graph.py` stays unit-testable and cycle-free.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from agate.agentspec import AgentSpec
from agate.delegate import delegate
from agate.identity import ActingAs, agent_id, spec_version
from agate.tags import SessionTags, role_session_name


class GraphError(ValueError):
    """A graph that cannot be built safely — a depth/fanout cap breach, or a child whose
    scope is disjoint from its parent (which `delegate` refuses). Fail closed."""


@dataclass(frozen=True, slots=True)
class GraphNode:
    """One node of a built agent graph. `tags` is this node's NARROWED credential
    (intersection of its parent's authority and its own spec). `path` is the root→here
    label chain for attribution; `depth` is 0 at the root."""

    spec: AgentSpec
    tags: SessionTags
    depth: int
    path: tuple[str, ...]
    children: tuple[GraphNode, ...] = field(default_factory=tuple)


def build_graph(root_spec: AgentSpec, root_tags: SessionTags, *, subject: str = "") -> GraphNode:
    """Build the graph for `root_spec` under `root_tags`, narrowing each child's
    credential from its parent's (monotonic, transitive) and enforcing the ROOT's caps as
    the single family ceiling.

    The root node carries `root_tags` verbatim (it's already the verified session). Each
    child's tags = `delegate(parent_tags, child_spec)` — so a child can never out-scope or
    out-tier its parent, and a disjoint-scope child raises `GraphError` (via delegate's
    DelegationError). `max_depth`/`max_fanout` come from the ROOT (one ceiling for the
    whole family, not per-node), so a sub-agent can't widen the bound."""
    return _build(root_spec, root_tags, subject, root_spec.max_depth, root_spec.max_fanout, 0,
                  (root_spec.name,))


def _build(
    spec: AgentSpec,
    tags: SessionTags,
    subject: str,
    max_depth: int,
    max_fanout: int,
    depth: int,
    path: tuple[str, ...],
) -> GraphNode:
    if depth > max_depth:
        raise GraphError(f"agent graph exceeds max_depth {max_depth} at {'/'.join(path)}")
    if len(spec.agents) > max_fanout:
        raise GraphError(
            f"node {'/'.join(path)} exceeds max_fanout {max_fanout} ({len(spec.agents)} children)"
        )
    children: list[GraphNode] = []
    for child_spec in spec.agents:
        try:
            child_tags = delegate(tags, child_spec, subject=subject)
        except ValueError as exc:  # DelegationError (disjoint scope, etc.) — fail closed
            raise GraphError(
                f"cannot delegate to child {child_spec.name!r} of {'/'.join(path)}: {exc}"
            ) from exc
        children.append(
            _build(
                child_spec, child_tags, subject, max_depth, max_fanout,
                depth + 1, (*path, child_spec.name),
            )
        )
    return GraphNode(spec=spec, tags=tags, depth=depth, path=path, children=tuple(children))


def flatten(root: GraphNode) -> list[GraphNode]:
    """All nodes of the graph, root-first (pre-order). For the executor to walk."""
    out = [root]
    for c in root.children:
        out.extend(flatten(c))
    return out


def attribution_chain(node: GraphNode, *, subject: str = "") -> str:
    """The unforgeable attribution for a node: `<tenant>@<root>/.../<this>`. Reuses the
    #79 `role_session_name` tenant encoding so every hop in the call graph is traceable to
    the tenant + the chain that reached it (the call graph IS the audit graph)."""
    base = role_session_name(node.tags.tenant, subject or node.path[0])
    return base + "/" + "/".join(node.path)


def node_acting_as(node: GraphNode, *, subject: str) -> ActingAs:
    """The OBO 'acting-as' record for one graph node's actions (#137). The agent is THIS
    node (its own scoped identity), the chain is its full root→here ancestry, and the OBO
    user is the verified subject — so a sub-agent's action records *agent X, on behalf of
    user U, via root/.../X*. The whole graph acts on the one root user's authority, each
    hop attributed to its own node."""
    return ActingAs(
        agent=agent_id(node.tags.tenant, node.path[-1]),
        agent_version=spec_version(node.spec),
        tenant=node.tags.tenant,
        subject=subject,
        remit={"tier": node.tags.tier, "scope": node.tags.scope, "tools": list(node.spec.tools)},
        chain="/".join(node.path),
    )


def cascade_nodes(
    node: GraphNode, spend_lookup
) -> list[tuple[str, float, float | None]]:
    """The `cost.evaluate_cascade` node-list for a call AT `node`: one (label, spend,
    budget) row per ANCESTOR (root→node), so the call must fit under EVERY ancestor's
    remaining budget — the family ceiling (#112). `spend_lookup(GraphNode) -> (spend,
    budget|None)` is injected (the executor supplies live numbers from the spend table;
    tests fake it), keeping this pure. A node with no budget row imposes no cap (None),
    exactly as `evaluate_cascade` treats it.

    `node` here must carry its ancestor chain; callers pass a node obtained from a walk
    that threads ancestry (the executor holds the path). We reconstruct the ancestry
    labels from `node.path` and let `spend_lookup` resolve each level's numbers."""
    rows: list[tuple[str, float, float | None]] = []
    for i, label in enumerate(node.path):
        spend, budget = spend_lookup(node, i, label)
        rows.append((label, spend, budget))
    return rows
