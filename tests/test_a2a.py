"""Unit tests for A2A external-peer admission — the governance core (#119 slice). No AWS.

The §8.6/§4/§10 invariant: an external A2A peer is bounded by an ASSUMED ROLE, not its card.
Its authority = delegate(caller, request) — scope ∩, tier = min, tenant fixed, disjoint
rejected — independent of whatever the card advertises. The card requests; the credential
decides.
"""

from __future__ import annotations

import pytest
from agate.a2a import A2AError, PeerRequest, admit_peer, peer_cascade_nodes
from agate.tags import SessionTags


def _caller(scope="lab", tier="frontier", tenant="uni", aff="researcher"):
    return SessionTags(
        affiliation=aff, tenant=tenant, courses=(), tier=tier, role="member", scope=scope
    )


# --- THE HEADLINE: card is a claim, assumed-role is authority ----------------


def test_peer_clamped_to_requested_scope_within_caller():
    a = admit_peer(
        _caller("lab"),
        PeerRequest(name="collab", requested_scope="lab/photonics", requested_role="researcher"),
        subject="prof",
    )
    assert a.child_tags.scope == "lab/photonics"  # narrowed to the request, within the caller
    assert a.child_tags.tenant == "uni"  # tenant fixed to the caller
    assert a.child_tags.role == "member"  # an external peer is never admin


def test_tier_clamps_to_min_of_caller_and_request():
    # caller mid + peer requests researcher (frontier) -> min = mid (never the card's claim)
    a = admit_peer(
        _caller("lab", tier="mid"),
        PeerRequest(name="x", requested_scope="lab", requested_role="researcher"),
        subject="p",
    )
    assert a.child_tags.tier == "mid"


def test_disjoint_request_refused():
    # the card trying to reach outside the caller's subtree is rejected, fail-closed
    with pytest.raises(A2AError):
        admit_peer(
            _caller("chemistry"),
            PeerRequest(name="evil", requested_scope="physics"),
            subject="p",
        )


def test_authority_depends_only_on_caller_and_request_not_other_card_fields():
    # Two peers with the SAME (requested_scope, requested_role) but wildly different
    # name/origin produce the SAME credential — no other card field influences authority.
    base = _caller("lab", tier="mid")
    a = admit_peer(
        base,
        PeerRequest(
            name="a", requested_scope="lab/x", requested_role="ta", origin="https://good.example"
        ),
        subject="p",
    )
    b = admit_peer(
        base,
        PeerRequest(
            name="b-totally-different",
            requested_scope="lab/x",
            requested_role="ta",
            origin="https://evil.example",
        ),
        subject="p",
    )
    assert a.child_tags == b.child_tags  # identical credential — the card name/origin is inert


# --- monotonic / transitive -------------------------------------------------


def test_peer_of_a_narrowed_caller_stays_within_it():
    # a peer admitted from an already-narrowed caller can't exceed that caller (#106 transitivity)
    narrowed = _caller("lab/photonics", tier="oss")
    a = admit_peer(
        narrowed,
        # asks for broader scope + higher tier than the narrowed caller holds:
        PeerRequest(name="p", requested_scope="lab", requested_role="researcher"),
        subject="prof",
    )
    assert a.child_tags.scope == "lab/photonics"  # clamped to the narrowed caller
    assert a.child_tags.tier == "oss"  # clamped to the narrowed caller


# --- external attribution ----------------------------------------------------


def test_peer_hop_attributed_as_external_on_the_root_users_authority():
    a = admit_peer(
        _caller("lab", tenant="uni"),
        PeerRequest(name="collab", requested_scope="lab", origin="https://peer.example"),
        subject="prof",
    )
    assert a.acting_as.agent == "uni/external-collab"  # the external- marker
    assert a.acting_as.on_behalf_of == "uni@prof"  # the verified ROOT user, not the peer
    assert a.acting_as.attributed is True
    assert a.acting_as.remit["external"] is True
    assert a.acting_as.remit["origin"] == "https://peer.example"  # untrusted provenance


def test_peer_id_is_injection_safe():
    # a name with / : .. can't escape the {tenant}/external-… id
    a = admit_peer(
        _caller("lab"),
        PeerRequest(name="../evil:bot", requested_scope="lab"),
        subject="p",
    )
    assert a.acting_as.agent.count("/") == 1  # exactly tenant / name
    assert ":" not in a.acting_as.agent  # colon stripped by _clean_id


def test_origin_cannot_change_the_tenant():
    # a cross-tenant origin is provenance only — the tenant is fixed to the caller's
    a = admit_peer(
        _caller("lab", tenant="uni"),
        PeerRequest(name="x", requested_scope="lab", origin="other-tenant.example"),
        subject="p",
    )
    assert a.child_tags.tenant == "uni"
    assert a.acting_as.agent.startswith("uni/")


# --- family budget -----------------------------------------------------------


def test_peer_cascade_has_caller_ancestors_plus_the_peer():
    a = admit_peer(
        _caller("lab"), PeerRequest(name="collab", requested_scope="lab"), subject="prof"
    )
    nodes = peer_cascade_nodes(("uni@prof", "root"), a, lambda label: (0.0, 10.0))
    labels = [n[0] for n in nodes]
    assert labels == ["uni@prof", "root", "uni/external-collab"]


def test_peer_call_rejected_when_an_ancestor_budget_is_breached():
    from cost import evaluate_priced_cascade

    a = admit_peer(
        _caller("lab"), PeerRequest(name="collab", requested_scope="lab"), subject="prof"
    )
    # the caller's "root" ancestor is at its cap -> a priced peer call is rejected there
    nodes = peer_cascade_nodes(
        ("uni@prof", "root"),
        a,
        lambda label: (0.0, 0.000001) if label == "root" else (0.0, None),
    )
    res = evaluate_priced_cascade(price_usd=0.5, nodes=nodes)
    assert res.decision == "reject"
    assert res.breaching_node == "root"
