"""Tests for the Tier 1 choke point — no AWS (spend/budget/STS/Bedrock stubbed).

Post-SEC-1: identity is derived from the IdP token (claims_to_tags), and budget is
looked up server-side — never from request fields. Tests prove a malicious body
cannot forge tenant/tier/budget.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chokepoint import handler as cp  # noqa: E402

# 1x1 transparent PNG (valid magic bytes) — shared across the image tests.
_PNG = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


class _FakeBedrock:
    def __init__(self):
        self.calls = 0
        self.last_model = None  # the modelId the choke point invoked Converse with
        self.last_messages = None  # the Converse `messages` it was called with

    def converse(self, modelId, messages, inferenceConfig):  # noqa: N803
        self.calls += 1
        self.last_model = modelId
        self.last_messages = messages
        return {
            "output": {"message": {"content": [{"text": "answer"}]}},
            "usage": {"inputTokens": 12, "outputTokens": 8},
        }


class _Wired:
    def __init__(self, br: _FakeBedrock):
        self._br = br
        self.spend = 0.0
        self.budget: float | None = 100.0
        self.assumed_tags = None  # records the SessionTags assume_user_role got
        # #81 cascade: per-scope-node budgets/spend keyed by node label, and a log
        # of (node, cost) writes the choke point made on allow.
        self.scope_budgets: dict[str, float | None] = {}
        self.scope_spend: dict[str, float] = {}
        self.scope_writes: list[tuple[str, float]] = []

    @property
    def calls(self) -> int:
        return self._br.calls

    @property
    def last_model(self) -> str | None:
        return self._br.last_model

    @property
    def last_messages(self):
        return self._br.last_messages


@pytest.fixture
def wired(monkeypatch):
    fake_br = _FakeBedrock()
    w = _Wired(fake_br)

    def fake_assume(tags, user):
        w.assumed_tags = tags
        return fake_br

    monkeypatch.setattr(cp, "assume_user_role", fake_assume)
    monkeypatch.setattr(cp, "read_spend", lambda tenant, user, period: w.spend)
    monkeypatch.setattr(cp, "lookup_budget", lambda tenant, user, period: w.budget)
    monkeypatch.setattr(
        cp, "read_scope_spend", lambda tenant, node, period: w.scope_spend.get(node, 0.0)
    )
    monkeypatch.setattr(
        cp, "lookup_scope_budget", lambda tenant, node, period: w.scope_budgets.get(node)
    )
    monkeypatch.setattr(
        cp,
        "_increment_scope_spend",
        lambda tenant, node, period, cost: w.scope_writes.append((node, cost)),
    )
    monkeypatch.setattr(cp, "AUTHENTICATED_ROLE_ARN", "arn:aws:iam::123:role/agate-authenticated")

    # Simulate a VERIFIED token: decode the JSON the test passes as `idp_token`.
    # Real signature/JWKS verification is covered by tests/test_jwt_verify.py.
    # Mirror the real validate_idp_token's failure code (token_invalid) so 402-code tests are
    # faithful — the production path raises token_invalid on any TokenError.
    def _decode(token):
        if not token:
            raise cp.ChokepointError("no token", code="token_invalid")
        try:
            claims = json.loads(token)
        except ValueError as exc:
            raise cp.ChokepointError("bad token", code="token_invalid") from exc
        if not isinstance(claims, dict):
            raise cp.ChokepointError("bad token", code="token_invalid")
        return claims

    monkeypatch.setattr(cp, "validate_idp_token", _decode)
    return w


# A valid (Phase-1 placeholder) IdP token = pre-validated claims JSON, same as broker.
def _token(affiliation="student", tenant="chem", sub="student-7", **extra):
    claims = {"sub": sub, "affiliation": affiliation, "tenant": tenant, **extra}
    return json.dumps(claims)


def _req(token=None, **over):
    r = {
        "idp_token": token if token is not None else _token(),
        "model": "oss",
        "messages": [{"role": "user", "content": "hello"}],
        "max_tokens": 1000,
    }
    r.update(over)
    return r


def test_allows_and_invokes_when_within_budget(wired):
    wired.spend, wired.budget = 1.0, 100.0
    out = cp.process(_req(), period="2026-06")
    assert out["text"] == "answer"
    assert wired.calls == 1
    # the session was scoped by the TOKEN-derived tags
    sent = {t["Key"]: t["Value"] for t in wired.assumed_tags.to_sts_tags()}
    assert sent["agate:tenant"] == "chem"
    assert sent["agate:tier"] == "oss"


def test_auto_routes_to_an_entitled_model(wired):
    # "auto" → the server picks within the verified tier (student → oss). With budget
    # headroom + default difficulty, thrifty picks the cheapest oss model. The model is
    # NEVER above the tier, and the response reports the routed choice.
    wired.spend, wired.budget = 0.0, 100.0
    out = cp.process(_req(model="auto"), period="2026-06")
    assert out["text"] == "answer"
    assert wired.last_model == "openai.gpt-oss-20b-1:0"  # an oss-tier model
    assert out["model"] == "openai.gpt-oss-20b-1:0"
    assert out["model_route"]["model"] == "openai.gpt-oss-20b-1:0"
    assert "reason" in out["model_route"]


def test_auto_with_no_model_field_also_routes(wired):
    wired.spend, wired.budget = 0.0, 100.0
    req = _req()
    del req["model"]  # omitted entirely → treated as auto
    cp.process(req, period="2026-06")
    assert wired.last_model == "openai.gpt-oss-20b-1:0"


def test_auto_never_exceeds_tier_for_faculty(wired):
    # faculty → mid tier; auto must pick from the entitled (oss+mid) set, never a
    # frontier-only model. (models_for_tier is cumulative; frontier-only = the two
    # not present at the mid tier.)
    from agate.entitlements import models_for_tier

    wired.spend, wired.budget = 0.0, 100.0
    cp.process(_req(token=_token(affiliation="faculty"), model="auto"), period="2026-06")
    mid = set(models_for_tier("mid"))
    frontier_only = set(models_for_tier("frontier")) - mid
    assert wired.last_model in mid
    assert wired.last_model not in frontier_only


def test_explicit_model_is_not_rerouted(wired):
    # A concrete (non-auto) model id passes through unchanged — no routing, no model_route.
    wired.spend, wired.budget = 1.0, 100.0
    out = cp.process(_req(model="openai.gpt-oss-120b-1:0"), period="2026-06")
    assert wired.last_model == "openai.gpt-oss-120b-1:0"
    assert "model_route" not in out


def test_auto_upgrades_to_vision_model_when_turn_has_a_figure(wired):
    # #244 H2: a figure-bearing turn under "auto" must route to a vision-capable entitled model,
    # not the cheapest text-only one. faculty → mid tier includes Claude (vision).
    wired.spend, wired.budget = 0.0, 100.0
    req = _req(token=_token(affiliation="faculty"), model="auto")
    req["messages"] = [{"role": "user", "content": "interpret [figure from c1]", "images": [_PNG]}]
    out = cp.process(req, period="2026-06")
    from agate.entitlements import supports_vision

    assert supports_vision(wired.last_model)
    assert "vision" in out["model_route"]["reason"]
    # The image reached Converse as an image block.
    assert any(b.get("image") for b in wired.last_messages[0]["content"])


def test_text_only_model_strips_image_and_placeholder(wired):
    # #244 H2: a text-only model (student → oss) must NOT be told about a figure it can't see —
    # both the image block AND the "[figure from cN]" text placeholder are stripped.
    wired.spend, wired.budget = 0.0, 100.0
    req = _req(model="openai.gpt-oss-120b-1:0")  # explicit text-only model
    req["messages"] = [
        {"role": "user", "content": "explain [figure from c1]\nT_eq = 500", "images": [_PNG]}
    ]
    cp.process(req, period="2026-06")
    content = wired.last_messages[0]["content"]
    assert all(not b.get("image") for b in content)  # no image block
    text = " ".join(b.get("text", "") for b in content)
    assert "[figure from c1]" not in text  # placeholder gone
    assert "T_eq = 500" in text  # real text output kept


def test_response_reports_spend_and_budget_for_the_ui(wired):
    # The UI shows "where you stand": spend_after = prior spend + this call's cost,
    # plus the period budget (None when no cap is configured).
    wired.spend, wired.budget = 2.0, 50.0
    out = cp.process(_req(), period="2026-06")
    b = out["budget"]
    assert b["period"] == "2026-06"
    assert b["budget_usd"] == 50.0
    assert b["spend_usd"] >= 2.0  # prior spend + this call's actual cost
    assert out["cost"] >= 0.0


def test_response_budget_is_null_when_no_cap(wired):
    wired.spend, wired.budget = 0.0, None  # no budget row configured
    out = cp.process(_req(), period="2026-06")
    assert out["budget"]["budget_usd"] is None


def test_rejects_pre_call_when_over_budget(wired):
    wired.spend, wired.budget = 99.999, 100.0
    with pytest.raises(cp.ChokepointError, match="budget"):
        cp.process(_req(model="frontier", max_tokens=1000), period="2026-06")
    assert wired.calls == 0  # model NOT invoked


def test_zero_budget_rejects_before_call(wired):
    wired.spend, wired.budget = 0.0, 0.0
    with pytest.raises(cp.ChokepointError):
        cp.process(_req(), period="2026-06")
    assert wired.calls == 0


def test_no_budget_configured_allows(wired):
    wired.spend, wired.budget = 9999.0, None
    out = cp.process(_req(), period="2026-06")
    assert out["text"] == "answer"


# --- SEC-1 regression: the body cannot forge identity or budget ---------------


def test_body_cannot_forge_tenant_or_tier(wired):
    wired.spend, wired.budget = 1.0, 100.0
    # Malicious body claims tenant=law, tier=frontier, budget=1e9 — all ignored.
    out = cp.process(
        _req(tenant="law", user="victim", tier="frontier", courses=["x"], budget=1e9),
        period="2026-06",
    )
    assert out["text"] == "answer"
    sent = {t["Key"]: t["Value"] for t in wired.assumed_tags.to_sts_tags()}
    # tenant/tier come from the TOKEN (chem/oss), not the body (law/frontier)
    assert sent["agate:tenant"] == "chem"
    assert sent["agate:tier"] == "oss"


def test_body_budget_field_is_ignored(wired):
    # Caller sends a huge body budget but the server budget is tiny -> reject.
    wired.spend, wired.budget = 0.5, 0.4
    with pytest.raises(cp.ChokepointError, match="budget"):
        cp.process(_req(model="frontier", budget=1e9, max_tokens=1000), period="2026-06")
    assert wired.calls == 0


def test_missing_or_bad_token_fails_closed(wired):
    with pytest.raises(cp.ChokepointError):
        cp.process(_req(token=""), period="2026-06")
    with pytest.raises(cp.ChokepointError):
        cp.process(_req(token="not json"), period="2026-06")


def test_token_without_tenant_fails_closed(wired):
    # claims_to_tags raises ClaimsError on a missing tenant -> ChokepointError.
    bad = json.dumps({"sub": "u1", "affiliation": "student"})  # no tenant
    with pytest.raises(cp.ChokepointError):
        cp.process(_req(token=bad), period="2026-06")
    assert wired.calls == 0


def test_missing_messages_rejected(wired):
    # Messages are required; a missing model is NOT an error (it means auto, #190).
    with pytest.raises(cp.ChokepointError):
        cp.process(_req(messages=[]), period="2026-06")
    # model=None routes via auto rather than rejecting.
    wired.spend, wired.budget = 0.0, 100.0
    out = cp.process(_req(model=None), period="2026-06")
    assert out["text"] == "answer"


def test_estimate_input_tokens_is_server_side():
    # char/4 + 1, conservative round-up; no client override accepted.
    assert cp.estimate_input_tokens([{"role": "user", "content": "x" * 40}]) == 11
    assert cp.estimate_input_tokens([]) == 1


def test_estimate_input_tokens_charges_for_images():
    # #244 H1: each attached figure adds a conservative token charge so the pre-call gate can't be
    # under-run by attaching images (the chokepoint's exact-spend guarantee).
    base = cp.estimate_input_tokens([{"role": "user", "content": "x" * 40}])
    with_img = cp.estimate_input_tokens(
        [{"role": "user", "content": "x" * 40, "images": [_PNG, _PNG]}]
    )
    assert with_img == base + 2 * cp.IMAGE_TOKEN_CHARGE


def test_to_converse_messages_folds_system_into_first_user_turn():
    # Bedrock Converse has no system role, and the oss tier rejects system messages;
    # the SPA's RAG path prepends grounding as a system message. Fold it into the
    # first user turn so every model accepts it and grounding precedes the question.
    msgs = [
        {"role": "system", "content": "CONTEXT: agate is a gateway."},
        {"role": "user", "content": "what is it?"},
    ]
    out = cp.to_converse_messages(msgs)
    assert [m["role"] for m in out] == ["user"]  # no system role survives
    text = out[0]["content"][0]["text"]
    assert text.startswith("CONTEXT: agate is a gateway.")
    assert text.endswith("what is it?")


def test_to_converse_messages_no_system_is_passthrough():
    msgs = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "yo"}]
    out = cp.to_converse_messages(msgs)
    assert out == [
        {"role": "user", "content": [{"text": "hi"}]},
        {"role": "assistant", "content": [{"text": "yo"}]},
    ]


def test_to_converse_messages_system_only_becomes_user_turn():
    out = cp.to_converse_messages([{"role": "system", "content": "ctx"}])
    assert out == [{"role": "user", "content": [{"text": "ctx"}]}]


def test_to_converse_messages_attaches_png_figure_as_image_block():
    # Canvas result→prompt loop (#244): a user turn's `images` become Converse image blocks
    # after the text, with raw PNG bytes.
    out = cp.to_converse_messages([{"role": "user", "content": "interpret", "images": [_PNG]}])
    assert out[0]["role"] == "user"
    assert out[0]["content"][0] == {"text": "interpret"}
    img = out[0]["content"][1]
    assert img["image"]["format"] == "png"
    assert isinstance(img["image"]["source"]["bytes"], (bytes, bytearray))
    assert len(img["image"]["source"]["bytes"]) > 0


def test_to_converse_messages_skips_non_png_image():
    # A non-PNG / malformed data-URI is never forwarded as an arbitrary blob.
    out = cp.to_converse_messages(
        [
            {
                "role": "user",
                "content": "x",
                "images": ["javascript:alert(1)", "data:text/html;base64,zz"],
            }
        ]
    )
    assert out[0]["content"] == [{"text": "x"}]


def test_image_blocks_rejects_png_prefix_with_non_png_bytes():
    # #244 M2: a data-URI with the png prefix but arbitrary (non-PNG-magic) bytes is skipped —
    # a saved notebook is untrusted, so we validate the DECODED bytes, not just the prefix.
    import base64 as _b64

    fake = "data:image/png;base64," + _b64.b64encode(b"not a real png").decode()
    assert cp._image_blocks([fake]) == []
    # And a real PNG still passes.
    assert len(cp._image_blocks([_PNG])) == 1


def test_image_blocks_caps_count():
    # #244 M3: never forward more than the per-request image cap.
    out = cp._image_blocks([_PNG] * (cp._MAX_IMAGES + 5))
    assert len(out) == cp._MAX_IMAGES


def test_handler_with_system_message_never_sends_system_role(wired):
    # End-to-end through process(): a system message must not reach Converse as a
    # system role (the live oss model 500s on it). Capture what the fake gets.
    seen = {}

    def capture_converse(modelId, messages, inferenceConfig):  # noqa: N803
        seen["messages"] = messages
        return {
            "output": {"message": {"content": [{"text": "answer"}]}},
            "usage": {"inputTokens": 12, "outputTokens": 8},
        }

    wired._br.converse = capture_converse
    wired.spend, wired.budget = 1.0, 100.0
    req = _req()
    req["messages"] = [
        {"role": "system", "content": "grounding"},
        {"role": "user", "content": "q?"},
    ]
    cp.process(req, period="2026-06")
    assert all(m["role"] != "system" for m in seen["messages"])
    assert "grounding" in seen["messages"][0]["content"][0]["text"]


def test_handler_maps_reject_to_402(wired):
    wired.spend, wired.budget = 0.0, 0.0
    resp = cp.handler({"body": json.dumps(_req())}, None)
    assert resp["statusCode"] == 402
    assert "budget_rejected" in resp["body"]


def test_402_carries_a_machine_code(wired):
    # agate#265 C1: the 402 body classifies WHY it was rejected so a caller (quarry) doesn't
    # string-match. A real budget breach → budget_exceeded; a bad token → token_invalid.
    wired.spend, wired.budget = 0.0, 0.0
    body = json.loads(cp.handler({"body": json.dumps(_req())}, None)["body"])
    assert body["code"] == "budget_exceeded"

    bad = cp.handler({"body": json.dumps({"messages": [{"role": "user", "content": "x"}]})}, None)
    assert bad["statusCode"] == 402
    assert json.loads(bad["body"])["code"] == "token_invalid"


def test_402_bad_request_code_for_missing_messages(wired):
    resp = cp.handler({"body": json.dumps({"idp_token": _token(), "messages": []})}, None)
    assert resp["statusCode"] == 402
    assert json.loads(resp["body"])["code"] == "bad_request"


def test_handler_200_on_allow(wired):
    wired.spend, wired.budget = 0.0, 100.0
    resp = cp.handler({"body": json.dumps(_req())}, None)
    assert resp["statusCode"] == 200
    assert "answer" in resp["body"]


# --- #81 budget cascade (Tier-1 hierarchical scope) --------------------------


def test_no_scope_token_unchanged_no_scope_io(wired):
    # Regression guard: a token with no data_scope behaves exactly as before — no
    # scope budget reads and no scope spend writes.
    wired.spend, wired.budget = 1.0, 100.0
    out = cp.process(_req(), period="2026-06")
    assert out["text"] == "answer" and wired.calls == 1
    assert wired.scope_writes == []  # nothing written to scope rows


def test_scoped_session_within_all_budgets_allows_and_records_scope_spend(wired):
    wired.spend, wired.budget = 0.0, 100.0
    wired.scope_budgets = {"arts-sci": 100.0, "arts-sci/chemistry": 100.0}
    out = cp.process(_req(token=_token(data_scope="arts-sci/chemistry")), period="2026-06")
    assert out["text"] == "answer" and wired.calls == 1
    # write-on-allow: EXACTLY the two ancestor scope rows, with the actual-usage cost,
    # and NOT a user/tenant row (those stay with the async meter — no double count).
    written_nodes = [n for n, _ in wired.scope_writes]
    assert written_nodes == ["arts-sci", "arts-sci/chemistry"]
    assert all(cost > 0 for _, cost in wired.scope_writes)


def test_ancestor_budget_breach_rejects_and_names_node(wired):
    wired.spend, wired.budget = 0.0, 100.0
    # The DEPT node is exhausted; the request must be rejected naming it, no call.
    wired.scope_budgets = {"arts-sci": 100.0, "arts-sci/chemistry": 0.0}
    with pytest.raises(cp.ChokepointError, match="scope:arts-sci/chemistry"):
        cp.process(_req(token=_token(data_scope="arts-sci/chemistry")), period="2026-06")
    assert wired.calls == 0
    assert wired.scope_writes == []  # nothing invoked, nothing recorded


def test_scope_node_without_budget_is_skipped(wired):
    wired.spend, wired.budget = 0.0, 100.0
    # No budget rows for any scope node -> no cap there; user budget passes -> allow.
    out = cp.process(_req(token=_token(data_scope="arts-sci/chemistry")), period="2026-06")
    assert out["text"] == "answer" and wired.calls == 1


def test_body_supplied_data_scope_is_ignored(wired):
    # SEC-1: scope comes from the TOKEN (tags.scope), not the request body. A body
    # data_scope must not create scope checks/writes for an unscoped token.
    wired.spend, wired.budget = 1.0, 100.0
    out = cp.process(_req(data_scope="law/evil"), period="2026-06")  # body field
    assert out["text"] == "answer"
    assert wired.scope_writes == []  # token had no scope -> no scope activity
