"""Agent spec — a declarative agent that compiles to a scoped identity (#104, §1).

This is the keystone of the agent-platform vision (`docs/agate-agents-vision.md` §1):
an **agent is a declarative artifact** (`*.agate.yaml`) that the compiler
(`agate.agentcompile`, #105) turns into a scoped credential + tool policy + budget
rows + a reasoning payload. The spec IS the agent's IAM, so a compiled agent cannot
exceed it.

This module is the SCHEMA + PARSER half: frozen dataclasses + fail-closed validation,
pure and AWS-free (no boto3) exactly like `agate.tags`/`agate.entitlements`, so it is
fully unit-testable. It deliberately generalizes `agate.patterns.Pattern` (a reviewed
declarative reasoning config) into the richer agent spec — the `reasoning` field IS a
`patterns.Pattern` (or a registry key for one), so nothing about reasoning is reinvented.

Fail-closed is the rule everywhere: an unknown key, an over-broad/garbled field, an
unknown tool, or a malformed budget is REJECTED (`SpecError`), never silently widened.
Tools are denied by absence — the compiler only emits a grant for a capability the spec
explicitly lists, and a tool not in the capability catalog can't be listed at all.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Literal

from agate.budget import normalise_scope
from agate.entitlements import Affiliation, Tier, derive_tier
from agate.patterns import Pattern, PatternError, Role
from agate.patterns import get as pattern_get
from agate.skills import SkillError, get_skill, skill_capabilities


class SpecError(ValueError):
    """A malformed or over-broad agent spec. Fail closed — never silently widened."""


# --- role -> tier -----------------------------------------------------------
# The spec's `role` vocabulary (what a human writes in the YAML) folded onto the
# entitlement affiliation set, then through the single-source-of-truth tier derivation
# (`entitlements.derive_tier`). An unrecognised role falls to the oss floor — least
# privilege, never raise-then-widen. `grant: true` (a researcher with funding) reaches
# frontier, mirroring derive_tier's grant path.
_ROLE_AFFILIATION: dict[str, Affiliation] = {
    "student": "student",
    "ta": "student",  # a TA is entitlement-wise a student unless granted
    "learner": "student",
    "instructor": "faculty",
    "faculty": "faculty",
    "professor": "faculty",
    "staff": "staff",
    "researcher": "researcher",
    "pi": "researcher",
}


def role_to_tier(role: str) -> Tier:
    """Resolve a spec `role` label to a model tier via the affiliation→tier map.

    Unknown role → oss floor (least privilege). There is deliberately NO `grant`
    escalation here: a spec cannot self-assert frontier access. Tier is bounded by
    the declared role, and any further promotion is a property of the VERIFIED
    spawner's credential applied at spawn time (#106) — never claimed by the artifact
    itself (the same "authority is the credential, never client-claimed" rule the
    broker enforces). A funded researcher's agent reaches frontier via `role:
    researcher`, not a flag."""
    affiliation = _ROLE_AFFILIATION.get((role or "").strip().lower())
    return derive_tier(affiliation)


# --- capability catalog -----------------------------------------------------
# The tools an agent may declare. Mirrors `patterns._REGISTRY`: a reviewed, in-code
# registry. Each capability carries the GRANT it expands to — a typed descriptor, not
# free-form policy, so the compiler stays the only thing that emits IAM JSON. An agent
# can only list a capability that exists here; the compiler emits a grant ONLY for the
# capabilities the spec lists (undeclared = denied by absence).

ResourceKind = Literal["docs-scope", "drafts-queue", "vector-read", "gateway-tool"]


@dataclass(frozen=True, slots=True)
class CapabilityGrant:
    """What IAM scope a capability expands to. The compiler interpolates the tenant/
    scope principal tags into the resources, so a tool can never widen reach. A `write`
    capability targets a DRAFT path (never a live system) — the vision §5 rule."""

    actions: tuple[str, ...]
    resource_kind: ResourceKind
    write: bool = False


@dataclass(frozen=True, slots=True)
class Capability:
    """A reviewed tool an agent may be granted. `name` is what the spec lists."""

    name: str
    title: str
    grant: CapabilityGrant


_CAPABILITIES: dict[str, Capability] = {}


def register_capability(cap: Capability) -> Capability:
    if cap.name in _CAPABILITIES:
        raise SpecError(f"duplicate capability: {cap.name!r}")
    _CAPABILITIES[cap.name] = cap
    return cap


def get_capability(name: str) -> Capability:
    try:
        return _CAPABILITIES[name]
    except KeyError as exc:
        raise SpecError(f"unknown tool/capability: {name!r}") from exc


def capability_catalog() -> list[dict[str, str]]:
    """The selectable capabilities, for an authoring UI (name/title/write)."""
    return [
        {"name": c.name, "title": c.title, "write": str(c.grant.write).lower()}
        for c in _CAPABILITIES.values()
    ]


# Two reference capabilities matching the vision §1 YAML, proving the pattern.
register_capability(
    Capability(
        name="course-materials-reader",
        title="Read course/scope documents (read-only)",
        grant=CapabilityGrant(actions=("s3:GetObject",), resource_kind="docs-scope"),
    )
)
register_capability(
    Capability(
        name="gradebook-drafts",
        title="Write feedback to a draft queue (instructor approves before it goes live)",
        grant=CapabilityGrant(
            actions=("s3:PutObject",), resource_kind="drafts-queue", write=True
        ),
    )
)

# --- Campus MCP tools (#113/#114) -------------------------------------------
# Real campus systems as first-class tools, each reached via AgentCore Gateway. The
# `gateway-tool` grant fences WHICH tools an agent may invoke (IAM, via the Gateway invoke
# action on the tool's ARN — an undeclared tool is denied by absence). The tool's EFFECT
# is bounded by the agent's `agate:scope` + the budget cascade (#81 — a write/submit) +
# user-delegated OAuth (the agent acts AS the verified user, so the source's own ACL
# composes with agate's scope). This is the §5 split: IAM = which, scope/OAuth = effect.
# (These are the action plane; content systems that ingest INTO agate are connectors,
# governed by the #80/#84 data fence — a separate concern, #133.)
_GATEWAY_INVOKE = ("bedrock-agentcore:InvokeGateway",)

register_capability(
    Capability(
        name="library-search",
        title="Search the library catalog / discovery service (read-only)",
        grant=CapabilityGrant(actions=_GATEWAY_INVOKE, resource_kind="gateway-tool"),
    )
)
register_capability(
    Capability(
        name="lms-read",
        title="Read LMS roster/assignments (read-only; the draft-feedback write is "
        "the separate gradebook-drafts capability)",
        grant=CapabilityGrant(actions=_GATEWAY_INVOKE, resource_kind="gateway-tool"),
    )
)
register_capability(
    Capability(
        name="sis-self-read",
        title="Read the caller's OWN student records from the SIS (read-only)",
        grant=CapabilityGrant(actions=_GATEWAY_INVOKE, resource_kind="gateway-tool"),
    )
)
register_capability(
    Capability(
        name="hpc-submit",
        title="Submit an HPC job to the scheduler (the flagship 'agent that acts'); the "
        "submit is gated at run time on the caller's scope/allocation + budget cascade",
        grant=CapabilityGrant(actions=_GATEWAY_INVOKE, resource_kind="gateway-tool", write=True),
    )
)
register_capability(
    Capability(
        name="hpc-monitor",
        title="Read HPC job status / summarise the caller's own jobs (read-only)",
        grant=CapabilityGrant(actions=_GATEWAY_INVOKE, resource_kind="gateway-tool"),
    )
)


# --- spec field types -------------------------------------------------------

BudgetPer = Literal["student", "user", "scope", "tenant"]
PeriodKind = Literal["term", "month"]
MemoryKind = Literal["none", "per-invoker", "personal", "shared"]
Visibility = Literal["private", "course", "tenant"]
InvokerKind = Literal["roster", "scope", "tenant"]
# How an unattended run fires (#115). DELIBERATELY only two kinds, both per-event:
# `schedule` (EventBridge Scheduler cron/rate) and `event` (an EventBridge/S3 source).
# There is no `poll`/`daemon` kind — the NO-CLOCKS invariant is structural in the grammar,
# not a runtime check: nothing a spec can declare implies a standing/idle component.
TriggerKind = Literal["schedule", "event"]

ReasoningRef = str | Pattern

_MEMORY_KINDS: frozenset[str] = frozenset(("none", "per-invoker", "personal", "shared"))
_VISIBILITIES: frozenset[str] = frozenset(("private", "course", "tenant"))
_BUDGET_PER: frozenset[str] = frozenset(("student", "user", "scope", "tenant"))
_PERIOD_KINDS: frozenset[str] = frozenset(("term", "month"))
_INVOKER_KINDS: frozenset[str] = frozenset(("roster", "scope", "tenant"))
_TRIGGER_KINDS: frozenset[str] = frozenset(("schedule", "event"))

# "$20 / student / term"  ->  (20, student, term). Also accepts no leading $.
_BUDGET_RE = re.compile(r"^\$?\s*([0-9]+(?:\.[0-9]+)?)\s*/\s*(\w+)\s*/\s*(\w+)$")


@dataclass(frozen=True, slots=True)
class BudgetSpec:
    usd: float
    per: BudgetPer
    period_kind: PeriodKind


@dataclass(frozen=True, slots=True)
class InvokerSpec:
    kind: InvokerKind
    ref: str  # e.g. the course id for kind="roster" ("" for tenant-wide)


@dataclass(frozen=True, slots=True)
class TriggerSpec:
    """A trigger binding — a CLASSIFIED declaration (#115). `on` carries a `kind:detail`
    grammar (like `InvokerSpec`): `schedule:cron(...)` / `schedule:rate(...)` for an
    EventBridge Scheduler schedule, or `event:<source>` (e.g. `event:s3.object-created`) for
    an EventBridge/S3 event source. The actual rule/schedule/Step-Functions wiring is the
    deferred deploy phase (§6); `agate.triggers` turns this into the binding descriptor + the
    bounded fire-time run. `kind`/`detail` are split + validated at parse (fail-closed)."""

    on: str  # the raw `kind:detail`, e.g. "event:lms.assignment-submitted"
    then: str  # an action/handler name resolved by the triggers phase
    kind: TriggerKind = "event"  # split from `on` at parse
    detail: str = ""  # the part after `kind:` — the cron/rate expr or the event source


# Graph caps (#111): bounded defaults so an agent graph can't recurse or fan out without
# limit. A spec may lower them; values above these ceilings are rejected (fail-closed).
DEFAULT_MAX_DEPTH = 3
DEFAULT_MAX_FANOUT = 5
_CEILING_MAX_DEPTH = 8
_CEILING_MAX_FANOUT = 8
# Total nodes a single spec tree may contain. parse_spec recurses into ALL children, so
# without a parse-time bound a deep chain stack-overflows and a wide tree (fanout^depth)
# hangs BEFORE the graph builder's caps fire (security review #111). This hard ceiling
# (and the per-call depth guard) makes parsing itself bounded — fail-closed.
_CEILING_TOTAL_NODES = 1024


@dataclass(frozen=True, slots=True)
class AgentSpec:
    """A validated agent definition, ready for the compiler (#105). The `reasoning`
    field is a `patterns.Pattern` (resolved from a registry key or built inline), so
    the reasoning construct is the existing primitive, not a new one."""

    name: str
    description: str
    role: str
    reasoning: Pattern
    scope: str = ""  # a single agate:scope path ("" = tenant-wide, see parse rules)
    tools: tuple[str, ...] = ()  # EFFECTIVE tools: authored `tools` ∪ each skill's bundle
    # Skills declared by the spec (#119): portable capability packages. Kept for audit/UI;
    # their capabilities are expanded into `tools` at parse, so the compiler sees the union
    # and a skill grants exactly its (already-catalogued) capabilities — never more.
    skills: tuple[str, ...] = ()
    memory: MemoryKind = "none"
    budget: BudgetSpec | None = None
    invokers: InvokerSpec | None = None
    triggers: tuple[TriggerSpec, ...] = ()
    visibility: Visibility = "private"
    # Agent graph (#111): sub-agents this node may invoke. Each is a full AgentSpec, so a
    # graph is just a spec whose nodes can themselves be specs — the graph executor
    # delegates each child's credential from this node's (monotonic narrowing, #106).
    agents: tuple[AgentSpec, ...] = ()
    # Caps that bound the graph (no infinite recursion / fan-out bomb), validated at parse.
    max_depth: int = DEFAULT_MAX_DEPTH
    max_fanout: int = DEFAULT_MAX_FANOUT

    @property
    def tier(self) -> Tier:
        return role_to_tier(self.role)


# Keys the spec accepts. An unknown top-level key is rejected (fail closed) so a typo
# can't silently become a no-op on an autonomous agent.
_KNOWN_KEYS: frozenset[str] = frozenset(
    (
        "agent",
        "name",
        "description",
        "role",
        "scope",
        "reasoning",
        "tools",
        "skills",
        "memory",
        "budget",
        "invokers",
        "triggers",
        "visibility",
        "agents",
        "max_depth",
        "max_fanout",
    )
)


def _parse_cap(raw: object, default: int, ceiling: int, name: str) -> int:
    """Parse a graph cap (max_depth/max_fanout): a positive int, default if absent,
    rejected if above the hard ceiling (fail-closed — a spec can lower a cap, never
    raise it past the platform limit)."""
    if raw is None:
        return default
    if not isinstance(raw, int) or isinstance(raw, bool) or raw < 1:
        raise SpecError(f"{name} must be a positive integer")
    if raw > ceiling:
        raise SpecError(f"{name} exceeds the ceiling ({raw} > {ceiling})")
    return raw


def _require_str(data: dict, key: str, *, aliases: tuple[str, ...] = ()) -> str:
    for k in (key, *aliases):
        v = data.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    raise SpecError(f"{key} is required and must be a non-empty string")


def _parse_reasoning(raw: object) -> Pattern:
    """A registry key (str) → the registered Pattern; an inline dict → a built+validated
    Pattern. Anything else → SpecError."""
    if isinstance(raw, str) and raw.strip():
        try:
            return pattern_get(raw.strip())
        except PatternError as exc:
            raise SpecError(str(exc)) from exc
    if isinstance(raw, dict):
        return _inline_pattern(raw)
    if raw is None:
        # Reached when a spec gives no `reasoning` AND no declared skill supplies a pattern
        # (#119). Reasoning stays required — fail closed with a clear message.
        raise SpecError("reasoning is required (a pattern key, an inline pattern, or a skill)")
    raise SpecError("reasoning must be a pattern key or an inline pattern definition")


def _inline_pattern(d: dict) -> Pattern:
    """Build a Pattern from an inline dict and validate its shape (DEBATE needs roles)."""
    mode = d.get("mode")
    if mode not in ("SYNTHESIS", "DEBATE", "ANALYSIS"):
        raise SpecError("inline reasoning.mode must be SYNTHESIS, DEBATE, or ANALYSIS")
    roles = tuple(
        Role(
            label=str(r.get("label", "")),
            system=str(r.get("system", "")),
            model=r.get("model", "balanced"),
            max_tokens=int(r.get("max_tokens", 1024)),
        )
        for r in (d.get("roles") or [])
        if isinstance(r, dict)
    )
    if mode == "DEBATE" and not roles:
        raise SpecError("inline DEBATE reasoning must define at least one role")
    adj = d.get("adjudicator")
    adjudicator = (
        Role(
            label=str(adj.get("label", "adjudicator")),
            system=str(adj.get("system", "")),
            model=adj.get("model", "best"),
            max_tokens=int(adj.get("max_tokens", 1024)),
        )
        if isinstance(adj, dict)
        else None
    )
    return Pattern(
        key=str(d.get("key", "inline")),
        title=str(d.get("title", "inline")),
        description=str(d.get("description", "")),
        mode=mode,
        roles=roles,
        adjudicator=adjudicator,
        review_system=d.get("review_system"),
    )


def _parse_budget(raw: object) -> BudgetSpec:
    """Parse `"$20 / student / term"` or a structured `{usd, per, period}` dict."""
    if isinstance(raw, str):
        m = _BUDGET_RE.match(raw.strip())
        if not m:
            raise SpecError("budget must look like '$20 / student / term'")
        usd_s, per, period = m.group(1), m.group(2).lower(), m.group(3).lower()
        usd = float(usd_s)
    elif isinstance(raw, dict):
        per = str(raw.get("per", "")).lower()
        period = str(raw.get("period", raw.get("period_kind", ""))).lower()
        try:
            usd = float(raw.get("usd"))  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise SpecError("budget.usd must be a number") from exc
    else:
        raise SpecError("budget must be a string or an object")
    if not math.isfinite(usd) or usd < 0:  # mirror budget.py NaN/inf + range guard
        raise SpecError("budget usd must be a finite number >= 0")
    if per not in _BUDGET_PER:
        raise SpecError(f"budget 'per' must be one of {sorted(_BUDGET_PER)}")
    if period not in _PERIOD_KINDS:
        raise SpecError(f"budget period must be one of {sorted(_PERIOD_KINDS)}")
    return BudgetSpec(usd=usd, per=per, period_kind=period)  # type: ignore[arg-type]


def _parse_invokers(raw: object) -> InvokerSpec:
    """`"roster:chem-101"` → InvokerSpec(roster, chem-101). Shape only (resolution
    is the per-invoker / LTI phase)."""
    if not isinstance(raw, str) or not raw.strip():
        raise SpecError("invokers must be a string like 'roster:chem-101'")
    kind, _, ref = raw.strip().partition(":")
    kind = kind.lower()
    if kind not in _INVOKER_KINDS:
        raise SpecError(f"invokers kind must be one of {sorted(_INVOKER_KINDS)}")
    return InvokerSpec(kind=kind, ref=ref)  # type: ignore[arg-type]


def _parse_triggers(raw: object) -> tuple[TriggerSpec, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple)):
        raise SpecError("triggers must be a list")
    out: list[TriggerSpec] = []
    for t in raw:
        if not isinstance(t, dict) or not t.get("on") or not t.get("then"):
            raise SpecError("each trigger must have 'on' and 'then'")
        on = str(t["on"]).strip()
        kind, sep, detail = on.partition(":")
        kind = kind.lower()
        detail = detail.strip()
        if not sep or kind not in _TRIGGER_KINDS:
            # Fail-closed: a typo'd kind on an autonomous agent must NOT silently no-op.
            raise SpecError(
                f"trigger 'on' must be '<kind>:<detail>' with kind in {sorted(_TRIGGER_KINDS)} "
                f"(got {on!r})"
            )
        if not detail:
            raise SpecError(f"trigger {on!r} needs a detail after '{kind}:'")
        if kind == "schedule" and not (
            (detail.startswith("cron(") or detail.startswith("rate(")) and detail.endswith(")")
        ):
            # A schedule must be a complete EventBridge Scheduler expression — never a
            # free-form string that could mean "run continuously" (NO CLOCKS: per-event
            # only). Require the closing ')' too, so `cron(...)<trailing>` fails at parse
            # rather than slipping through to the deploy phase.
            raise SpecError(
                f"schedule trigger must be a cron(...) or rate(...) expression (got {detail!r})"
            )
        out.append(
            TriggerSpec(on=on, then=str(t["then"]), kind=kind, detail=detail)  # type: ignore[arg-type]
        )
    return tuple(out)


def parse_spec(
    data: dict[str, Any], *, _depth: int = 0, _budget: list[int] | None = None
) -> AgentSpec:
    """Validate a spec dict into an AgentSpec. Fail-closed: unknown keys, malformed or
    over-broad fields, unknown tools, and bad budgets all raise SpecError.

    Dict-in keeps the validated core dependency-light (no YAML import); `load_spec` is
    the thin YAML edge.

    `_depth`/`_budget` bound the RECURSION itself (a spec tree's nodes are parsed before
    the graph builder's caps could fire), so a maliciously deep chain can't stack-overflow
    and a maliciously wide tree can't exhaust memory — both are rejected at parse time.
    """
    if _budget is None:
        _budget = [_CEILING_TOTAL_NODES]
    if _depth > _CEILING_MAX_DEPTH:
        raise SpecError(f"spec nesting exceeds maximum depth ({_CEILING_MAX_DEPTH})")
    _budget[0] -= 1
    if _budget[0] < 0:
        raise SpecError(f"spec exceeds the maximum total node count ({_CEILING_TOTAL_NODES})")
    if not isinstance(data, dict):
        raise SpecError("spec must be a mapping")
    unknown = set(data) - _KNOWN_KEYS
    if unknown:
        raise SpecError(f"unknown spec keys: {sorted(unknown)}")

    name = _require_str(data, "name", aliases=("agent",))
    description = _require_str(data, "description")
    role = _require_str(data, "role")

    # Scope: a single path via the tags grammar (rejects `..`). A scope that is GIVEN
    # but garbles to empty is rejected — a malformed scope must NOT silently become
    # tenant-wide on an autonomous agent. Omitting scope entirely = tenant-wide ("").
    raw_scope = data.get("scope")
    if raw_scope is None or (isinstance(raw_scope, str) and not raw_scope.strip()):
        scope = ""
    else:
        scope = normalise_scope(str(raw_scope))
        if not scope:
            raise SpecError("scope did not normalise to a valid path")

    # Skills (#119): portable capability packages. Each declared skill expands to its
    # reviewed capabilities, UNIONED into the effective `tools` set — so a skill grants
    # EXACTLY its (already-catalogued) capabilities and the compiler clamps them like any
    # tool. A skill that names an uncatalogued capability fails closed (validate_skill).
    raw_skills = data.get("skills") or ()
    if not isinstance(raw_skills, (list, tuple)):
        raise SpecError("skills must be a list")
    skills = tuple(str(s) for s in raw_skills)

    raw_tools = data.get("tools") or ()
    if not isinstance(raw_tools, (list, tuple)):
        raise SpecError("tools must be a list")
    authored_tools = tuple(str(t) for t in raw_tools)
    # Effective tools = authored ∪ each skill's capabilities, deduped, order-stable.
    effective: list[str] = list(authored_tools)
    for s in skills:
        try:
            caps = skill_capabilities(s)  # unknown skill / uncatalogued capability -> raises
        except SkillError as exc:  # surface as a SpecError (the spec-parse contract)
            raise SpecError(str(exc)) from exc
        for cap in caps:
            if cap not in effective:
                effective.append(cap)
    tools = tuple(effective)
    for t in tools:
        get_capability(t)  # raises SpecError on an unknown tool (covers authored + expanded)

    # Reasoning: an explicit `reasoning` always wins (the author's choice is authoritative).
    # Only when none is given may a declared skill's `pattern` supply it — fill, never
    # override. With neither, `_parse_reasoning(None)` raises (reasoning stays required).
    if data.get("reasoning") is not None:
        reasoning = _parse_reasoning(data.get("reasoning"))
    else:
        skill_pattern = next(
            (get_skill(s).pattern for s in skills if get_skill(s).pattern is not None), None
        )
        reasoning = _parse_reasoning(skill_pattern)

    memory = str(data.get("memory", "none")).lower()
    if memory not in _MEMORY_KINDS:
        raise SpecError(f"memory must be one of {sorted(_MEMORY_KINDS)}")

    visibility = str(data.get("visibility", "private")).lower()
    if visibility not in _VISIBILITIES:
        raise SpecError(f"visibility must be one of {sorted(_VISIBILITIES)}")

    budget = _parse_budget(data["budget"]) if data.get("budget") is not None else None
    invokers = _parse_invokers(data["invokers"]) if data.get("invokers") is not None else None
    triggers = _parse_triggers(data.get("triggers"))

    max_depth = _parse_cap(
        data.get("max_depth"), DEFAULT_MAX_DEPTH, _CEILING_MAX_DEPTH, "max_depth"
    )
    max_fanout = _parse_cap(
        data.get("max_fanout"), DEFAULT_MAX_FANOUT, _CEILING_MAX_FANOUT, "max_fanout"
    )
    # Agent-graph children (#111): each is a full, recursively-validated AgentSpec. Fanout
    # is capped here; depth is enforced by the graph builder (which knows the whole chain).
    raw_agents = data.get("agents") or ()
    if not isinstance(raw_agents, (list, tuple)):
        raise SpecError("agents must be a list")
    if len(raw_agents) > max_fanout:
        raise SpecError(f"agents exceeds max_fanout ({len(raw_agents)} > {max_fanout})")
    agents = tuple(parse_spec(a, _depth=_depth + 1, _budget=_budget) for a in raw_agents)

    return AgentSpec(
        name=name,
        description=description,
        role=role,
        reasoning=reasoning,
        scope=scope,
        tools=tools,
        skills=skills,
        memory=memory,  # type: ignore[arg-type]
        budget=budget,
        invokers=invokers,
        triggers=triggers,
        visibility=visibility,  # type: ignore[arg-type]
        agents=agents,
        max_depth=max_depth,
        max_fanout=max_fanout,
    )


def load_spec(text: str) -> AgentSpec:
    """Parse a YAML `*.agate.yaml` document into an AgentSpec. Thin edge: pyyaml is an
    optional dependency (not in the core), so import it lazily and fail with a clear
    message if absent — the validated core (`parse_spec`) needs no YAML."""
    try:
        import yaml  # noqa: PLC0415 — optional edge dependency, imported lazily
    except ModuleNotFoundError as exc:  # pragma: no cover - environment-dependent
        raise SpecError("load_spec needs pyyaml installed; pass a dict to parse_spec") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise SpecError(f"invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise SpecError("spec document must be a YAML mapping")
    return parse_spec(data)
