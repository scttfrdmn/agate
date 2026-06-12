// Pure visual-citation resolution (§10.2.7, §10.2.9). Mirrors agg/multimodal.py
// (citation_event / corpus_deeplink) on the SPA side. No SDK — unit-testable.

import type { CitationEvent, CitationModality } from "../events/protocol";

export interface VisualElement {
  sourceId: string;
  modality: CitationModality;
  // Locator within the source — e.g. "figure-3", "table-2".
  ref: string;
  thumb?: string; // base64 thumbnail for image/table previews
}

// Build the `citation` event for a retrieved element (text or visual).
export function citationEvent(el: VisualElement): CitationEvent {
  const ev: CitationEvent = {
    type: "citation",
    source: el.sourceId,
    modality: el.modality,
    ref: el.ref,
  };
  if (el.thumb) ev.thumb = el.thumb;
  return ev;
}

// Resolve a citation to an in-corpus deep link. Text links to the source; a visual
// element deep-links to the specific figure/table via a fragment the /corpus/<id>
// view honours — so a citation can click through to the figure, not just the doc.
export function corpusDeeplink(el: VisualElement): string {
  const base = `/corpus/${el.sourceId}`;
  return el.modality === "text" ? base : `${base}#${el.ref}`;
}

// Map S3 Vectors result metadata to a VisualElement. The ingest layer writes
// `modality`/`ref`/`thumb` into vector metadata; we read them back defensively.
export function elementFromMetadata(
  sourceId: string,
  metadata: Record<string, unknown>,
): VisualElement {
  const modality = (metadata.modality as CitationModality) ?? "text";
  const ref = typeof metadata.ref === "string" ? metadata.ref : "";
  const thumb = typeof metadata.thumb === "string" ? metadata.thumb : undefined;
  return { sourceId, modality, ref, thumb };
}
