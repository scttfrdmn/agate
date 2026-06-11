"""Unit tests for the pure RAG helpers. No AWS."""

from __future__ import annotations

import pytest
from agg.rag import (
    ChunkRecord,
    TenantKeyError,
    build_chunk_records,
    chunk_text,
    index_name_for_tenant,
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
    assert index_name_for_tenant("chem") == "agg-chem"


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
