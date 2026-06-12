"""Pure multimodal-KB helpers (§10.2.7) — embeddings, path selection, citations.

Side-effect-free and AWS-free. Covers the parts the multimodal KB needs that are
pure logic: building the Nova multimodal-embedding request, parsing its response,
choosing the ingestion path (native embeddings vs parser+text), and resolving a
retrieved visual element to a `citation` event with a figure/table deep link.

Verified against the live service (§10.2.7 Phase-0 gate, issue #17):
- `amazon.nova-2-multimodal-embeddings-v1:0` embeds TEXT/IMAGE/AUDIO/VIDEO,
  `taskType=SINGLE_EMBEDDING` -> {"embeddings": [{"embeddingType", "embedding"}]},
  dimension 3072 (distinct from the 1024-dim text index — separate index).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

# Nova multimodal embedding model + its dimension (gate-verified). The multimodal
# index is created at this dimension; it is NOT the 1024-dim text index.
NOVA_MULTIMODAL_MODEL_ID = "amazon.nova-2-multimodal-embeddings-v1:0"
NOVA_MULTIMODAL_DIMENSION = 3072

# Embedding purpose distinguishes index-time vs query-time embeddings (Nova accepts
# GENERIC_INDEX for stored vectors and GENERIC_QUERY/GENERIC_RETRIEVAL for queries).
EmbeddingPurpose = Literal["GENERIC_INDEX", "GENERIC_QUERY"]

# Modalities the retrieval layer can attribute a citation to (§10.2.9 citation event).
Modality = Literal["text", "image", "table", "audio", "video"]

# Ingestion paths (§10.2.7).
IngestionPath = Literal["native", "parser"]


def nova_embed_request(
    *,
    text: str | None = None,
    image_b64: str | None = None,
    image_format: str | None = None,
    purpose: EmbeddingPurpose = "GENERIC_INDEX",
) -> dict[str, Any]:
    """Build the Nova multimodal embedding invoke body for one input.

    Exactly one of `text` or `image_b64` must be given. The shape mirrors the
    gate-verified contract: a SINGLE_EMBEDDING task with a typed params block.
    """
    if (text is None) == (image_b64 is None):
        raise ValueError("provide exactly one of text or image_b64")

    params: dict[str, Any] = {"embeddingPurpose": purpose}
    if text is not None:
        params["text"] = {"truncationMode": "END", "value": text}
    else:
        if not image_format:
            raise ValueError("image_format is required with image_b64")
        params["image"] = {"format": image_format, "source": {"bytes": image_b64}}

    return {"taskType": "SINGLE_EMBEDDING", "singleEmbeddingParams": params}


def parse_nova_embedding(response: dict[str, Any]) -> list[float]:
    """Extract the embedding vector from a Nova SINGLE_EMBEDDING response.

    Response shape: {"embeddings": [{"embeddingType": ..., "embedding": [...]}]}.
    Raises if no embedding is present (fail loud — a missing vector is a bug, not a
    silently-empty index entry).
    """
    embeddings = response.get("embeddings")
    if not embeddings:
        raise ValueError("Nova response carried no embeddings")
    first = embeddings[0]
    vector = first.get("embedding") if isinstance(first, dict) else first
    if not isinstance(vector, list) or not vector:
        raise ValueError("Nova embedding entry had no vector")
    return vector


# --- ingestion path selection (§10.2.7) -------------------------------------

# File extensions whose visual/temporal content the native multimodal path
# preserves best. Everything else (and the explicit fallback) goes through the
# parser+text path.
_NATIVE_EXTENSIONS = {
    "png",
    "jpg",
    "jpeg",
    "gif",
    "webp",
    "bmp",
    "tiff",  # images
    "mp4",
    "mov",
    "webm",
    "mkv",  # video
    "mp3",
    "wav",
    "flac",
    "m4a",
    "ogg",  # audio
}


def select_ingestion_path(
    filename: str,
    *,
    native_available: bool = True,
) -> IngestionPath:
    """Choose native multimodal embeddings vs parser+text for a source file.

    Default to native for visual/temporal media where it is available (preserves
    visual similarity / query-by-image); fall back to parser+text otherwise or when
    the native path is unavailable for the Region (§10.2.7). Text-like documents go
    through the parser path (their text is what matters).
    """
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if native_available and ext in _NATIVE_EXTENSIONS:
        return "native"
    return "parser"


# --- visual citation resolution (§10.2.7, §10.2.9) --------------------------


@dataclass(frozen=True, slots=True)
class VisualElement:
    """A retrieved element that a citation can resolve to."""

    source_id: str
    modality: Modality
    # Locator within the source — e.g. "figure-3", "table-2", page anchor.
    ref: str
    thumb: str | None = None  # base64 thumbnail for image/table previews


def citation_event(element: VisualElement) -> dict[str, Any]:
    """Build the `citation` event payload (§10.2.9) for a retrieved element."""
    ev: dict[str, Any] = {
        "type": "citation",
        "source": element.source_id,
        "modality": element.modality,
        "ref": element.ref,
    }
    if element.thumb:
        ev["thumb"] = element.thumb
    return ev


def corpus_deeplink(element: VisualElement) -> str:
    """Resolve a citation to an in-corpus deep link (§10.2.3).

    Text citations link to the source; visual citations (image/table/etc.) deep-link
    to the specific element via a fragment the SPA's /corpus/<id> view honours.
    """
    base = f"/corpus/{element.source_id}"
    if element.modality == "text":
        return base
    return f"{base}#{element.ref}"
