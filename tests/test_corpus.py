"""Pure tests for agate.corpus — the document key/prefix fence (#191)."""

from __future__ import annotations

import pytest
from agate.corpus import (
    CorpusKeyError,
    docs_list_prefix,
    docs_object_key,
    notebook_object_key,
    notebooks_list_prefix,
)


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


# --- saved notebooks (#200 slice 4) -----------------------------------------


def test_notebook_key_scoped_and_unscoped():
    assert notebook_object_key("chem", "chem-101", "abc") == "chem/chem-101/_notebooks/abc.json"
    assert notebook_object_key("chem", "", "abc") == "chem/_notebooks/abc.json"


def test_notebook_key_sanitises_id_and_rejects_empty():
    # A traversal-ish / dotted id is stripped to the id grammar; a single .json suffix is added.
    assert notebook_object_key("chem", "", "../../etc/passwd").endswith(
        "/_notebooks/etcpasswd.json"
    )
    with pytest.raises(CorpusKeyError):
        notebook_object_key("chem", "", "...")
    with pytest.raises(CorpusKeyError):
        notebook_object_key("", "", "abc")


def test_notebooks_prefix_is_fenced_and_slash_terminated():
    assert notebooks_list_prefix("chem", "chem-101") == "chem/chem-101/_notebooks/"
    assert notebooks_list_prefix("chem", "") == "chem/_notebooks/"
    assert notebooks_list_prefix("chem", "wk3").endswith("/_notebooks/")


def test_notebook_namespace_never_collides_with_a_document():
    # An uploaded filename can't land in the _notebooks namespace: the sanitiser strips the
    # leading underscore, so "_notebooks" becomes the ordinary file "notebooks" at the tenant
    # root — never the reserved `_notebooks/` prefix a saved notebook uses.
    assert docs_object_key("chem", "", "_notebooks") == "chem/notebooks"
    assert "_notebooks/" not in docs_object_key("chem", "", "_notebooks.txt")
