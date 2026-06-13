"""Unit tests for the demo IdP pre-token trigger. No AWS."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from infra.functions.demo_idp import pretoken  # noqa: E402


def _event(**attrs):
    return {"request": {"userAttributes": attrs}}


def test_maps_custom_attrs_to_top_level_claims():
    ev = pretoken.handler(
        _event(
            **{
                "custom:affiliation": "researcher",
                "custom:tenant": "kempner",
                "custom:courses": "CS50,CS51",
                "custom:grant": "true",
            }
        ),
        None,
    )
    add = ev["response"]["claimsOverrideDetails"]["claimsToAddOrOverride"]
    assert add == {
        "affiliation": "researcher",
        "tenant": "kempner",
        "courses": "CS50,CS51",
        "grant": "true",
    }


def test_omits_absent_attributes():
    ev = pretoken.handler(_event(**{"custom:tenant": "chem"}), None)
    add = ev["response"]["claimsOverrideDetails"]["claimsToAddOrOverride"]
    assert add == {"tenant": "chem"}
    assert "affiliation" not in add


def test_empty_attributes_yield_empty_overrides():
    ev = pretoken.handler(_event(), None)
    assert ev["response"]["claimsOverrideDetails"]["claimsToAddOrOverride"] == {}


def test_mapped_claims_feed_claims_to_tags():
    # End-to-end: the mapped claims are exactly what claims_to_tags consumes.
    from agg.tags import claims_to_tags

    ev = pretoken.handler(
        _event(
            **{
                "custom:affiliation": "student",
                "custom:tenant": "chem",
                "custom:courses": "CHEM-101",
            }
        ),
        None,
    )
    claims = dict(ev["response"]["claimsOverrideDetails"]["claimsToAddOrOverride"])
    claims["sub"] = "u1"
    tags = claims_to_tags(claims)
    assert tags.tenant == "chem"
    assert tags.tier == "oss"
    assert "CHEM-101" in tags.courses
