"""Unit tests for connectors — the bounded ingestion-target core (#133). No AWS.

The headline (§8.6 / §10): a connector chooses a destination KEY inside its connecting user's
`{tenant}/{scope}/` subtree — it never gains a new credential or a wider read. Whatever the
SOURCE names an item, the dest key lands inside the authorized subtree, so the #80 IAM Deny +
#84 scope filter (already proven live) confine it exactly like an uploaded document.
"""

from __future__ import annotations

import pytest
from agate.connectors import (
    ConnectorError,
    all_connectors,
    confine_dest_key,
    connector_dest_key,
    get_connector,
)
from agate.rag import ancestors, scope_path_from_s3_key, tenant_from_s3_key

# --- registry ----------------------------------------------------------------


def test_six_sources_register_with_expected_auth_modes():
    by_kind = {c.kind: c for c in all_connectors()}
    assert set(by_kind) == {"gdrive", "box", "teams", "discord", "s3", "nfs"}
    assert by_kind["gdrive"].auth_mode == "user-oauth"
    assert by_kind["box"].auth_mode == "user-oauth"
    assert by_kind["teams"].auth_mode == "user-oauth"
    assert by_kind["discord"].auth_mode == "user-oauth"
    assert by_kind["s3"].auth_mode == "scoped-role"
    assert by_kind["nfs"].auth_mode == "ingest-lambda"


def test_user_oauth_sources_are_gateway_targets():
    by_kind = {c.kind: c for c in all_connectors()}
    assert by_kind["gdrive"].gateway_target is True
    assert by_kind["s3"].gateway_target is False  # direct via the #80 role, no Gateway


def test_unknown_connector_fails_closed():
    with pytest.raises(ConnectorError):
        get_connector("dropbox")
    with pytest.raises(ConnectorError):
        connector_dest_key(tenant="chem", scope="chemistry", connector="dropbox", item_path="x")


# --- the headline: confinement to {tenant}/{scope}/ --------------------------


def test_dest_key_lands_under_tenant_scope():
    k = connector_dest_key(
        tenant="chem",
        scope="chemistry/chem-101",
        connector="gdrive",
        item_path="Shared/notes.gdoc",
    )
    assert k.startswith("chem/chemistry/chem-101/_connectors/gdrive/")
    assert tenant_from_s3_key(k) == "chem"
    assert confine_dest_key("chem", "chemistry/chem-101", k) is True


def test_connecting_users_scope_is_an_ancestor_of_the_ingest_key():
    # The #84 scope_filter matches when the session's node is in the chunk's ancestor list.
    # The connector ingests UNDER the user's scope, so the user's scope is an ancestor of the
    # ingest key's scope path — retrieval will see it.
    k = connector_dest_key(
        tenant="chem", scope="chemistry/chem-101", connector="box", item_path="f.txt"
    )
    assert "chemistry/chem-101" in ancestors(scope_path_from_s3_key(k))


@pytest.mark.parametrize(
    "item",
    [
        "../../physics/secret.doc",
        "/etc/passwd",
        "a/../../b",
        "....//x",
        "./../../../../root",
    ],
)
def test_adversarial_item_path_cannot_escape_subtree(item):
    # No source-supplied filename can walk out of the connector's tenant+scope subtree.
    k = connector_dest_key(
        tenant="chem", scope="chemistry/chem-101", connector="box", item_path=item
    )
    assert confine_dest_key("chem", "chemistry/chem-101", k) is True
    # and it never reaches a sibling scope like physics at the scope root
    assert not scope_path_from_s3_key(k).startswith("physics")


def test_traversal_scope_falls_to_tenant_wide_never_sibling():
    # A `..` in the scope is rejected by normalise_scope → tenant-wide, NOT the escaped path.
    k = connector_dest_key(
        tenant="chem", scope="chemistry/../physics", connector="s3", item_path="f.txt"
    )
    assert k.startswith("chem/_connectors/")  # tenant-wide fallback, inside the tenant fence
    assert tenant_from_s3_key(k) == "chem"


def test_empty_scope_stays_inside_tenant_fence():
    k = connector_dest_key(tenant="chem", scope="", connector="nfs", item_path="lab/run1.csv")
    assert k.startswith("chem/_connectors/nfs/")
    assert tenant_from_s3_key(k) == "chem"


def test_empty_tenant_rejected():
    with pytest.raises(ConnectorError):
        connector_dest_key(tenant="", scope="chemistry", connector="s3", item_path="x")


def test_empty_item_path_still_yields_a_leaf():
    k = connector_dest_key(tenant="chem", scope="chemistry", connector="s3", item_path="")
    assert k == "chem/chemistry/_connectors/s3/item"


# --- round-trip through the ingest parsers (the real fence) ------------------


def test_dest_key_round_trips_through_ingest_parsers():
    # The key a connector writes must be one the existing ingest pipeline (#84) parses to the
    # SAME tenant + a within-subtree scope — so retrieval confines it with no new code.
    k = connector_dest_key(
        tenant="chem", scope="chemistry/chem-101", connector="teams", item_path="ch/msg.txt"
    )
    assert tenant_from_s3_key(k) == "chem"
    assert scope_path_from_s3_key(k).startswith("chemistry/chem-101/")


def test_confine_rejects_cross_tenant_key():
    # A key under a DIFFERENT tenant is not confined for `chem`.
    other = connector_dest_key(tenant="physics", scope="phys", connector="s3", item_path="f.txt")
    assert confine_dest_key("chem", "chemistry", other) is False
