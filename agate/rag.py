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


# --- Hierarchical scope (#70): school/dept/course (+ lab/project) -------------
# Metadata key carrying a document's ancestor-path list (filterable). S3 Vectors
# has no prefix operator, so subtree visibility is achieved by storing every
# ancestor-or-self path on the doc and matching the session's node with $in
# (validated live, #70). The scope path is the directory portion BETWEEN the
# tenant and the filename: `chem/chemistry/chem-101/wk3.pdf` -> `chemistry/chem-101`.
SCOPE_META_KEY = "scope_ancestors"


def scope_path_from_s3_key(key: str) -> str | None:
    """The hierarchical scope path (tenant excluded, filename excluded), or None.

    `chem/chemistry/chem-101/wk3.pdf` -> `chemistry/chem-101`
    `chem/chem-101/wk3.pdf`           -> `chem-101`         (flat = single segment)
    `chem/handbook.pdf`               -> None               (tenant-wide, no scope)
    The tenant is the hard ABAC fence; this path narrows WITHIN the tenant index.
    """
    parts = [p for p in key.lstrip("/").split("/") if p]
    if len(parts) < 3:  # tenant + at least one scope segment + filename
        return None
    return "/".join(parts[1:-1])


def ancestors(scope_path: str) -> list[str]:
    """Every ancestor-or-self prefix of a scope path, broad -> specific.

    `chemistry/chem-101` -> ["chemistry", "chemistry/chem-101"]. A doc stores this
    list; a session sitting at any of these nodes (its own scope) matches via $in,
    giving subtree visibility (a chair at `chemistry` sees every course under it).
    """
    segs = [s for s in scope_path.split("/") if s]
    return ["/".join(segs[: i + 1]) for i in range(len(segs))]


def scope_filter(scope_nodes: tuple[str, ...] | list[str]) -> dict:
    """S3 Vectors filter scoping retrieval to a session's hierarchy node(s).

    A chunk is visible when it is TRULY tenant-wide (neither `scope_ancestors` nor
    `course` set) OR one of the session's scope nodes is in the chunk's ancestor
    list (subtree visibility) OR — for docs written under the flat model — its
    `course` matches a node (backward compat). Empty nodes -> only tenant-wide docs
    (fail-closed). Generalises course_filter; a flat course is a single node.

    Mirrors `scopeFilter` in web/src/rag/retriever.ts — keep the two in lockstep.
    """
    nodes = [n for n in scope_nodes if n]
    # Truly tenant-wide: no scope AND no course (so a no-scope session can't see
    # flat course material either — same fail-closed posture as course_filter).
    tenant_wide = {
        "$and": [{SCOPE_META_KEY: {"$exists": False}}, {COURSE_META_KEY: {"$exists": False}}]
    }
    if not nodes:
        return tenant_wide
    return {
        "$or": [
            tenant_wide,
            {SCOPE_META_KEY: {"$in": nodes}},
            {COURSE_META_KEY: {"$in": nodes}},  # backward-compat with flat course docs
        ]
    }


def retrieval_nodes(scope: str, courses: tuple[str, ...] | list[str]) -> list[str]:
    """The scope-node list a session retrieves under: `ancestors(scope)` ∪ `courses`.

    Fed to `scope_filter` by the server-side retrieval proxy (#84). A session sees its
    hierarchical subtree (a chair at `chemistry` reaches `chemistry/chem-101` via the
    chunk's `scope_ancestors`) PLUS its flat enrolled courses, PLUS tenant-wide docs
    (handled by `scope_filter`). Order is broad→specific then courses; deduped.

    Fail-closed by construction: an unconfined session has scope=="" so
    `ancestors("")==[]`, and a no-course session contributes []; both empty → []
    → `scope_filter([])` returns tenant-wide-only. Never widens beyond the (separately
    IAM-enforced) tenant. Derived ONLY from verified session tags, never a request field.
    """
    nodes = list(ancestors(scope))
    seen = set(nodes)
    for c in courses:
        if c and c not in seen:
            seen.add(c)
            nodes.append(c)
    return nodes


def index_name_for_tenant(tenant: str) -> str:
    """The per-tenant TEXT S3 Vectors index name (design §4: one index per tenant)."""
    return f"agate-{tenant}"


def mm_index_name_for_tenant(tenant: str) -> str:
    """The per-tenant MULTIMODAL index name (the 3072-dim `-mm` index; #94).

    Mirrors infra/stacks/data.py's `_index(..., suffix="mm")`. Separate from the
    1024-dim text index; scoped by the same tenant ABAC tag + the same scope filter.
    """
    return f"agate-{tenant}-mm"


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
    metadata: dict[str, str | int | list[str]] = field(default_factory=dict)


def build_chunk_records(
    s3_key: str,
    text: str,
    *,
    source_system: str | None = None,
    source_item: str | None = None,
    **chunk_kwargs,
) -> list[ChunkRecord]:
    """Chunk a document and assemble per-chunk records with source metadata.

    Metadata carries provenance (source key, chunk index) so retrieval results can
    cite their origin. The tenant is intentionally NOT trusted from metadata — it is
    enforced by the index the vectors are written to (one index per tenant).

    For connector-ingested content (#133), `source_system` (e.g. `"gdrive"`) and
    `source_item` (the original source path/URL) attach non-filterable provenance so a
    retrieved chunk cites its source SYSTEM + item, not just the agate S3 key. Both are
    optional and additive: omitting them leaves the metadata exactly as an upload's. The
    tenant/scope are STILL derived from `s3_key` (the hard fence), never from these.
    """
    tenant = tenant_from_s3_key(s3_key)
    course = course_from_s3_key(s3_key)
    scope_path = scope_path_from_s3_key(s3_key)
    scope_ancestors = ancestors(scope_path) if scope_path else []
    records: list[ChunkRecord] = []
    for i, chunk in enumerate(chunk_text(text, **chunk_kwargs)):
        metadata: dict[str, str | int | list[str]] = {
            "source_key": s3_key,
            "tenant": tenant,
            "chunk": i,
            # The chunk text itself, so QueryVectors can return it for
            # prompt injection without a second S3 fetch.
            "text": chunk,
        }
        # Course is filterable (flat leaf, kept for backward compat): retrieval
        # narrows to the session's enrolled courses + tenant-wide docs.
        if course is not None:
            metadata[COURSE_META_KEY] = course
        # Hierarchical scope (#70): the ancestor-path list gives subtree visibility
        # via $in against the session's node(s). Set only when the key has a scope
        # path; a tenant-wide doc carries neither course nor scope.
        if scope_ancestors:
            metadata[SCOPE_META_KEY] = scope_ancestors
        # Connector provenance (#133): the source system + item, non-filterable.
        if source_system:
            metadata["source_system"] = source_system
        if source_item:
            metadata["source_item"] = source_item
        records.append(ChunkRecord(key=vector_key(s3_key, i), text=chunk, metadata=metadata))
    return records
