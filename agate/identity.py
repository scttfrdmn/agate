"""Agent identity & on-behalf-of (OBO) — who · on whose authority · what remit (#137, §2.5).

Every agent action must answer three distinct questions, and agate already *encodes* the
answers — just implicitly, scattered across the `<tenant>@<subject>` RoleSessionName (#79),
bounded delegation (#106), and the agent-graph attribution chain (#112). This module makes
them ONE canonical, auditable record so every downstream path — tool calls (#113),
connector reads (#133), memory writes (#110), the saved-session audit (#109), and the live
executors (#136) — emits the SAME OBO record rather than retrofitting it.

The model mirrors AgentCore Identity: an agent is a **workload identity** (it authenticates
*as itself*, not by impersonating the user), and an action binds **both** the agent
identity AND the authorizing user — AWS's "Agent access token". `ActingAs` is the portable
equivalent: *agent X · on behalf of user U · within remit R · via chain C*.

Pure and AWS-free: identity is DERIVED (deterministic agent id, content-version) and the
OBO user is RECOVERED from the verified RoleSessionName — never client-supplied. Fail-closed:
a record always carries both an agent AND an on-behalf-of (or an explicit *unattributed*
marker for a legacy un-encoded session) — there is no half-attributed action.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from agate.budget import _clean_id
from agate.tags import subject_from_session_name, tenant_from_session_name

# Marker for the OBO user when a session name carries no encoded tenant (legacy/un-encoded).
# An action is NEVER silently "trusted" as some user — it is explicitly unattributed.
UNATTRIBUTED = "unattributed"


def agent_id(tenant: str, spec_name: str) -> str:
    """The stable WHO: `{tenant}/{spec_name}` (id grammar). Deterministic and reproducible
    — the same authored spec always has the same identity within its tenant, so it audits
    and diffs cleanly. The live AgentCore workload-identity ARN is the deploy-time binding
    of this id (deferred, #136). `_clean_id` strips `/` etc., so neither part can inject an
    extra path segment that escapes the `{tenant}/` prefix."""
    t = _clean_id(tenant)
    n = _clean_id(spec_name)
    if not t or not n:
        raise ValueError("agent_id needs a non-empty tenant and spec name")
    return f"{t}/{n}"


def spec_version(spec) -> str:  # noqa: ANN001 — an agentspec.AgentSpec (loose to avoid a cycle)
    """A short content digest of the spec's identity-bearing fields — provenance for WHICH
    version of the authored agent acted. Stable for the same spec, changes when the bound
    (role/scope/tools/budget/agents) changes, so the audit can pin the exact spec a run used.
    Derived from the compiled bound, not the object id, so it's reproducible."""
    budget = spec.budget
    parts = (
        spec.name,
        spec.role,
        spec.scope,
        ",".join(spec.tools),
        f"{budget.usd}/{budget.per}/{budget.period_kind}" if budget else "",
        ",".join(a.name for a in spec.agents),
    )
    return hashlib.sha256("\x00".join(parts).encode()).hexdigest()[:12]


@dataclass(frozen=True, slots=True)
class ActingAs:
    """The canonical OBO record for an action: the three answers + the chain.

    `agent`/`agent_version` = WHO (a stable workload identity, as itself).
    `on_behalf_of` = WHOSE authority (the verified `<tenant>@<subject>`, or UNATTRIBUTED).
    `remit` = WHAT it may do (a compact bound view; the legible form is #108).
    `chain` = the attribution path for a graph hop (`user/root/lit/...`), or "".
    """

    agent: str
    agent_version: str
    tenant: str
    subject: str
    remit: dict = field(default_factory=dict)
    chain: str = ""

    @property
    def on_behalf_of(self) -> str:
        """`<tenant>@<subject>` — the human this action acts for, or UNATTRIBUTED."""
        if self.subject == UNATTRIBUTED or not self.tenant:
            return UNATTRIBUTED
        return f"{self.tenant}@{self.subject}"

    @property
    def attributed(self) -> bool:
        """An action is attributed iff it carries BOTH an agent AND a real OBO user
        (the §10 invariant: no half-attributed action)."""
        return bool(self.agent) and self.subject != UNATTRIBUTED

    def to_dict(self) -> dict:
        return {
            "agent": self.agent,
            "agent_version": self.agent_version,
            "on_behalf_of": self.on_behalf_of,
            "tenant": self.tenant,
            "subject": self.subject,
            "remit": dict(self.remit),
            "chain": self.chain,
            "attributed": self.attributed,
        }

    def summary(self) -> str:
        return f"agent {self.agent!r} · on behalf of {self.on_behalf_of} · remit {self.remit}"


def acting_as_from_session(
    session_name: str,
    *,
    agent: str,
    agent_version: str = "",
    remit: dict | None = None,
    chain: str = "",
) -> ActingAs:
    """Build an `ActingAs` from a VERIFIED RoleSessionName (#79). The OBO user comes ONLY
    from the session name — `tenant_from_session_name`/`subject_from_session_name` — never a
    client field. A legacy/un-encoded name (no `<tenant>@`) is marked UNATTRIBUTED rather
    than fabricating a user (fail-closed)."""
    tenant = tenant_from_session_name(session_name)
    if tenant is None:
        return ActingAs(
            agent=agent,
            agent_version=agent_version,
            tenant="",
            subject=UNATTRIBUTED,
            remit=remit or {},
            chain=chain,
        )
    return ActingAs(
        agent=agent,
        agent_version=agent_version,
        tenant=tenant,
        subject=subject_from_session_name(session_name),
        remit=remit or {},
        chain=chain,
    )
