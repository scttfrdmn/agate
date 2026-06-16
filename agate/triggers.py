"""Triggered + durable runs — the bounded fire-time core (#115, vision §6).

An agent earns its name by working **unattended**: scheduled ("every Monday, summarize new
papers"), event-driven (a dataset lands → profile it), or durable multi-step. The hard
property — §6 and the §10.2 invariant — is that a triggered run is **no more privileged than
the human who authored it**: at fire time it assumes `delegate(author, spec)` (#106), exactly
the credential the author would get spawning it by hand. And **NO CLOCKS**: triggers are
per-event (EventBridge Scheduler / event rules / Step Functions), never an idle daemon.

This module is PURE and AWS-free, like `agentcompile`/`graph`/`delegate`:
  * `compile_triggers` turns each classified `TriggerSpec` into a `TriggerBinding` — the
    descriptor the deferred deploy phase maps to a Scheduler schedule / event rule / S3
    notification (folds into #136). It carries no idling resource: the only binding kinds are
    `schedule` and `event`, both per-event, so NO CLOCKS is structural, not a runtime check.
  * `plan_triggered_run` derives the bounded run for ONE fire: the author-narrowed credential
    (`delegate`), the `ActingAs` OBO record (#137, author = on-behalf-of — NEVER the event
    payload), and the budget cascade node-list (#81). Fail-closed: a spec scope disjoint from
    the author's refuses to bind rather than firing over-broad.
  * `gate_triggered_run` is the fire-time call gate — a thin pass-through to the existing
    `cost.precall.evaluate_cascade`, so an unattended run is held to the SAME budget the
    author hits interactively. Durable multi-step needs no special state here: the run plan
    is deterministic and re-evaluable, which is what lets Step Functions resume it across
    hours with no always-on component.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from cost.precall import CascadeResult, evaluate_cascade

from agate.agentspec import AgentSpec, TriggerKind
from agate.delegate import DelegationError, delegate
from agate.identity import ActingAs, agent_id
from agate.tags import SessionTags, role_session_name


class TriggerError(ValueError):
    """A trigger that cannot be bound safely — e.g. a spec whose scope is disjoint from the
    author's, which would fire over-broad. Fail closed: refuse to bind."""


@dataclass(frozen=True, slots=True)
class TriggerBinding:
    """The deploy descriptor for ONE trigger (pure data; the live wiring is deferred to
    #136). `kind` is `schedule` (→ EventBridge Scheduler) or `event` (→ an EventBridge rule
    / S3 notification); `expression` is the cron/rate expr or the event source; `handler` is
    the spec's `then`; `agent` is the stable `{tenant}/{name}` id (#137) this fires.

    There is intentionally no field that implies a standing/idle resource — both kinds are
    per-event, so a binding can never describe a clock (NO CLOCKS, §10)."""

    kind: TriggerKind
    expression: str
    handler: str
    agent: str


def compile_triggers(spec: AgentSpec, *, tenant: str) -> tuple[TriggerBinding, ...]:
    """Build the `TriggerBinding`s for a spec under a (verified, or placeholder) tenant. The
    specs are already classified + validated by `agentspec._parse_triggers`, so this is a
    pure shape map — no re-validation, no AWS. The deferred deploy phase consumes these."""
    aid = agent_id(tenant, spec.name)
    return tuple(
        TriggerBinding(kind=t.kind, expression=t.detail, handler=t.then, agent=aid)
        for t in spec.triggers
    )


@dataclass(frozen=True, slots=True)
class TriggeredRun:
    """The bounded plan for ONE fire of a trigger. Everything an executor needs to run the
    handler with no more authority than the author had interactively:

    `child_tags`  — the author-narrowed credential (`delegate(author, spec)`).
    `acting_as`   — the OBO record (#137): agent X · on behalf of the AUTHOR · within remit.
    `handler`     — the spec's `then` (which action to run).
    `kind`/`source` — what fired it (schedule expr / event source) — DATA, not identity.
    `cascade`     — the `(label, spend, budget)` node-list for `evaluate_cascade` (#81), the
                    author's own budget rows, so the run can't out-spend its author.
    """

    child_tags: SessionTags
    acting_as: ActingAs
    handler: str
    kind: TriggerKind
    source: str
    cascade: tuple[tuple[str, float, float | None], ...] = field(default_factory=tuple)


def plan_triggered_run(
    compiled,  # noqa: ANN001 — an agentcompile.CompiledAgent (loose to avoid an import cycle)
    author_tags: SessionTags,
    *,
    subject: str,
    binding: TriggerBinding,
    spend_lookup=None,
) -> TriggeredRun:
    """Plan one fire of `binding` for the agent `compiled`, authored by the VERIFIED
    `author_tags`/`subject`. The security crux of #115:

    * The credential is `delegate(author_tags, compiled.spec)` — the unattended run's
      authority is the AUTHOR's verified authority ∩ the spec, never broader than what the
      author could do interactively (§6/§10.2). A disjoint scope raises `TriggerError`
      (refuse to bind, never fire over-broad).
    * The OBO user is the AUTHOR, recovered from the bound `<tenant>@<subject>` session name
      (#79/#137) — there is NO event-identity parameter, so an event payload (what file
      landed, who submitted) can never set who the run acts as. The event is trigger DATA.
    * `cascade` is the author's budget node-list (the `graph.cascade_nodes` shape), so
      `gate_triggered_run` holds the run to the author's budget.

    `spend_lookup(label, index) -> (spend, budget|None)` is injected (live numbers from the
    spend table; tests fake it). When None, the cascade is empty (no caps) — the executor
    supplies it at fire time. Pure: no STS/DDB.
    """
    from agate.agentcompile import acting_as  # lazy: agentcompile imports this module

    try:
        child_tags = delegate(author_tags, compiled.spec, subject=subject)
    except DelegationError as exc:  # disjoint scope etc. — fail closed
        raise TriggerError(
            f"cannot bind trigger for agent {compiled.spec.name!r}: {exc}"
        ) from exc

    session_name = role_session_name(child_tags.tenant, subject)
    record = acting_as(compiled, session_name=session_name)

    cascade: tuple[tuple[str, float, float | None], ...] = ()
    if spend_lookup is not None:
        # The author's own budget chain — broad→specific, the same nodes the interactive
        # cascade gates on (#81). The label set is the author's scope path; here we use the
        # child's tenant + scope as the single authoritative node the executor seeds, and
        # the executor may extend it. Keep it minimal + deterministic.
        labels = [child_tags.tenant, child_tags.scope or child_tags.tenant]
        rows: list[tuple[str, float, float | None]] = []
        for i, label in enumerate(labels):
            spend, budget = spend_lookup(label, i)
            rows.append((label, spend, budget))
        cascade = tuple(rows)

    return TriggeredRun(
        child_tags=child_tags,
        acting_as=record,
        handler=binding.handler,
        kind=binding.kind,
        source=binding.expression,
        cascade=cascade,
    )


def gate_triggered_run(
    run: TriggeredRun,
    *,
    model_id: str,
    input_tokens: int,
    max_tokens: int,
) -> CascadeResult:
    """The fire-time call gate: a thin pass-through to `evaluate_cascade` over the run's
    cascade (#81). An unattended call is allowed only if it fits under every one of the
    AUTHOR's budget nodes — identical to the interactive gate. An empty cascade (no caps
    seeded) allows, exactly as `evaluate_cascade` treats `nodes=[]`."""
    return evaluate_cascade(
        model_id=model_id,
        input_tokens=input_tokens,
        max_tokens=max_tokens,
        nodes=list(run.cascade),
    )
