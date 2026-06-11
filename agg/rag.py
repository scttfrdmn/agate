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

# S3 layout: s3://{agg-docs bucket}/{tenant}/{path...}  (design §4).
# The first path segment IS the tenant; this is the isolation key.
_TENANT_SEG = re.compile(r"^([a-zA-Z0-9._-]+)/(.+)$")


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


def index_name_for_tenant(tenant: str) -> str:
    """The per-tenant S3 Vectors index name (design §4: one index per tenant)."""
    return f"agg-{tenant}"


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
    records: list[ChunkRecord] = []
    for i, chunk in enumerate(chunk_text(text, **chunk_kwargs)):
        records.append(
            ChunkRecord(
                key=vector_key(s3_key, i),
                text=chunk,
                metadata={
                    "source_key": s3_key,
                    "tenant": tenant,
                    "chunk": i,
                    # The chunk text itself, so QueryVectors can return it for
                    # prompt injection without a second S3 fetch.
                    "text": chunk,
                },
            )
        )
    return records
