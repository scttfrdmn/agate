"""Pure RAG helpers — chunking, tenant-key derivation, vector assembly.

Side-effect-free and AWS-free (design §4: "data connects through identity"). The
ingest Lambda and any tooling import these so the tenancy/key logic has one tested
definition. The actual Bedrock-embeddings and S3-Vectors I/O live in the Lambda;
nothing here touches boto3.

Tenant isolation is the FERPA-critical invariant (security memo §6): a document's
tenant is derived ONLY from its S3 key prefix, never from caller-supplied data, so
a misplaced object cannot smuggle itself into another tenant's index.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

# S3 layout: s3://{agate-docs bucket}/{tenant}/{course?}/{path...}  (design §4).
# The first path segment IS the tenant (the isolation key). The OPTIONAL second
# segment may name a course (e.g. `chem/chem-101/week3.pdf`), which scopes the
# document to enrolled students via the session's agate:courses claim. A document
# directly under the tenant (`chem/handbook.pdf`) is tenant-wide (no course).
_TENANT_SEG = re.compile(r"^([a-zA-Z0-9._-]+)/(.+)$")
# A path segment that looks like a course id: letters+digits with an optional
# separator, e.g. `chem-101`, `cs50`, `BIO_220`. Deliberately conservative so a
# plain folder like `syllabus/` is NOT mistaken for a course.
_COURSE_SEG = re.compile(r"^[A-Za-z]{2,}[-_]?[0-9]{2,}[A-Za-z]?$")
# Metadata key carrying the course on each vector (filterable at query time).
COURSE_META_KEY = "course"


class TenantKeyError(ValueError):
    """The S3 key does not carry a usable tenant prefix — ingest must skip it."""


def tenant_from_s3_key(key: str) -> str:
    """Extract the tenant from an S3 object key's first path segment.

    `chem/syllabus/week1.pdf` -> `chem`. Raises if the key has no tenant prefix
    (e.g. a bare filename at the bucket root) so ingest fails closed rather than
    guessing a tenant.
    """
    m = _TENANT_SEG.match(key.lstrip("/"))
    if not m:
        raise TenantKeyError(f"S3 key has no tenant prefix: {key!r}")
    return m.group(1)


def course_from_s3_key(key: str) -> str | None:
    """Extract an optional course id from the SECOND path segment.

    `chem/chem-101/week3.pdf` -> `chem-101`; `chem/handbook.pdf` -> None (tenant-wide);
    `chem/syllabus/notes.pdf` -> None (a plain folder, not a course id). The course
    drives per-enrollment retrieval scope via the session's agate:courses claim, but
    it is NOT a security boundary on its own — the tenant index is the hard fence; the
    course filter narrows WITHIN the tenant the session can already read.

    NOTE (future work, hierarchical scope #70): this is the FLAT model — one course
    leaf directly under the tenant. The intended generalisation is a scope PATH
    (school/department/course for teaching, school/department/lab-or-project for
    research) used uniformly for RBAC, retrieval, AND budget cascade. A deeper key
    like `tenant/dept/course/...` currently yields None here (treated tenant-wide),
    which is safe (never over-scopes) but coarse until that model lands.
    """
    parts = key.lstrip("/").split("/")
    if len(parts) < 3:  # need tenant/<seg>/file at minimum for a course segment
        return None
    candidate = parts[1]
    return candidate if _COURSE_SEG.match(candidate) else None


def index_name_for_tenant(tenant: str) -> str:
    """The per-tenant S3 Vectors index name (design §4: one index per tenant)."""
    return f"agate-{tenant}"


def chunk_text(
    text: str,
    *,
    max_chars: int = 1200,
    overlap: int = 150,
) -> list[str]:
    """Split text into overlapping chunks on paragraph/sentence boundaries.

    Deterministic and dependency-free. Prefers to break at blank-line paragraph
    boundaries, falling back to a hard character window when a single block exceeds
    `max_chars`. Overlap preserves context across chunk edges for retrieval.
    """
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    if overlap < 0 or overlap >= max_chars:
        raise ValueError("overlap must be in [0, max_chars)")

    normalised = text.replace("\r\n", "\n").strip()
    if not normalised:
        return []

    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", normalised) if p.strip()]

    chunks: list[str] = []
    buf = ""
    for para in paragraphs:
        candidate = f"{buf}\n\n{para}" if buf else para
        if len(candidate) <= max_chars:
            buf = candidate
            continue
        # Flush what we have, then place this paragraph (windowing if oversized).
        if buf:
            chunks.append(buf)
        if len(para) <= max_chars:
            buf = para
        else:
            chunks.extend(_window(para, max_chars, overlap))
            buf = ""
    if buf:
        chunks.append(buf)

    return _apply_overlap(chunks, overlap)


def _window(s: str, max_chars: int, overlap: int) -> list[str]:
    """Hard character windowing for a single oversized block."""
    step = max_chars - overlap
    return [s[i : i + max_chars] for i in range(0, len(s), step)]


def _apply_overlap(chunks: list[str], overlap: int) -> list[str]:
    """Prefix each chunk (after the first) with the tail of the previous one."""
    if overlap == 0 or len(chunks) <= 1:
        return chunks
    out = [chunks[0]]
    # Deliberately unequal-length pairing (chunks vs chunks[1:]); strict=False.
    for prev, cur in zip(chunks, chunks[1:], strict=False):
        tail = prev[-overlap:]
        out.append(f"{tail}{cur}" if not cur.startswith(tail) else cur)
    return out


def course_filter(courses: tuple[str, ...] | list[str]) -> dict | None:
    """Build an S3 Vectors metadata filter scoping retrieval to a session's courses.

    A chunk is visible when EITHER it carries no `course` metadata (a tenant-wide
    document) OR its course is one the session is enrolled in. Returns the filter
    dict for QueryVectors, or None when the caller has no courses (then only
    tenant-wide docs are visible — course material is hidden, fail-closed).

    This narrows WITHIN the tenant index the credential already gates; it is a
    relevance/enrollment scope, not the security boundary (that's the per-tenant
    index + ABAC). The shape matches S3 Vectors' filter grammar ($in / $exists).
    """
    enrolled = [c for c in courses if c]
    # Tenant-wide docs (no course metadata) are always in scope.
    tenant_wide = {COURSE_META_KEY: {"$exists": False}}
    if not enrolled:
        return tenant_wide
    return {"$or": [tenant_wide, {COURSE_META_KEY: {"$in": enrolled}}]}


def vector_key(s3_key: str, chunk_index: int) -> str:
    """Stable, collision-resistant vector key for a document chunk.

    Re-ingesting the same object overwrites its chunks (idempotent) because the
    key is derived from the S3 key + chunk index, not random.
    """
    digest = hashlib.sha256(s3_key.encode("utf-8")).hexdigest()[:16]
    return f"{digest}:{chunk_index}"


@dataclass(frozen=True, slots=True)
class ChunkRecord:
    """One chunk ready to embed + store, with its non-filterable metadata."""

    key: str
    text: str
    metadata: dict[str, str | int] = field(default_factory=dict)


def build_chunk_records(s3_key: str, text: str, **chunk_kwargs) -> list[ChunkRecord]:
    """Chunk a document and assemble per-chunk records with source metadata.

    Metadata carries provenance (source key, chunk index) so retrieval results can
    cite their origin. The tenant is intentionally NOT trusted from metadata — it is
    enforced by the index the vectors are written to (one index per tenant).
    """
    tenant = tenant_from_s3_key(s3_key)
    course = course_from_s3_key(s3_key)
    records: list[ChunkRecord] = []
    for i, chunk in enumerate(chunk_text(text, **chunk_kwargs)):
        metadata: dict[str, str | int] = {
            "source_key": s3_key,
            "tenant": tenant,
            "chunk": i,
            # The chunk text itself, so QueryVectors can return it for
            # prompt injection without a second S3 fetch.
            "text": chunk,
        }
        # Course is filterable: retrieval narrows to the session's enrolled courses
        # (+ tenant-wide docs). Only set when the key actually names a course.
        if course is not None:
            metadata[COURSE_META_KEY] = course
        records.append(ChunkRecord(key=vector_key(s3_key, i), text=chunk, metadata=metadata))
    return records
