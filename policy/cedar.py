"""Cedar policy generation (design §8, §13.4) — two distinct surfaces.

There are two Cedar layers in agate, generated from the SAME `agate.entitlements`
source so they cannot drift, but enforced in different schemas:

1. **The chat-path IAM mirror** (`model_invoke_policies` / `retrieve_policy` /
   `call_tool_policy` / `forbid_cross_tenant` / `generate_policy_set` /
   `policy_statements`). This is the human-auditable Cedar that *mirrors* the IAM
   model-access scope (design §5: "this mapping lives in Cedar … not in code
   branches"). It uses an ABSTRACT entity model (`Action::"InvokeModel"`,
   `resource.tier`, `principal.tenant`) for review, NOT AgentCore's schema.
   IMPORTANT: AgentCore Policy does NOT accept this — its analyzer rejects the
   abstract `resource` ("constrain to AgentCore::Gateway"). This set is the
   readable mirror only; IAM (#5/#13.2) is what actually enforces the chat path.

2. **The agent-path AgentCore tool policy** (`agentcore_tool_policy_statements`).
   This is real, deployed authorization in **AgentCore Policy's own Cedar schema**,
   enforced natively on every Gateway tool call. AgentCore generates the schema
   from the Gateway's tools; a policy MUST name `AgentCore::Gateway` (a specific
   gateway ARN or the type), a tool action (`AgentCore::Action::"<target>___<tool>"`),
   an authenticated principal type (`AgentCore::IamEntity` / `AgentCore::OAuthUser`),
   and carry a constraining `when` (a bare permit fails "Overly Permissive"). These
   facts were confirmed against the live service (#154). This layer sits UNDER the
   #113 IAM gateway fence as defence in depth (§8).

Pure text generation, AWS-free and unit-tested. The CDK governance stack loads the
output into a `CfnPolicy` per statement.
"""

from __future__ import annotations

from agate.entitlements import TIERS, models_for_tier

# AgentCore Policy's own Cedar entity model (#154, confirmed live). The analyzer
# generates the action set from the bound Gateway's tools; the principal/resource
# entity TYPES below are fixed by the service.
AGENTCORE_PRINCIPAL_IAM = "AgentCore::IamEntity"
AGENTCORE_PRINCIPAL_OAUTH = "AgentCore::OAuthUser"
AGENTCORE_GATEWAY_TYPE = "AgentCore::Gateway"
AGENTCORE_ACTION_TYPE = "AgentCore::Action"
# A tool action's name is "<gateway-target-name>___<tool-name>" (triple underscore),
# e.g. the live agate-slurm target's hpc-submit tool -> "agate-slurm___hpc-submit".
AGENTCORE_TOOL_ACTION_SEP = "___"

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
        _comment("Generated from agate.entitlements — model access mirrors the IAM scope."),
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


def model_invoke_policy_statements() -> list[tuple[str, str]]:
    """The per-tier InvokeModel permits as SEPARATE `(name, statement)` pairs. AgentCore
    `CfnPolicy` holds exactly ONE Cedar statement, so each tier's permit is its own policy
    resource (a multi-statement string is rejected — "unexpected token `permit`/`forbid`")."""
    out: list[tuple[str, str]] = []
    for tier in TIERS:
        models = ", ".join(f'"{m}"' for m in models_for_tier(tier))
        out.append(
            (
                f"invoke-{tier}",
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
                f"}};",
            )
        )
    return out


