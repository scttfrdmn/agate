"""Unit tests for created-agent records (#118 deploy-on-confirm). No AWS — pure.

A created agent is a scope-tagged S3 object whose KEY prefix (`{tenant}/{scope}/`) is the #80
access fence (mirrors SavedSession, #109). The record stores the validated draft dict + the
server-supplied provenance; the key sanitises every segment so a crafted scope/name can't
escape the tenant/scope prefix.
"""

from __future__ import annotations

import pytest
from agate.agent_record import (
    AgentRecordError,
    agent_object_key,
    build_agent_record,
    from_json,
)

_SPEC = {
    "agent": "paper-sweep",
    "description": "summarize new papers",
    "role": "researcher",
    "scope": "chemistry/chem-101",
    "reasoning": "lit-review",
    "tools": ["library-search"],
}


def _record(**over):
    kw = dict(
        name="paper-sweep",
        tenant="uni",
        scope="chemistry/chem-101",
        agent_id="uni/paper-sweep",
        spec_version="abc123def456",
        created_by="uni@prof",
        created="2026-06-17T00:00:00Z",
        spec=_SPEC,
        boundary=["reads chemistry/chem-101", "≤ $20 / user / month"],
    )
    kw.update(over)
    return build_agent_record(**kw)


# --- key derivation: the prefix IS the fence -------------------------------


def test_key_is_under_tenant_scope_prefix():
    assert (
        agent_object_key("uni", "chemistry/chem-101", "paper-sweep")
        == "uni/chemistry/chem-101/_agents/paper-sweep.json"
    )


def test_unscoped_agent_lives_at_tenant_root():
    assert agent_object_key("uni", "", "paper-sweep") == "uni/_agents/paper-sweep.json"


def test_key_sanitises_crafted_name_no_path_injection():
    # A name trying to inject path segments is flattened to one id segment: `/` is stripped,
    # so it can't escape the `_agents/` segment (dots survive as a flat filename — harmless
    # without a `/`, they can't traverse).
    key = agent_object_key("uni", "chemistry", "../../evil/agent")
    assert key.startswith("uni/chemistry/_agents/")
    # exactly one segment after _agents/ — no `/` injection escaped the prefix
    tail = key.split("/_agents/")[1]
    assert "/" not in tail


def test_key_traversal_scope_collapses_to_tenant_root_not_widened():
    # A `..` scope normalises to empty -> tenant root, never escaping the tenant.
    key = agent_object_key("uni", "../other", "a")
    assert key == "uni/_agents/a.json"


def test_key_requires_tenant_and_name():
    with pytest.raises(AgentRecordError):
        agent_object_key("", "scope", "a")
    with pytest.raises(AgentRecordError):
        agent_object_key("uni", "scope", "///")  # name garbles to empty


# --- record build + round-trip ---------------------------------------------


def test_build_and_roundtrip_preserves_spec_and_provenance():
    rec = _record()
    back = from_json(rec.to_json())
    assert back.name == "paper-sweep"
    assert back.tenant == "uni"
    assert back.scope == "chemistry/chem-101"
    assert back.agent_id == "uni/paper-sweep"
    assert back.spec_version == "abc123def456"
    assert back.created_by == "uni@prof"
    assert back.spec == _SPEC  # the validated draft dict reloads verbatim
    assert "reads chemistry/chem-101" in back.boundary


def test_build_rejects_empty_spec():
    with pytest.raises(AgentRecordError):
        _record(spec={})


def test_from_json_rejects_missing_spec():
    with pytest.raises(AgentRecordError):
        from_json('{"name": "x", "tenant": "uni"}')


def test_stored_spec_reloads_through_parse_spec():
    # The stored spec must be exactly what agentspec.parse_spec accepts (the audit source).
    from agate.agentspec import parse_spec

    rec = _record()
    back = from_json(rec.to_json())
    spec = parse_spec(back.spec)  # must not raise
    assert spec.name == "paper-sweep"
    assert spec.scope == "chemistry/chem-101"
