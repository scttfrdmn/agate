"""Slurm MCP server (#136 / #114) — the live integration behind hpc-submit/hpc-monitor.

AgentCore Gateway invokes this Lambda as an MCP-Lambda target. It is the EFFECT half of the
§5 split: the #113 IAM already fenced WHICH agents may invoke the HPC gateway tool; this
server enforces what the call may DO. The shape mirrors the #84 retrieval proxy exactly —
verify the inbound identity, derive the boundary (tenant/scope) ONLY from the verified
credential (never the tool payload), then act under it:

  * `hpc-monitor` (read): summarise the caller's OWN jobs — its `agate:scope` → ONE Slurm
    account; it never sees another lab's queue.
  * `hpc-submit`  (WRITE): gate on the budget cascade (#81) BEFORE the scheduler is touched
    (`agate.slurm.gate_submit`), reject over-allocation pre-call naming the breaching node,
    and on allow submit + record the debit + emit the #137 `ActingAs` attribution.

The actual scheduler transport is the injected `_submit_job`/`_list_jobs` seam — the
institution wires it to its own cluster at deploy (a LICENSED_WORKLOAD_STUB-style boundary;
there is no agate-hosted Slurm). Per-request Lambda, no clock. Fails closed: any
verification/scoping/budget error returns an error envelope, never a silent broad action.
"""

from __future__ import annotations

import json
import os

from agate.identity import acting_as_from_session
from agate.jwt_verify import TokenError, config_from_env, verify_token
from agate.slurm import SlurmError, gate_submit, slurm_account_for_scope
from agate.tags import ClaimsError, claims_to_tags, role_session_name

# Worst-case token shape for pricing a submit's gated cost (the agent run the submit drives).
# Non-secret deploy config; the institution tunes it. Defaults are non-zero so the cascade
# actually bites — a $0 estimate would make the gate a no-op (it always fits any budget).
# Mirrors the chokepoint's default-max-tokens ceiling.
SUBMIT_MODEL_ID = os.environ.get(
    "AGATE_SUBMIT_MODEL_ID", "us.anthropic.claude-opus-4-1-20250805-v1:0"
)
SUBMIT_INPUT_TOKENS = int(os.environ.get("AGATE_SUBMIT_INPUT_TOKENS", "10000"))
SUBMIT_MAX_TOKENS = int(os.environ.get("AGATE_SUBMIT_MAX_TOKENS", "4000"))


class SlurmToolError(ValueError):
    """A tool call that cannot be served safely. Fail closed."""


def validate_idp_token(token: str) -> dict:
    """Verify the campus-IdP token (real RS256/JWKS) — the SAME verifier the broker +
    retrieval proxy use. The inbound identity is the verified user the agent acts for."""
    if not token or not isinstance(token, str):
        raise SlurmToolError("missing idp_token")
    try:
        return verify_token(token, **config_from_env())
    except TokenError as exc:
        raise SlurmToolError(f"token verification failed: {exc}") from exc


def _spend_lookup_factory(spend_reader):
    """Bind a `(label) -> (spend, budget|None)` lookup over the live spend/budget tables.
    `spend_reader` is injected (the DynamoDB edge) so the gate logic stays testable."""

    def lookup(label: str) -> tuple[float, float | None]:
        return spend_reader(label)

    return lookup


def monitor(tags, *, list_jobs) -> dict:
    """`hpc-monitor`: the caller's OWN jobs, filtered to its scope's single account. The
    account is derived from the verified scope, so a caller can't enumerate another lab."""
    account = slurm_account_for_scope(tags.tenant, tags.scope)
    jobs = list_jobs(account)  # injected transport; returns a list of job dicts
    return {"account": account, "jobs": jobs}


