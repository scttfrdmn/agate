"""A2UI — the panel-action governor (#119 slice, vision §8.6).

A2UI is the "beyond just another chatbot" payoff: an agent renders an INTERACTIVE panel (a
live dataset profile, a budget gauge, a clickable citation graph) instead of a wall of text.
The governing rule: rendered components are bounded by the SAME scope — an agent can only
surface data + **actions** its credential permits, so a "live panel" can't become an
exfiltration or privileged-action surface.

The headline, enforced here: **a panel action is INERT until it resolves to a capability the
agent actually holds.** A control an agent emits is just a *proposal* to invoke a capability —
it grants nothing. `govern_panel` strips any action whose capability the agent didn't declare
(denied by absence, the SAME rule as an undeclared tool, #113), and marks a write action as
**draft-bound** (§5 — a panel write lands in a `_drafts/` queue for review, never a live
system, enforced by the #113 `drafts-queue` resource). The actual invocation still goes
through the #113 IAM tool grant at click time — this governor is the UX-layer strip, the IAM
grant is the enforcement (defense in depth).

PURE and AWS-free: a pre-render filter over the agent's EFFECTIVE `tools` (already
skill-expanded by #119 and clamped to the author by #106/#118), reusing the #113 catalog. Per
§0.1, agenkit renders `safe_actions()` + emits a denied event per stripped action; agate
decides what may be rendered.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from agate.agentspec import SpecError, get_capability


@dataclass(frozen=True, slots=True)
class PanelAction:
    """A control an agent wants to surface in a live A2UI panel. `name` is the control's
    id/label; `capability` is the #113 capability it would invoke when clicked (`""` = a
    PURELY DISPLAY control that invokes nothing — inert by construction); `payload` is the
    DATA the control carries (filter params, drill-down coords) — never authority."""

    name: str
    capability: str = ""
    payload: dict = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ActionVerdict:
    """One governance decision for a panel action. `write` is True when the backing capability
    is a draft-write (so the renderer can label it 'draft for review, never live', §5)."""

    action: PanelAction
    allowed: bool
    reason: str
    write: bool = False

    def to_dict(self) -> dict:
        return {
            "name": self.action.name,
            "capability": self.action.capability,
            "allowed": self.allowed,
            "reason": self.reason,
            "write": self.write,
        }


@dataclass(frozen=True, slots=True)
class PanelVerdict:
    """The governed panel: actions partitioned into `allowed` / `denied`. `safe_actions()`
    is what agenkit renders; the `denied` verdicts become `policy_denied`/`action_denied`
    events. `scope` is the agent's scope the panel is bounded to (audit/provenance)."""

    allowed: tuple[ActionVerdict, ...]
    denied: tuple[ActionVerdict, ...]
    scope: str = ""

    def safe_actions(self) -> tuple[PanelAction, ...]:
        """Only the admitted actions — the controls the renderer may surface."""
        return tuple(v.action for v in self.allowed)


def can_surface_action(action: PanelAction, tools: tuple[str, ...]) -> ActionVerdict:
    """Decide whether one panel action may be surfaced by an agent holding `tools` (its
    EFFECTIVE, skill-expanded, author-clamped capability set). Fail-closed:

      * a display-only action (no `capability`) → allowed (inert; surfaces no authority);
      * an action whose capability is NOT in `tools` → denied (by absence, the #113 rule);
      * an unknown capability (not in the #113 catalog) → denied (caught, never surfaced);
      * an action whose capability the agent holds → allowed, `write` from the grant (§5).
    """
    cap = action.capability
    if not cap:
        return ActionVerdict(action, allowed=True, reason="display-only (no capability)")
    if cap not in tools:
        return ActionVerdict(
            action, allowed=False,
            reason=f"agent does not hold capability {cap!r} (denied by absence)",
        )
    try:
        capability = get_capability(cap)  # validates against the #113 catalog
    except SpecError:
        # An agent's tools are validated at parse, so this is belt-and-suspenders: a
        # capability that isn't in the catalog can never be surfaced.
        return ActionVerdict(
            action, allowed=False, reason=f"unknown capability {cap!r} (not in catalog)"
        )
    return ActionVerdict(
        action, allowed=True, reason="within the agent's held capabilities",
        write=capability.grant.write,
    )


def govern_panel(
    actions: tuple[PanelAction, ...] | list[PanelAction],
    tools: tuple[str, ...],
    *,
    scope: str = "",
) -> PanelVerdict:
    """Govern a proposed A2UI panel: partition its actions into allowed/denied by whether
    each resolves to a capability the agent holds (`can_surface_action`). The renderer
    surfaces only `safe_actions()`; every denied action is attributed for a denied event. A
    panel can thus surface only the actions the agent's credential already permits — it can't
    become a privileged-action surface. Pure: no AWS, no invocation (the click still hits the
    #113 IAM grant)."""
    allowed: list[ActionVerdict] = []
    denied: list[ActionVerdict] = []
    for action in actions:
        verdict = can_surface_action(action, tuple(tools))
        (allowed if verdict.allowed else denied).append(verdict)
    return PanelVerdict(allowed=tuple(allowed), denied=tuple(denied), scope=scope)
