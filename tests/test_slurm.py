"""Unit tests for the Slurm HPC tool — scope→allocation + budget gate (#136 / #114). No AWS.

The §5/§10.2 invariant made concrete: a caller's verified `agate:scope` maps to exactly ONE
Slurm allocation (never a sibling lab's), and `hpc-submit` is gated on the budget cascade (#81)
before the scheduler is touched. Identity/account come ONLY from the verified credential, never
the tool payload.
"""

from __future__ import annotations

import pytest
from agate.slurm import (
    SlurmError,
    gate_submit,
    slurm_account_for_scope,
    submit_cascade_nodes,
)
from agate.tags import SessionTags
from infra.functions.slurm import handler as h

_MODEL = "us.anthropic.claude-opus-4-1-20250805-v1:0"


def _tags(scope="lab/photonics", tenant="chem", tier="frontier"):
    return SessionTags(
        affiliation="faculty", tenant=tenant, courses=(), tier=tier, role="member", scope=scope
    )


# --- scope -> account: deterministic, injective, confined ---------------------


def test_account_is_deterministic_and_confined():
    assert slurm_account_for_scope("chem", "lab/photonics") == "chem-lab_photonics"
    assert slurm_account_for_scope("chem", "lab/photonics") == slurm_account_for_scope(
        "chem", "lab/photonics"
    )


def test_sibling_scopes_get_distinct_accounts():
    a = slurm_account_for_scope("chem", "lab/photonics")
    b = slurm_account_for_scope("chem", "lab/optics")
    assert a != b


def test_traversal_scope_falls_to_tenant_default_never_sibling():
    # `..` is rejected by normalise_scope → the tenant-wide allocation, NOT the escaped path.
    acct = slurm_account_for_scope("chem", "lab/../physics")
    assert acct == "chem-default"


def test_empty_scope_is_tenant_default():
    assert slurm_account_for_scope("chem", "") == "chem-default"


def test_empty_tenant_rejected():
    with pytest.raises(SlurmError):
        slurm_account_for_scope("", "lab/photonics")


# --- the cascade node-list (hierarchical allocation budget) -------------------


def test_cascade_nodes_are_scope_ancestors_broad_to_specific():
    nodes = submit_cascade_nodes("chem", "lab/photonics", lambda label: (0.0, 10.0))
    labels = [n[0] for n in nodes]
    assert labels == ["lab", "lab/photonics"]  # ancestor-or-self, broad→specific


# --- the budget gate: a submit can't out-spend its allocation -----------------


def test_submit_rejected_over_allocation_names_breaching_node():
    d = gate_submit(
        tenant="chem", scope="lab/photonics", model_id=_MODEL,
        input_tokens=100000, max_tokens=4000,
        spend_lookup=lambda label: (0.0, 0.000001) if label == "lab/photonics" else (0.0, None),
    )
    assert d.allowed is False
    assert d.cascade.breaching_node == "lab/photonics"
    assert d.account == "chem-lab_photonics"  # account still resolved (for the audit)


def test_submit_allowed_within_budget():
    d = gate_submit(
        tenant="chem", scope="lab/photonics", model_id=_MODEL,
        input_tokens=10, max_tokens=10,
        spend_lookup=lambda label: (0.0, 1000.0),
    )
    assert d.allowed is True
    assert d.account == "chem-lab_photonics"


# --- handler: identity/account from the credential, NEVER the payload ---------


def test_submit_ignores_payload_supplied_account():
    # A malicious job_spec carrying another lab's account must have NO effect — the account
    # is derived from the verified scope.
    res = h.submit(
        _tags(scope="lab/photonics"), "prof",
        {"account": "physics-EVIL", "scope": "physics", "script": "run.sh"},
        spend_reader=lambda label: (0.0, 1000.0),
        submit_job=lambda acct, spec: f"job-on-{acct}",
    )
    assert res["account"] == "chem-lab_photonics"  # NOT physics-EVIL
    assert res["jobId"] == "job-on-chem-lab_photonics"


def test_submit_emits_obo_attribution():
    res = h.submit(
        _tags(), "prof", {"script": "run.sh"},
        spend_reader=lambda label: (0.0, 1000.0),
        submit_job=lambda acct, spec: "job-1",
    )
    aa = res["actingAs"]
    assert aa["on_behalf_of"] == "chem@prof"  # the verified user
    assert aa["agent"] == "chem/hpc-submit"
    assert aa["attributed"] is True


def test_submit_raises_when_over_budget_before_scheduler():
    # The scheduler transport must NOT be called when the budget rejects.
    calls = []
    with pytest.raises(h.SlurmToolError):
        h.submit(
            _tags(), "prof", {"script": "run.sh"},
            spend_reader=lambda label: (0.0, 0.000001),
            submit_job=lambda acct, spec: calls.append(acct) or "job",
        )
    assert calls == []  # never reached the scheduler


def test_monitor_filters_to_callers_own_account():
    captured = {}
    res = h.monitor(
        _tags(scope="lab/photonics"),
        list_jobs=lambda acct: captured.setdefault("acct", acct) or [{"id": "j1"}],
    )
    assert res["account"] == "chem-lab_photonics"
    assert captured["acct"] == "chem-lab_photonics"  # the transport saw only the own account


def test_handler_envelope_fails_closed_on_bad_token():
    # No verified token → 403 envelope, never a 200 with a broad action.
    resp = h.handler({"body": '{"tool": "hpc-monitor", "idp_token": "garbage"}'}, None)
    assert resp["statusCode"] == 403
