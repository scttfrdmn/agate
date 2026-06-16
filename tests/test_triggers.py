"""Unit tests for triggered + durable runs — the bounded fire-time core (#115). No AWS.

The headline invariant (§6 / §10.2): a triggered/unattended run is `delegate(author, spec)`
— bounded by the human who authored it, attributed to the author (OBO, NEVER the event), and
gated by the author's budget. And NO CLOCKS: only per-event binding kinds exist.
"""

from __future__ import annotations

import pytest
from agate.agentcompile import compile_agent
from agate.agentspec import _TRIGGER_KINDS, parse_spec
from agate.tags import SessionTags
from agate.triggers import (
    TriggerError,
    compile_triggers,
    gate_triggered_run,
    plan_triggered_run,
)

# A cheap, real model id so the pricebook resolves a rate (worst-case pricing is deterministic).
_MODEL = "anthropic.claude-3-haiku-20240307-v1:0"


def _spec(scope="chemistry/chem-101", triggers=None, **over):
    d = {
        "agent": "paper-sweep", "description": "d", "role": "researcher",
        "scope": scope, "reasoning": "lit-review",
        "triggers": triggers or [{"on": "schedule:cron(0 9 ? * MON *)", "then": "summarize"}],
    }
    d.update(over)
    return parse_spec(d)


def _author(scope="chemistry", tier="frontier", tenant="chem"):
    return SessionTags(
        affiliation="faculty", tenant=tenant, courses=(), tier=tier, role="member", scope=scope
    )


# --- classification (delegated to the parser, asserted here end-to-end) ------


def test_schedule_and_event_classify():
    c = compile_agent(
        _spec(triggers=[
            {"on": "schedule:rate(1 day)", "then": "summarize"},
            {"on": "event:s3.object-created", "then": "profile"},
        ])
    )
    kinds = {b.kind: b for b in c.trigger_bindings}
    assert kinds["schedule"].expression == "rate(1 day)"
    assert kinds["event"].expression == "s3.object-created"


def test_bad_kind_and_bad_schedule_fail_closed():
    from agate.agentspec import SpecError

    with pytest.raises(SpecError):
        _spec(triggers=[{"on": "poll:every-second", "then": "x"}])
    with pytest.raises(SpecError):
        _spec(triggers=[{"on": "schedule:whenever", "then": "x"}])
    with pytest.raises(SpecError):
        _spec(triggers=[{"on": "event:s3.created"}])  # missing 'then'


# --- the headline: least-privilege, bounded by the author --------------------


def test_run_credential_is_delegate_of_author():
    c = compile_agent(_spec(scope="chemistry/chem-101"))
    run = plan_triggered_run(c, _author(scope="chemistry"), subject="prof",
                             binding=c.trigger_bindings[0])
    # narrowed to the spec's deeper scope; tier = min(author, spec)
    assert run.child_tags.scope == "chemistry/chem-101"
    assert run.child_tags.tenant == "chem"
    assert run.child_tags.role == "member"  # an agent is never admin


def test_run_tier_is_min_of_author_and_spec():
    c = compile_agent(_spec(role="ta"))  # ta -> oss tier
    run = plan_triggered_run(c, _author(tier="frontier"), subject="prof",
                             binding=c.trigger_bindings[0])
    assert run.child_tags.tier == "oss"  # min(frontier, oss)


def test_disjoint_scope_refuses_to_bind():
    # An author whose scope is disjoint from the spec must NOT fire over-broad.
    c = compile_agent(_spec(scope="chemistry/chem-101"))
    with pytest.raises(TriggerError):
        plan_triggered_run(c, _author(scope="physics"), subject="prof",
                           binding=c.trigger_bindings[0])


# --- OBO is the author, never the event --------------------------------------


def test_obo_user_is_the_author():
    c = compile_agent(_spec())
    run = plan_triggered_run(c, _author(tenant="chem"), subject="prof",
                             binding=c.trigger_bindings[0])
    assert run.acting_as.on_behalf_of == "chem@prof"
    assert run.acting_as.attributed is True
    assert run.acting_as.agent == "chem/paper-sweep"  # tenant from the author session


def test_event_payload_cannot_set_the_obo_user():
    # plan_triggered_run has no event-identity parameter. Even if a caller had an
    # attacker-shaped payload, there is nowhere to inject it — the OBO user comes solely
    # from the author's (subject, tenant). This documents that by signature.
    c = compile_agent(_spec())
    run = plan_triggered_run(c, _author(tenant="chem"), subject="prof",
                             binding=c.trigger_bindings[0])
    # The binding/source carries DATA (what fired), not identity.
    assert run.source == "cron(0 9 ? * MON *)"
    assert "@" not in run.source  # no identity smuggled through the trigger expression
    assert run.acting_as.subject == "prof"


# --- budget = the author's ---------------------------------------------------


def test_gate_rejects_when_author_budget_exceeded():
    c = compile_agent(_spec(scope="chemistry/chem-101"))
    # Seed the author's scope node at its cap so any call is rejected.
    run = plan_triggered_run(
        c, _author(scope="chemistry"), subject="prof", binding=c.trigger_bindings[0],
        spend_lookup=lambda label, i: (100.0, 1.0) if "chem-101" in label else (0.0, None),
    )
    res = gate_triggered_run(run, model_id=_MODEL, input_tokens=1000, max_tokens=1000)
    assert res.decision == "reject"
    assert res.breaching_node == "chemistry/chem-101"


def test_gate_allows_within_budget():
    c = compile_agent(_spec())
    run = plan_triggered_run(
        c, _author(), subject="prof", binding=c.trigger_bindings[0],
        spend_lookup=lambda label, i: (0.0, 1000.0),
    )
    res = gate_triggered_run(run, model_id=_MODEL, input_tokens=10, max_tokens=10)
    assert res.decision == "allow"


def test_no_cascade_seeded_allows():
    c = compile_agent(_spec())
    run = plan_triggered_run(c, _author(), subject="prof", binding=c.trigger_bindings[0])
    assert run.cascade == ()
    res = gate_triggered_run(run, model_id=_MODEL, input_tokens=10, max_tokens=10)
    assert res.decision == "allow"  # empty nodes -> no cap


# --- NO CLOCKS is structural -------------------------------------------------


def test_only_per_event_binding_kinds_exist():
    # The producible binding kinds are EXACTLY {schedule, event} — both per-event. There is
    # no `poll`/`daemon`/standing kind a spec can declare, so a binding can never be a clock.
    c = compile_agent(
        _spec(triggers=[
            {"on": "schedule:rate(1 day)", "then": "a"},
            {"on": "event:s3.object-created", "then": "b"},
        ])
    )
    produced = {b.kind for b in c.trigger_bindings}
    assert produced <= _TRIGGER_KINDS == {"schedule", "event"}


def test_compile_triggers_binds_tenant_into_agent_id():
    spec = _spec()
    bindings = compile_triggers(spec, tenant="chem")
    assert bindings[0].agent == "chem/paper-sweep"
