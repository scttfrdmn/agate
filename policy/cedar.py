"""Cedar policy generation (design §8, §13.4) — the human-auditable governance layer.

For the agent path these policies are loaded into **AgentCore Policy**, which
enforces them natively on every tool/action call; for the chat path Cedar is the
readable mirror of the IAM scope (design §5: "this mapping lives in Cedar … not in
code branches"). Both are generated from the SAME `agg.entitlements` table that
drives the IAM model-access policy, so the auditable layer and the enforced layer
cannot drift.

Pure text generation, AWS-free and unit-tested. The CDK governance stack loads the
output into a `CfnPolicy`.
"""

from __future__ import annotations

from agg.entitlements import TIERS, models_for_tier

# Cedar entity/action names (§13.4). Kept as constants so the schema and policies
# agree and a rename stays in one place.
ACTION_INVOKE = 'Action::"InvokeModel"'
ACTION_RETRIEVE = 'Action::"Retrieve"'
ACTION_CALL_TOOL = 'Action::"CallTool"'


def _comment(text: str) -> str:
    return f"// {text}"


def model_invoke_policies() -> str:
    """Permit InvokeModel when the resource's tier matches the principal's tier and
    tenant — the Cedar mirror of the IAM model-access policy (§13.2/§13.4).

    One permit per tier keeps the policy readable and lets a reviewer see exactly
    which models each tier reaches; the model set per tier comes straight from the
    entitlement table (cumulative, lower tiers included).
    """
    blocks = [
        _comment("Generated from agg.entitlements — model access mirrors the IAM scope."),
    ]
    for tier in TIERS:
        models = ", ".join(f'"{m}"' for m in models_for_tier(tier))
        blocks.append(
            f"// tier {tier}: {len(models_for_tier(tier))} entitled models\n"
            f"permit(\n"
            f"  principal,\n"
            f"  action == {ACTION_INVOKE},\n"
            f"  resource\n"
            f") when {{\n"
            f'  principal.tier == "{tier}" &&\n'
            f"  resource.tier == principal.tier &&\n"
            f"  resource.tenant == principal.tenant &&\n"
            f"  resource.model in [{models}]\n"
            f"}};"
        )
    return "\n\n".join(blocks)


def retrieve_policy() -> str:
    """Permit Retrieve only on an index whose tenant/course the principal carries
    (§13.3/§13.4) — the data-scope mirror."""
    return (
        _comment("Retrieval is confined to the principal's tenant + enrolled courses.")
        + "\n"
        + "permit(\n"
        + "  principal,\n"
        + f"  action == {ACTION_RETRIEVE},\n"
        + "  resource\n"
        + ") when {\n"
        + "  resource.index_tenant == principal.tenant &&\n"
        + '  (resource.course == "" || resource.course in principal.courses)\n'
        + "};"
    )


def call_tool_policy() -> str:
    """Permit CallTool only for tools the principal is allowed and within tenant
    (agent path, §13.4) — gates every AgentCore Gateway tool call."""
    return (
        _comment("Agent tool use is gated per user: allowed tool set + tenant match.")
        + "\n"
        + "permit(\n"
        + "  principal,\n"
        + f"  action == {ACTION_CALL_TOOL},\n"
        + "  resource\n"
        + ") when {\n"
        + "  resource.tool in principal.allowed_tools &&\n"
        + "  resource.tenant == principal.tenant\n"
        + "};"
    )


def forbid_cross_tenant() -> str:
    """A defence-in-depth forbid: never permit any action across tenants, even if a
    permit above is mis-scoped. `forbid` wins over `permit` in Cedar."""
    return (
        _comment("Defence in depth: cross-tenant access is forbidden outright.")
        + "\n"
        + "forbid(principal, action, resource)\n"
        + "when { resource has tenant && resource.tenant != principal.tenant };"
    )


def generate_policy_set() -> str:
    """The full Cedar policy text (model access + retrieval + tools + the
    cross-tenant forbid), ready to load into AgentCore Policy."""
    return "\n\n".join(
        [
            model_invoke_policies(),
            retrieve_policy(),
            call_tool_policy(),
            forbid_cross_tenant(),
        ]
    )
