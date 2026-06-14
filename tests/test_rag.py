"""Unit tests for the pure RAG helpers. No AWS."""

from __future__ import annotations

import pytest
from agate.rag import (
    COURSE_META_KEY,
    SCOPE_META_KEY,
    ChunkRecord,
    TenantKeyError,
    ancestors,
    build_chunk_records,
    chunk_text,
    course_filter,
    course_from_s3_key,
    index_name_for_tenant,
    retrieval_nodes,
    scope_filter,
    scope_path_from_s3_key,
    tenant_from_s3_key,
    vector_key,
)

# --- tenant derivation (the FERPA-critical invariant) -----------------------


@pytest.mark.parametrize(
    "key,tenant",
    [
        ("chem/syllabus/week1.pdf", "chem"),
        ("/chem/a.txt", "chem"),  # leading slash tolerated
        ("kempner-lab/data/notes.md", "kempner-lab"),
        ("CHEM-101/x", "CHEM-101"),
    ],
)
def test_tenant_from_s3_key(key, tenant):
    assert tenant_from_s3_key(key) == tenant


@pytest.mark.parametrize("key", ["bare.pdf", "", "/", "noslashhere"])
def test_tenant_from_s3_key_fails_closed(key):
    with pytest.raises(TenantKeyError):
        tenant_from_s3_key(key)


def test_index_name_per_tenant():
    assert index_name_for_tenant("chem") == "agate-chem"


# --- course derivation (per-enrollment scope) -------------------------------


@pytest.mark.parametrize(
    "key,course",
    [
        ("chem/chem-101/week3.pdf", "chem-101"),  # 2nd segment is a course id
        ("chem/cs50/notes.txt", "cs50"),
        ("chem/BIO_220/x.md", "BIO_220"),
        ("chem/handbook.pdf", None),  # directly under tenant -> tenant-wide
        ("chem/syllabus/notes.pdf", None),  # plain folder, not a course id
        ("chem/", None),  # no third segment
    ],
)
def test_course_from_s3_key(key, course):
    assert course_from_s3_key(key) == course


def test_build_chunk_records_tags_course_when_present():
    recs = build_chunk_records("chem/chem-101/w.txt", "Some content about acids.")
    assert recs and recs[0].metadata[COURSE_META_KEY] == "chem-101"


def test_build_chunk_records_omits_course_for_tenant_wide_doc():
    recs = build_chunk_records("chem/handbook.txt", "General policy text.")
    assert recs and COURSE_META_KEY not in recs[0].metadata


# --- course filter (retrieval scope) ----------------------------------------


def test_course_filter_no_courses_only_tenant_wide():
    # No enrollment -> only docs WITHOUT a course are visible (fail-closed).
    f = course_filter([])
    assert f == {COURSE_META_KEY: {"$exists": False}}


def test_course_filter_enrolled_includes_tenant_wide_or_enrolled():
    f = course_filter(["chem-101", "chem-102"])
    assert "$or" in f
    branches = f["$or"]
    assert {COURSE_META_KEY: {"$exists": False}} in branches
    assert {COURSE_META_KEY: {"$in": ["chem-101", "chem-102"]}} in branches


# --- hierarchical scope (#70) -----------------------------------------------


@pytest.mark.parametrize(
    "key,scope",
    [
        ("chem/chemistry/chem-101/wk3.pdf", "chemistry/chem-101"),  # school/dept/course
        ("chem/chem-101/wk3.pdf", "chem-101"),  # flat (single scope segment)
        ("medicine/genetics/smith-lab/data.txt", "genetics/smith-lab"),  # research
        ("chem/handbook.pdf", None),  # tenant-wide, no scope
        ("chem/", None),
    ],
)
def test_scope_path_from_s3_key(key, scope):
    assert scope_path_from_s3_key(key) == scope


def test_ancestors_broad_to_specific():
    assert ancestors("chemistry/chem-101") == ["chemistry", "chemistry/chem-101"]
    assert ancestors("a/b/c") == ["a", "a/b", "a/b/c"]
    assert ancestors("solo") == ["solo"]


def test_build_chunk_records_writes_ancestor_list_for_hierarchical_key():
    recs = build_chunk_records("chem/chemistry/chem-101/wk.txt", "content here")
    assert recs and recs[0].metadata[SCOPE_META_KEY] == ["chemistry", "chemistry/chem-101"]


def test_build_chunk_records_no_scope_for_tenant_wide():
    recs = build_chunk_records("chem/handbook.txt", "policy")
    assert recs and SCOPE_META_KEY not in recs[0].metadata