def policy_statements() -> list[tuple[str, str]]:
    """The chat-path IAM-mirror statements as SEPARATE `(name, statement)` pairs.

    NOTE (#154): this is the human-auditable mirror of the IAM model scope (§13.4), NOT what
    AgentCore Policy enforces — its abstract `resource`/`Action::"InvokeModel"` model is
    rejected by the AgentCore analyzer ("constrain to AgentCore::Gateway"). The agent-path
    policy AgentCore actually enforces is `agentcore_tool_policy_statements()`. IAM (#5/#13.2)
    enforces the chat path; this set is the readable mirror only.

    Each pair is one statement because AgentCore `CfnPolicy` holds exactly one (a multi-statement
    string is rejected — "unexpected token `permit`/`forbid`")."""
    return [
        *model_invoke_policy_statements(),
        ("retrieve", retrieve_policy()),
        ("call-tool", call_tool_policy()),
        ("forbid-cross-tenant", forbid_cross_tenant()),
    ]


def generate_policy_set() -> str:
    """The full Cedar policy text as ONE string (model access + retrieval + tools + the
    cross-tenant forbid) — for the human-auditable mirror / docs. NOTE: AgentCore `CfnPolicy`
    takes one statement, so the deploy stack loads `policy_statements()` (one policy each),
    NOT this concatenation."""
    return "\n\n".join(
        [
            model_invoke_policies(),
            retrieve_policy(),
            call_tool_policy(),
            forbid_cross_tenant(),
        ]
    )


# --------------------------------------------------------------------------- #
# Agent path — AgentCore Policy's own Cedar schema (#154, the deployed layer).  #
# --------------------------------------------------------------------------- #
def _tool_action(target: str, tool: str) -> str:
    """The AgentCore action entity for one Gateway tool: `<target>___<tool>` (the name the
    analyzer derives from the bound Gateway's tool schema)."""
    return f"{target}{AGENTCORE_TOOL_ACTION_SEP}{tool}"


def agentcore_tool_policy_statements(
    gateway_arn: str,
    tools: list[str],
    target: str,
    *,
    principal_type: str = AGENTCORE_PRINCIPAL_IAM,
) -> list[tuple[str, str]]:
    """The agent-path tool-authz policy set in AgentCore's OWN Cedar schema (#154).

    One permit per tool, each naming the specific Gateway ARN and the tool's AgentCore action,
    constrained by a `when` clause. AgentCore's analyzer (`FAIL_ON_ANY_FINDINGS`) rejects:
      * an abstract `resource`            -> "constrain to AgentCore::Gateway"  (the deploy bug)
      * a bare permit (no `when`)         -> "Overly Permissive"
      * `AgentCore::UnauthenticatedUser`  -> "unsupported principal type"
    so each statement pins resource == AgentCore::Gateway::"<arn>", action ==
    AgentCore::Action::"<target>___<tool>", an authenticated principal type, and carries a
    constraining `when`. The `when` requires the caller principal be present and identified
    (`principal has id && principal.id != ""`) — the workload identity the #137 chain binds;
    the cross-tenant fence itself stays in IAM (#113 PrincipalTag), which AgentCore's Cedar
    schema has no attribute for, so this layer is defence-in-depth UNDER that fence, not a
    replacement.

    `gateway_arn` is the concrete Gateway the policy governs (AgentCore requires a real ARN —
    a bare type or a non-ARN id is rejected). Generated from the deployed tool list so the
    enforced set tracks the Gateway's actual tools.
    """
    if principal_type not in (AGENTCORE_PRINCIPAL_IAM, AGENTCORE_PRINCIPAL_OAUTH):
        raise ValueError(f"unsupported AgentCore principal type: {principal_type!r}")
    out: list[tuple[str, str]] = []
    for tool in tools:
        action = _tool_action(target, tool)
        out.append(
            (
                f"tool-{tool}",
                f"// AgentCore tool authz: {target}/{tool} on this gateway only.\n"
                f"permit(\n"
                f"  principal is {principal_type},\n"
                f'  action == {AGENTCORE_ACTION_TYPE}::"{action}",\n'
                f'  resource == {AGENTCORE_GATEWAY_TYPE}::"{gateway_arn}"\n'
                f") when {{\n"
                f'  principal has id && principal.id != ""\n'
                f"}};",
            )
        )
    return out