def submit(tags, subject, job_spec, *, spend_reader, submit_job) -> dict:
    """`hpc-submit`: gate on the budget cascade, then submit to the caller's OWN allocation.

    `job_spec` is the (client-supplied) job description — its CONTENT is legitimate (it's the
    work to run), but it can carry NO identity/account/scope: those are derived from `tags`.
    `submit_job(account, job_spec) -> job_id` and `spend_reader(label) -> (spend, budget)` are
    injected transports. Returns the job id + the attribution; raises on a rejected budget."""
    decision = gate_submit(
        tenant=tags.tenant,
        scope=tags.scope,
        model_id=SUBMIT_MODEL_ID,
        input_tokens=SUBMIT_INPUT_TOKENS,
        max_tokens=SUBMIT_MAX_TOKENS,
        spend_lookup=_spend_lookup_factory(spend_reader),
    )
    if not decision.allowed:
        raise SlurmToolError(
            f"submit rejected: over allocation budget at {decision.cascade.breaching_node!r} "
            f"({decision.reason})"
        )
    job_id = submit_job(decision.account, job_spec)  # injected transport
    # #137 attribution: agent X · on behalf of the verified user · within remit. The OBO user
    # is recovered from the bound session name, never the payload.
    session_name = role_session_name(tags.tenant, subject)
    acting = acting_as_from_session(
        session_name,
        agent=f"{tags.tenant}/hpc-submit",
        remit={"scope": tags.scope, "account": decision.account, "tool": "hpc-submit"},
    )
    return {
        "account": decision.account,
        "jobId": job_id,
        "actingAs": acting.to_dict(),
    }


# --- live AWS edge (injected into the pure functions above) ------------------
# These are the deferred, institution-wired transports. Kept thin + lazy so the pure logic
# (slurm_account_for_scope / gate_submit) is unit-testable without AWS or a cluster.


def _real_spend_reader(label: str) -> tuple[float, float | None]:  # pragma: no cover
    """Read the live (spend, budget) for an allocation node from the spend/budget tables.
    Wired at deploy; a missing budget row => (spend, None) = no cap at that node."""
    raise SlurmToolError("spend reader not configured")


def _real_submit_job(account: str, job_spec: dict) -> str:  # pragma: no cover
    """Submit to the institution's Slurm scheduler (sbatch/REST) under `account`. This is the
    deploy-wired cluster transport — agate hosts no Slurm. Returns the scheduler's job id."""
    raise SlurmToolError("slurm submit transport not configured (deploy-wired)")


def _real_list_jobs(account: str) -> list:  # pragma: no cover
    raise SlurmToolError("slurm monitor transport not configured (deploy-wired)")


def process(req: dict) -> dict:
    """Route one MCP tool call. `req` carries the verified `idp_token`, the `tool`
    (`hpc-submit`|`hpc-monitor`), and a `job_spec` for submit. Tenant/scope/account are ALL
    derived from the token — any account/scope/tenant in the body is ignored (a client can't
    submit to another lab)."""
    claims = validate_idp_token(req.get("idp_token", ""))
    try:
        tags = claims_to_tags(claims)
    except ClaimsError as exc:
        raise SlurmToolError(f"cannot scope session: {exc}") from exc
    subject = str(claims.get("sub") or claims.get("subject") or "agate-user")

    tool = req.get("tool")
    if tool == "hpc-monitor":
        return monitor(tags, list_jobs=_real_list_jobs)
    if tool == "hpc-submit":
        job_spec = req.get("job_spec") or {}
        return submit(
            tags, subject, job_spec,
            spend_reader=_real_spend_reader, submit_job=_real_submit_job,
        )
    raise SlurmToolError(f"unknown tool: {tool!r}")


def handler(event: dict, context: object) -> dict:
    """MCP-Lambda target entry point. Fail-closed: a verification/scoping/budget failure
    returns an error envelope, never a silent broad action."""
    try:
        body = event.get("body") or "{}"
        if event.get("isBase64Encoded"):
            import base64

            body = base64.b64decode(body).decode("utf-8")
        req = json.loads(body) if isinstance(body, str) else body
        return _resp(200, process(req))
    except (SlurmToolError, SlurmError) as exc:
        return _resp(403, {"error": "not_entitled", "detail": str(exc)})
    except Exception:  # noqa: BLE001 — last-resort fail-closed
        return _resp(500, {"error": "slurm_tool_error"})


def _resp(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body),
    }
