"""Corpus document keys + list prefixes (#191) — pure path helpers.

A user uploads documents into their OWN tenant+scope subtree of the docs bucket,
and lists what's there. The S3 key layout is the same one ingest/retrieval already
use: `{tenant}/{scope...}/{filename}` (see `agate.rag.tenant_from_s3_key` /
`scope_path_from_s3_key`). The prefix IS the access boundary, so every segment is
sanitised to the key grammar — a crafted scope or filename can't inject extra `/`
levels or `..` that escape the `{tenant}/{scope}/` fence, and can't masquerade as a
reserved namespace (`_agents/`, `_rooms/`, `_sessions/`, `_mm-artifacts/`). Tenant
and scope come from the VERIFIED token (claims→tags), never a request field.

Pure + AWS-free; unit-tested without S3.
"""

from __future__ import annotations

from agate.budget import _clean_id, normalise_scope

# Reserved first-segment-after-scope names the corpus must never write into, so an
# uploaded file can't impersonate an agent/room/session record or the multimodal
# artifact store. A filename normalising to one of these is rejected.
_RESERVED_SEGMENTS = frozenset({"_agents", "_rooms", "_sessions", "_mm-artifacts"})


class CorpusKeyError(ValueError):
    """A tenant/scope/filename that cannot form a safe, in-fence corpus key."""


def _clean_filename(raw: str) -> str:
    """Sanitise an uploaded filename to a single safe path segment.

    Keeps the basename only (drops any directory parts the client sent), strips it to
    the id grammar, and preserves a single extension. Returns "" if nothing usable
    remains. No `/`, no `..`, no reserved-namespace prefix survives."""
    base = (raw or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not base:
        return ""
    # Split a trailing extension so a dotted name keeps one readable suffix.
    stem, dot, ext = base.rpartition(".")
    if dot and stem:
        cleaned = f"{_clean_id(stem)}.{_clean_id(ext)}" if _clean_id(ext) else _clean_id(stem)
    else:
        cleaned = _clean_id(base)
    cleaned = cleaned.strip("._")  # no leading dot/underscore → can't hit a reserved name
    return cleaned


def docs_object_key(tenant: str, scope: str, filename: str) -> str:
    """The S3 key for an uploaded document: `{tenant}/{scope}/{filename}`, or
    `{tenant}/{filename}` when unscoped (tenant root). Mirrors `agent_object_key`.

    Raises `CorpusKeyError` if tenant or filename don't normalise, or the filename
    reduces to a reserved namespace segment."""
    t = _clean_id(tenant)
    if not t:
        raise CorpusKeyError("tenant is required for a corpus key")
    name = _clean_filename(filename)
    if not name:
        raise CorpusKeyError("filename did not normalise to a valid name")
    if name in _RESERVED_SEGMENTS:
        raise CorpusKeyError(f"filename collides with a reserved namespace: {name}")
    norm_scope = normalise_scope(scope) if scope else ""
    prefix = f"{t}/{norm_scope}" if norm_scope else t
    return f"{prefix}/{name}"


def docs_list_prefix(tenant: str, scope: str) -> str:
    """The S3 list prefix for a session's in-scope documents: `{tenant}/{scope}/` or
    `{tenant}/` when unscoped. Always ends in `/` so it matches only that subtree
    (not a sibling whose name shares a string prefix). Raises if tenant is empty."""
    t = _clean_id(tenant)
    if not t:
        raise CorpusKeyError("tenant is required for a corpus list prefix")
    norm_scope = normalise_scope(scope) if scope else ""
    return f"{t}/{norm_scope}/" if norm_scope else f"{t}/"