def test_scope_filter_no_nodes_only_tenant_wide():
    # No scope -> only docs with neither scope nor course (true tenant-wide).
    f = scope_filter([])
    assert f == {
        "$and": [{SCOPE_META_KEY: {"$exists": False}}, {COURSE_META_KEY: {"$exists": False}}]
    }


def test_scope_filter_subtree_membership_and_backward_compat():
    # A chair at "chemistry" sees the subtree (scope_ancestors $in) AND flat course
    # docs whose course matches a node (backward compat).
    f = scope_filter(["chemistry", "chemistry/chem-101"])
    branches = f["$or"]
    assert {SCOPE_META_KEY: {"$in": ["chemistry", "chemistry/chem-101"]}} in branches
    assert {COURSE_META_KEY: {"$in": ["chemistry", "chemistry/chem-101"]}} in branches


# --- retrieval_nodes (#84): scope source for the broker-proxied retriever -----


def test_retrieval_nodes_scope_ancestors_union_courses():
    # A chair at chemistry/chem-101 also enrolled in bio-200: subtree ∪ courses.
    nodes = retrieval_nodes("chemistry/chem-101", ("bio-200",))
    assert nodes == ["chemistry", "chemistry/chem-101", "bio-200"]


def test_retrieval_nodes_courses_only_when_unconfined():
    # Most students: no data_scope -> just their courses (+ tenant-wide via scope_filter).
    assert retrieval_nodes("", ("chem-101", "chem-202")) == ["chem-101", "chem-202"]


def test_retrieval_nodes_empty_both_is_fail_closed():
    # Empty -> [] -> scope_filter([]) is tenant-wide-only (never broader than tenant).
    assert retrieval_nodes("", ()) == []
    assert scope_filter(retrieval_nodes("", ())) == scope_filter([])


def test_retrieval_nodes_dedupes_course_already_in_subtree():
    # A course equal to a scope node isn't duplicated.
    assert retrieval_nodes("chemistry", ("chemistry", "bio-200")) == ["chemistry", "bio-200"]


def test_mm_index_name_is_text_index_plus_mm_suffix():
    from agate.rag import index_name_for_tenant, mm_index_name_for_tenant

    assert mm_index_name_for_tenant("chem") == "agate-chem-mm"
    # distinct from the 1024-dim text index
    assert mm_index_name_for_tenant("chem") != index_name_for_tenant("chem")


# --- chunking ---------------------------------------------------------------


def test_chunk_empty_returns_nothing():
    assert chunk_text("") == []
    assert chunk_text("   \n\n  ") == []


def test_chunk_small_text_is_single_chunk():
    assert chunk_text("hello world") == ["hello world"]


def test_chunk_splits_on_paragraphs_within_limit():
    text = "para one.\n\npara two.\n\npara three."
    chunks = chunk_text(text, max_chars=20, overlap=0)
    # each paragraph is short; they pack until the 20-char limit
    assert all(len(c) <= 25 for c in chunks)  # +overlap headroom
    assert "para one." in chunks[0]


def test_chunk_windows_an_oversized_paragraph():
    big = "x" * 5000
    chunks = chunk_text(big, max_chars=1000, overlap=100)
    assert len(chunks) > 1
    assert all(len(c) <= 1000 for c in chunks)


def test_chunk_rejects_bad_overlap():
    with pytest.raises(ValueError):
        chunk_text("abc", max_chars=10, overlap=10)
    with pytest.raises(ValueError):
        chunk_text("abc", max_chars=0)


# --- vector keys ------------------------------------------------------------


def test_vector_key_is_stable_and_indexed():
    k0 = vector_key("chem/a.pdf", 0)
    k1 = vector_key("chem/a.pdf", 1)
    assert k0 != k1
    assert k0.endswith(":0") and k1.endswith(":1")
    # stable across calls (idempotent re-ingest)
    assert vector_key("chem/a.pdf", 0) == k0


def test_vector_key_differs_by_document():
    assert vector_key("chem/a.pdf", 0) != vector_key("chem/b.pdf", 0)


# --- record assembly --------------------------------------------------------


def test_build_chunk_records_carries_provenance_and_tenant():
    recs = build_chunk_records("chem/syllabus.txt", "hello world", max_chars=1000)
    assert len(recs) == 1
    r = recs[0]
    assert isinstance(r, ChunkRecord)
    assert r.metadata["source_key"] == "chem/syllabus.txt"
    assert r.metadata["tenant"] == "chem"
    assert r.metadata["chunk"] == 0
    assert r.metadata["text"] == "hello world"


def test_build_chunk_records_fails_closed_without_tenant():
    with pytest.raises(TenantKeyError):
        build_chunk_records("bare.txt", "hello")
