"""Unit tests for A2UI — the panel-action governor (#119 slice). No AWS.

The §8.6/§10 invariant: an agent-generated interactive panel surfaces only the actions its
credential permits — a panel action is INERT until it resolves to a capability the agent
holds (denied by absence), and a write action is draft-bound (never live, §5). A live panel
can't become a privileged-action surface.
"""

from __future__ import annotations

from agate.a2ui import PanelAction, can_surface_action, govern_panel

_TOOLS = ("library-search", "gradebook-drafts")  # a held read + a held write


# --- THE HEADLINE: inert until held -----------------------------------------


def test_action_for_a_held_capability_is_allowed():
    v = can_surface_action(PanelAction("search", "library-search"), _TOOLS)
    assert v.allowed is True


def test_action_for_an_unheld_capability_is_denied():
    # the agent didn't declare hpc-submit -> the control is stripped (denied by absence)
    v = can_surface_action(PanelAction("run-hpc", "hpc-submit"), _TOOLS)
    assert v.allowed is False
    assert "hpc-submit" in v.reason


def test_display_only_action_is_always_allowed():
    # a control that invokes no capability is inert by construction
    v = can_surface_action(PanelAction("show-chart"), ())
    assert v.allowed is True


# --- write actions are draft-bound (§5) -------------------------------------


def test_write_action_is_marked_write():
    v = can_surface_action(PanelAction("submit", "gradebook-drafts"), _TOOLS)
    assert v.allowed is True
    assert v.write is True  # the renderer labels it "draft for review, never live"


def test_read_action_is_not_marked_write():
    v = can_surface_action(PanelAction("search", "library-search"), _TOOLS)
    assert v.write is False


# --- fail-closed -------------------------------------------------------------


def test_unknown_capability_in_tools_is_denied_not_raised():
    # belt-and-suspenders: if a (malformed) tools set carries a capability NOT in the #113
    # catalog, the action is DENIED, never raised/surfaced.
    v = can_surface_action(
        PanelAction("evil", "delete-the-internet"), ("delete-the-internet",)
    )
    assert v.allowed is False
    assert "unknown capability" in v.reason


def test_unheld_capability_checked_before_catalog():
    # a capability not in the agent's tools is denied by absence even if it IS a real catalog
    # entry (hpc-submit exists, but this agent didn't declare it)
    v = can_surface_action(PanelAction("run", "hpc-submit"), _TOOLS)
    assert v.allowed is False
    assert "denied by absence" in v.reason


# --- payload is inert --------------------------------------------------------


def test_payload_does_not_affect_the_verdict():
    benign = can_surface_action(PanelAction("x", "hpc-submit", payload={}), _TOOLS)
    crafted = can_surface_action(
        PanelAction("x", "hpc-submit", payload={"escalate": "../../physics"}), _TOOLS
    )
    assert benign.allowed == crafted.allowed is False  # payload can't grant anything


# --- partition + safe_actions ------------------------------------------------


def test_govern_panel_partitions_and_exposes_safe_actions():
    actions = [
        PanelAction("show-chart"),  # display-only
        PanelAction("search", "library-search"),  # held
        PanelAction("submit", "gradebook-drafts"),  # held write
        PanelAction("run-hpc", "hpc-submit"),  # unheld
    ]
    v = govern_panel(actions, _TOOLS, scope="chemistry/chem-101")
    assert [a.action.name for a in v.allowed] == ["show-chart", "search", "submit"]
    assert [a.action.name for a in v.denied] == ["run-hpc"]
    assert [a.name for a in v.safe_actions()] == ["show-chart", "search", "submit"]
    assert v.scope == "chemistry/chem-101"


def test_empty_panel_is_all_allowed_vacuously():
    v = govern_panel([], _TOOLS)
    assert v.allowed == () and v.denied == ()
    assert v.safe_actions() == ()


# --- composes with the disposed agent (#118) --------------------------------


def test_composes_with_a_disposed_agent_tool_set():
    # a panel is rendered FOR a disposed/clamped agent; the governor takes its effective tools
    from agate.drafting import dispose_draft
    from agate.tags import SessionTags

    author = SessionTags(
        affiliation="faculty", tenant="uni", courses=(), tier="mid", role="member",
        scope="chemistry",
    )
    draft = {
        "agent": "ta", "description": "d", "role": "ta", "scope": "chemistry/chem-101",
        "reasoning": "lit-review", "tools": ["course-materials-reader", "gradebook-drafts"],
    }
    out = dispose_draft(draft, author, subject="prof")
    assert out.ok
    tools = out.instance.spec.tools
    v = govern_panel(
        [
            PanelAction("read", "course-materials-reader"),
            PanelAction("draft", "gradebook-drafts"),
            PanelAction("search", "library-search"),  # NOT in this agent's tools
        ],
        tools,
    )
    assert {a.action.name for a in v.allowed} == {"read", "draft"}
    assert [a.action.name for a in v.denied] == ["search"]
