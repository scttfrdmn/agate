"""Pure tests for agate.corpus — the document key/prefix fence (#191)."""

from __future__ import annotations

import pytest
from agate.corpus import CorpusKeyError, docs_list_prefix, docs_object_key


def test_object_key_scoped():
    assert docs_object_key("chem", "chemistry/chem-101", "notes.pdf") == (
        "chem/chemistry/chem-101/notes.pdf"
    )


def test_object_key_unscoped_is_tenant_root():
    assert docs_object_key("chem", "", "handbook.txt") == "chem/handbook.txt"


def test_object_key_keeps_only_the_basename():
    # A client-supplied path is reduced to its basename — no directory escape.
    assert docs_object_key("chem", "wk3", "../../etc/passwd") == "chem/wk3/passwd"
    assert docs_object_key("chem", "wk3", "a/b/c/file.md") == "chem/wk3/file.md"


def test_object_key_rejects_traversal_scope():
    # ".." scope normalises away → treated as unscoped (tenant root), never escapes.
    assert docs_object_key("chem", "../secret", "f.txt") == "chem/f.txt"


def test_reserved_namespace_filename_is_sanitised_not_collided():
    # A leading-underscore name can't impersonate a reserved namespace: the underscore
    # is stripped, so "_agents" becomes "agents" (a plain doc), never "_agents/".
    assert docs_object_key("chem", "wk3", "_agents") == "chem/wk3/agents"
    assert docs_object_key("chem", "wk3", "_rooms.txt") == "chem/wk3/rooms.txt"


def test_object_key_requires_tenant_and_filename():
    with pytest.raises(CorpusKeyError):
        docs_object_key("", "wk3", "f.txt")
    with pytest.raises(CorpusKeyError):
        docs_object_key("chem", "wk3", "///")


def test_object_key_preserves_a_single_extension():
    assert docs_object_key("chem", "", "Lecture Notes.PDF") == "chem/LectureNotes.PDF"


def test_list_prefix_scoped_and_unscoped():
    assert docs_list_prefix("chem", "chemistry/chem-101") == "chem/chemistry/chem-101/"
    assert docs_list_prefix("chem", "") == "chem/"


def test_list_prefix_always_ends_in_slash():
    # So "chem/" never matches a sibling tenant "chemistry/...".
    assert docs_list_prefix("chem", "").endswith("/")
    assert docs_list_prefix("chem", "wk3").endswith("/")


def test_list_prefix_requires_tenant():
    with pytest.raises(CorpusKeyError):
        docs_list_prefix("", "wk3")
