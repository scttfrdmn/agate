"""Agent-spec compiler — spec → scoped identity (#105, §1). The keystone.

Takes a validated `AgentSpec` (`agate.agentspec`, #104) and compiles it into the
artifacts that bound the agent: a `SessionTags` template, the model-access + data-scope
+ tool IAM policies, budget-row templates in the cascade key shapes, and a dispatch
payload for the reasoning construct. **The spec IS the agent's IAM** — the compiled
policies grant exactly the spec's tier + scope + tools and nothing broader.

Pure and AWS-free, exactly like `policy.generate`: it PRODUCES policy JSON / tag
templates / budget descriptors; it never calls STS or DynamoDB. It deliberately
COMPOSES the existing primitives rather than duplicating them:
  * `policy.generate.model_access_policy` / `data_scope_policy` / `agent_tool_policy`,
  * `agate.patterns.compile_pattern` for the reasoning payload,
  * `agate.budget` key-shape builders + `BudgetWrite` for the budget templates,
  * `agate.tags.SessionTags` for the credential template.

What is DEFERRED to bounded delegation (#106): filling the real tenant/subject into the
tags template, the actual `sts:AssumeRole`, and authorizing the budget rows under the
spawner via `budget.plan_budget_write`. The compiler emits the *templates* those steps
narrow + authorize. Triggers are emitted as shape-only descriptors (wiring is §6).
"""

from __future__ import annotations

from dataclasses import dataclass

from agate.agentspec import AgentSpec, get_capability
from agate.budget import BudgetWrite, _scope_pk, _tenant_pk, _user_pk
from agate.entitlements import models_for_tier
from agate.patterns import compile_pattern
from agate.tags import ROLE_MEMBER, SessionTags

# Placeholders the compiler stamps where a value is only known at SPAWN time (filled by
# bounded delegation #106 from the verified spawner/invoker). The braces make them
# collision-proof: `budget._clean_id` strips `{}` from any real tenant/user id, so a
# real key can never equal a template key — an un-narrowed template can't be mistaken
# for (or collide with) a real grant. #106 fills these BEFORE any DynamoDB write via
# `budget.plan_budget_write`; the compiler itself never touches DynamoDB.
_TENANT_PLACEHOLDER = "{tenant}"
_PERIOD_PLACEHOLDER = "{period}"


@dataclass(frozen=True, slots=True)
class CompiledAgent:
    """Everything the spec compiles to. All artifacts are templates/descriptors —
    pure data, no AWS calls have happened."""

    spec: AgentSpec
    tags_template: SessionTags
    model_access_policy: dict
    data_scope_policy: dict
    tool_policy: dict
    budget_rows: tuple[BudgetWrite, ...]
    dispatch_payload: dict
    triggers: tuple[dict, ...]


def _tool_grants(spec: AgentSpec) -> list[dict]:
    """Resolve the spec's declared tools to the grant dicts `agent_tool_policy` wants.
    Capability.name → its CapabilityGrant; a stable Sid per tool. Undeclared tools
    simply aren't here → the policy emits no Allow for them (denied by absence)."""
    grants: list[dict] = []
    for name in spec.tools:
        cap = get_capability(name)  # spec already validated these, but stay defensive
        sid = "Tool" + "".join(part.capitalize() for part in name.replace("_", "-").split("-"))
        grants.append(
            {
                "sid": sid,
                "actions": cap.grant.actions,
                "resource_kind": cap.grant.resource_kind,
                "write": cap.grant.write,
            }
        )
    return grants


def _budget_rows(spec: AgentSpec) -> tuple[BudgetWrite, ...]:
    """Budget-row TEMPLATES in the cascade key shapes (#81). tenant/period are
    placeholders filled at spawn; `budget.plan_budget_write` does the real authorization
    then. `per` picks the key shape: scope → the agent's own scope node; user/student →
    a per-user row; tenant → the tenant rollup."""
    b = spec.budget
    if b is None:
        return ()
    tenant, period = _TENANT_PLACEHOLDER, _PERIOD_PLACEHOLDER
    if b.per == "scope":
        node = spec.scope or ""  # a scope budget needs a node; "" = tenant-wide fallback
        pk = _scope_pk(tenant, node, period)
        return (BudgetWrite(pk=pk, budget_usd=b.usd, tenant=tenant, period=period, scope=node),)
    if b.per in ("user", "student"):
        # Per-invoker cap: the user id is filled per invoker at spawn (#106).
        pk = _user_pk(tenant, "{user}", period)
        return (BudgetWrite(pk=pk, budget_usd=b.usd, tenant=tenant, period=period, user="{user}"),)
    # tenant
    return (
        BudgetWrite(pk=_tenant_pk(tenant, period), budget_usd=b.usd, tenant=tenant, period=period),
    )


def compile_agent(
    spec: AgentSpec,
    *,
    region: str = "*",
    account: str = "",
    question: str = "",
    bucket: str | None = None,
    gateway_arn: str | None = None,
) -> CompiledAgent:
    """Compile a validated `AgentSpec` into its bounding artifacts. Pure: no STS, no
    DynamoDB — see the module docstring for what #106 fills in at spawn."""
    from policy.generate import agent_tool_policy, data_scope_policy, model_access_policy

    tier = spec.tier

    # Credential TEMPLATE. tier + scope are fixed by the spec; tenant/affiliation/courses
    # are placeholders the spawner fills (#106). The scope tag is what confines data +
    # tool reads to the agent's subtree once it carries a real tenant.
    tags_template = SessionTags(
        affiliation="student",  # placeholder; the invoker's real affiliation fills at spawn
        tenant=_TENANT_PLACEHOLDER,
        courses=(),
        tier=tier,
        role=ROLE_MEMBER,
        scope=spec.scope,
    )

    # Reasoning payload — straight composition of the existing pattern compiler against
    # the tier's entitled models, so `agent_dispatch.dispatch` runs it unchanged. A
    # pattern role can never pick a model above the agent's tier (the candidate list IS
    # models_for_tier(tier)).
    dispatch_payload = compile_pattern(
        spec.reasoning, question=question, entitled_models=models_for_tier(tier)
    )

    return CompiledAgent(
        spec=spec,
        tags_template=tags_template,
        model_access_policy=model_access_policy(region=region, account=account),
        data_scope_policy=data_scope_policy(bucket=bucket),
        tool_policy=agent_tool_policy(_tool_grants(spec), bucket=bucket, gateway_arn=gateway_arn),
        budget_rows=_budget_rows(spec),
        dispatch_payload=dispatch_payload,
        triggers=tuple({"on": t.on, "then": t.then} for t in spec.triggers),
    )
