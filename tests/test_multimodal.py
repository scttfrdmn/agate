"""Unit tests for the pure multimodal-KB helpers (§10.2.7). No AWS."""

from __future__ import annotations

import pytest
from agg.multimodal import (
    NOVA_MULTIMODAL_DIMENSION,
    VisualElement,
    citation_event,
    corpus_deeplink,
    nova_embed_request,
    parse_nova_embedding,
    select_ingestion_path,
)

# --- nova_embed_request -----------------------------------------------------


def test_text_embed_request_shape():
    body = nova_embed_request(text="hello", purpose="GENERIC_QUERY")
    assert body["taskType"] == "SINGLE_EMBEDDING"
    params = body["singleEmbeddingParams"]
    assert params["embeddingPurpose"] == "GENERIC_QUERY"
    assert params["text"]["value"] == "hello"


def test_image_embed_request_shape():
    body = nova_embed_request(image_b64="QUJD", image_format="png")
    params = body["singleEmbeddingParams"]
    assert params["image"]["format"] == "png"
    assert params["image"]["source"]["bytes"] == "QUJD"


def test_embed_request_requires_exactly_one_input():
    with pytest.raises(ValueError):
        nova_embed_request()
    with pytest.raises(ValueError):
        nova_embed_request(text="x", image_b64="y", image_format="png")


def test_image_embed_requires_format():
    with pytest.raises(ValueError):
        nova_embed_request(image_b64="QUJD")


# --- parse_nova_embedding ---------------------------------------------------


def test_parse_embedding_vector():
    resp = {"embeddings": [{"embeddingType": "TEXT", "embedding": [0.1, 0.2, 0.3]}]}
    assert parse_nova_embedding(resp) == [0.1, 0.2, 0.3]


def test_parse_embedding_empty_raises():
    with pytest.raises(ValueError):
        parse_nova_embedding({"embeddings": []})
    with pytest.raises(ValueError):
        parse_nova_embedding({})
    with pytest.raises(ValueError):
        parse_nova_embedding({"embeddings": [{"embeddingType": "TEXT"}]})


def test_multimodal_dimension_differs_from_text_index():
    # Guards the gate finding: multimodal is 3072, not the 1024 text index dim.
    assert NOVA_MULTIMODAL_DIMENSION == 3072


# --- select_ingestion_path --------------------------------------------------


@pytest.mark.parametrize(
    "filename,expected",
    [
        ("figure.png", "native"),
        ("scan.JPEG", "native"),
        ("lecture.mp4", "native"),
        ("interview.mp3", "native"),
        ("paper.pdf", "parser"),
        ("notes.txt", "parser"),
        ("data.csv", "parser"),
        ("noext", "parser"),
    ],
)
def test_select_ingestion_path(filename, expected):
    assert select_ingestion_path(filename) == expected


def test_native_path_falls_back_when_unavailable():
    # Region without native multimodal support -> parser even for an image.
    assert select_ingestion_path("figure.png", native_available=False) == "parser"


# --- citations + deep links -------------------------------------------------


def test_citation_event_for_figure_includes_thumb():
    el = VisualElement(source_id="PMC4521", modality="image", ref="figure-3", thumb="QUJD")
    ev = citation_event(el)
    assert ev == {
        "type": "citation",
        "source": "PMC4521",
        "modality": "image",
        "ref": "figure-3",
        "thumb": "QUJD",
    }


def test_citation_event_text_omits_thumb():
    ev = citation_event(VisualElement(source_id="DOC1", modality="text", ref="p2"))
    assert "thumb" not in ev
    assert ev["modality"] == "text"


def test_corpus_deeplink_text_vs_visual():
    text_el = VisualElement(source_id="DOC1", modality="text", ref="p2")
    fig_el = VisualElement(source_id="PMC4521", modality="image", ref="figure-3")
    table_el = VisualElement(source_id="PMC4521", modality="table", ref="table-2")
    assert corpus_deeplink(text_el) == "/corpus/DOC1"
    assert corpus_deeplink(fig_el) == "/corpus/PMC4521#figure-3"
    assert corpus_deeplink(table_el) == "/corpus/PMC4521#table-2"
