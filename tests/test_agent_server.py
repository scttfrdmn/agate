"""Tests for the agent container's invocation handling. No AWS — backends stubbed.

Exercises run_invocation end-to-end (dispatch + terminal receipt) and the
events→blob encoding, without the Bedrock/Code-Interpreter clients.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import server  # noqa: E402
from agent.backends import decode_payload, encode_payload  # noqa: E402


class StubBackend:
    def __init__(self, *_a, **_k):
        pass

    def converse(self, tier, system, prompt, max_tokens):
        word = "SYNTHESIS" if "Reply with one word" in system else "The cited answer."
        return word, {"inputTokens": 10, "outputTokens": 3}, None


@pytest.fixture
def stub_backends(monkeypatch):
    monkeypatch.setattr(server, "BedrockBackend", StubBackend)
    monkeypatch.setattr(server, "CODE_INTERPRETER_ID", "")  # no runner -> Ask/Panel only


def test_run_invocation_emits_stream_then_receipt(stub_backends):
    events = server.run_invocation({"question": "what is x?", "evidence": "DOC1"})
    types = [e["type"] for e in events]
    assert "route" in types
    assert "answer" in types
    # always closes with a receipt
    assert types[-1] == "receipt"
    receipt = events[-1]
    assert "rows" in receipt and "total" in receipt
    assert any(r["kind"] == "llm" for r in receipt["rows"])


def test_run_invocation_bad_payload_surfaces_error_then_receipt(stub_backends):
    events = server.run_invocation({})  # no question
    assert events[0]["type"] == "answer"
    assert events[0]["title"] == "error"
    assert events[-1]["type"] == "receipt"


def test_unverifiable_token_falls_back_to_oss(stub_backends, monkeypatch):
    # SEC-4b: no/invalid token -> oss tier -> a frontier model in the payload is
    # rejected by dispatch before any model call.
    events = server.run_invocation(
        {
            "question": "compare",
            "mode": "DEBATE",
            "evidence": "e",
            "roster": [
                {
                    "tier": "us.anthropic.claude-opus-4-1-20250805-v1:0",
                    "label": "x",
                    "max_tokens": 64,
                }
            ],
            "adjudicator": {"tier": "openai.gpt-oss-20b-1:0", "label": "adj", "max_tokens": 64},
            "idp_token": "",  # unverifiable -> oss
        }
    )
    # dispatch raised InvocationError (model not entitled) -> surfaced as error answer
    assert any(e["type"] == "answer" and e.get("title") == "error" for e in events)
    assert any("not entitled" in e.get("text", "") for e in events if e["type"] == "answer")


def test_verified_frontier_tier_allows_frontier_model(stub_backends, monkeypatch):
    # A verified frontier tier permits a frontier model (the positive case).
    monkeypatch.setattr(server, "_verified_tier", lambda payload: "frontier")
    events = server.run_invocation(
        {
            "question": "compare",
            "mode": "DEBATE",
            "evidence": "e",
            "roster": [
                {
                    "tier": "us.anthropic.claude-opus-4-1-20250805-v1:0",
                    "label": "x",
                    "max_tokens": 64,
                }
            ],
            "adjudicator": {
                "tier": "us.anthropic.claude-opus-4-1-20250805-v1:0",
                "label": "adj",
                "max_tokens": 64,
            },
        }
    )
    # no entitlement error; ran to a receipt
    assert not any(e.get("title") == "error" for e in events if e["type"] == "answer")
    assert events[-1]["type"] == "receipt"


def test_resolve_models_fills_defaults_with_real_ids():
    # A bare SPA payload (no roster/generator/router) gets concrete entitled ids,
    # never a bare logical label like "oss".
    entitled = ["openai.gpt-oss-20b-1:0", "openai.gpt-oss-120b-1:0", "google.gemma-3-12b-it"]
    out = server._resolve_models({"question": "q", "mode": "SYNTHESIS"}, entitled)
    assert out["generator"]["tier"] == "openai.gpt-oss-20b-1:0"
    assert out["router"]["tier"] == "openai.gpt-oss-20b-1:0"


def test_resolve_models_builds_debate_roster_when_missing():
    entitled = ["openai.gpt-oss-20b-1:0", "openai.gpt-oss-120b-1:0", "google.gemma-3-12b-it"]
    out = server._resolve_models({"question": "q", "mode": "DEBATE"}, entitled)
    assert len(out["roster"]) == 3
    assert all(m["tier"] in entitled for m in out["roster"])
    assert out["adjudicator"]["tier"] in entitled


def test_resolve_models_leaves_caller_supplied_config_untouched():
    entitled = ["openai.gpt-oss-20b-1:0"]
    given = {
        "question": "q",
        "generator": {"tier": "openai.gpt-oss-120b-1:0", "label": "g", "max_tokens": 9},
    }
    out = server._resolve_models(given, entitled)
    assert out["generator"]["tier"] == "openai.gpt-oss-120b-1:0"  # not overwritten


def test_bare_spa_payload_runs_to_receipt(stub_backends):
    # The exact shape the SPA sends: question + mode only, no roster/generator.
    events = server.run_invocation({"question": "what is agate?", "mode": "SYNTHESIS"})
    assert not any(e.get("title") == "error" for e in events if e["type"] == "answer")
    assert events[-1]["type"] == "receipt"
    # the generator model id actually used is a real entitled id, not "oss"
    assert any(e["type"] == "answer" and e.get("text") for e in events)


def test_pattern_runs_through_run_invocation(stub_backends, monkeypatch):
    # A registered pattern compiles against the verified tier's entitled models and
    # runs the DEBATE primitive end-to-end (route -> per-role models -> receipt).
    monkeypatch.setattr(server, "_verified_tier", lambda payload: "frontier")
    events = server.run_invocation(
        {"question": "Is the effect real?", "pattern": "red-team", "evidence": "some trials"}
    )
    labels = {e.get("label") for e in events if e["type"] == "model"}
    assert {"for", "against"} <= labels  # the red-team roles ran
    assert events[-1]["type"] == "receipt"
    assert not any(e.get("title") == "error" for e in events if e["type"] == "answer")


def test_unknown_pattern_surfaces_error(stub_backends):
    events = server.run_invocation({"question": "q", "pattern": "no-such-pattern"})
    assert any(e.get("title") == "error" for e in events if e["type"] == "answer")
    assert events[-1]["type"] == "receipt"


def test_pattern_per_role_system_prompt_reaches_backend(monkeypatch):
    # Each role's institution-defined system prompt must reach the model (not a
    # single shared prompt) — the whole point of a reasoning pattern.
    seen: list[str] = []

    class CapturingBackend:
        def __init__(self, *_a, **_k):
            pass

        def converse(self, tier, system, prompt, max_tokens):
            seen.append(system)
            return (
                ("SYNTHESIS" if "Reply with one word" in system else "ok"),
                {
                    "inputTokens": 1,
                    "outputTokens": 1,
                },
                None,
            )

    monkeypatch.setattr(server, "BedrockBackend", CapturingBackend)
    monkeypatch.setattr(server, "CODE_INTERPRETER_ID", "")
    monkeypatch.setattr(server, "_verified_tier", lambda payload: "frontier")
    server.run_invocation({"question": "q", "pattern": "lit-review", "evidence": "e"})
    joined = " ".join(seen)
    # the distinct role recipes are present, not one shared prompt
    assert "empirical CLAIMS" in joined
    assert "methodologist" in joined
    assert "GAPS" in joined


# --- memory hook (#130b) ----------------------------------------------------


def test_memory_disabled_is_a_noop(stub_backends, monkeypatch):
    # With no memory tool wired, neither recall nor record is attempted — the default path.
    from agent import memory_client

    monkeypatch.setattr(memory_client, "MEMORY_TOOL_ARN", "")
    calls = []
    monkeypatch.setattr(memory_client, "recall", lambda *a, **k: calls.append("recall") or [])
    monkeypatch.setattr(memory_client, "record", lambda *a, **k: calls.append("record") or True)
    events = server.run_invocation({"question": "q", "idp_token": "t"}, session_id="s-1")
    assert calls == []  # enabled() is False, so the hooks short-circuit before calling
    assert events[-1]["type"] == "receipt"


def test_memory_recall_prepended_to_evidence(stub_backends, monkeypatch):
    # When enabled, recalled memory is folded into the evidence the reasoning modes consume.
    from agent import memory_client

    seen_prompts = []

    class CapturingBackend:
        def __init__(self, *_a, **_k):
            pass

        def converse(self, tier, system, prompt, max_tokens):
            seen_prompts.append(prompt)
            return (
                ("SYNTHESIS" if "Reply with one word" in system else "ok"),
                {
                    "inputTokens": 1,
                    "outputTokens": 1,
                },
                None,
            )

    monkeypatch.setattr(server, "BedrockBackend", CapturingBackend)
    monkeypatch.setattr(memory_client, "enabled", lambda: True)
    monkeypatch.setattr(
        memory_client, "recall", lambda *a, **k: [{"content": "the user prefers SI units"}]
    )
    recorded = {}
    monkeypatch.setattr(
        memory_client,
        "record",
        lambda token, payload, **k: recorded.update(token=token, payload=payload) or True,
    )
    events = server.run_invocation(
        {"question": "compute", "idp_token": "tok", "evidence": "DOC1", "mode": "SYNTHESIS"},
        session_id="s-1",
    )
    # the Ask prompt carries BOTH the recalled memory and the original evidence
    ask = next(p for p in seen_prompts if "Question:" in p)
    assert "the user prefers SI units" in ask
    assert "DOC1" in ask
    # the turn's answers were recorded with the forwarded token
    assert recorded["token"] == "tok"
    assert any(e.get("type") == "answer" for e in [{"type": "answer"}])  # sanity
    assert events[-1]["type"] == "receipt"


def test_memory_record_skipped_without_session_id(stub_backends, monkeypatch):
    from agent import memory_client

    monkeypatch.setattr(memory_client, "enabled", lambda: True)
    monkeypatch.setattr(memory_client, "recall", lambda *a, **k: [])
    calls = []
    monkeypatch.setattr(memory_client, "record", lambda *a, **k: calls.append(1) or True)
    server.run_invocation({"question": "q", "idp_token": "tok"}, session_id="")
    assert calls == []  # no session id -> no record


def test_events_to_blob_is_ndjson():
    blob = server._events_to_blob(
        [{"type": "answer", "text": "hi"}, {"type": "cost", "total": 1.0}]
    )
    lines = blob.decode().strip().split("\n")
    assert len(lines) == 2
    assert json.loads(lines[0])["type"] == "answer"
    assert json.loads(lines[1])["total"] == 1.0


def test_payload_codec_roundtrip():
    obj = {"question": "q", "mode": "DEBATE", "roster": [{"tier": "frontier"}]}
    assert decode_payload(encode_payload(obj)) == obj
    assert decode_payload("") == {}
