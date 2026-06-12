import { describe, expect, it } from "vitest";

import { citationEvent, corpusDeeplink, elementFromMetadata } from "./citation";

describe("citationEvent", () => {
  it("includes a thumb for a visual element", () => {
    const ev = citationEvent({ sourceId: "PMC4521", modality: "image", ref: "figure-3", thumb: "QUJD" });
    expect(ev).toEqual({
      type: "citation",
      source: "PMC4521",
      modality: "image",
      ref: "figure-3",
      thumb: "QUJD",
    });
  });

  it("omits thumb for a text citation", () => {
    const ev = citationEvent({ sourceId: "DOC1", modality: "text", ref: "p2" });
    expect(ev.thumb).toBeUndefined();
  });
});

describe("corpusDeeplink", () => {
  it("links text citations to the source doc", () => {
    expect(corpusDeeplink({ sourceId: "DOC1", modality: "text", ref: "p2" })).toBe("/corpus/DOC1");
  });

  it("deep-links visual citations to the figure/table fragment", () => {
    expect(corpusDeeplink({ sourceId: "PMC4521", modality: "image", ref: "figure-3" })).toBe(
      "/corpus/PMC4521#figure-3",
    );
    expect(corpusDeeplink({ sourceId: "PMC4521", modality: "table", ref: "table-2" })).toBe(
      "/corpus/PMC4521#table-2",
    );
  });
});

describe("elementFromMetadata", () => {
  it("reads modality/ref/thumb from vector metadata", () => {
    const el = elementFromMetadata("PMC4521", { modality: "image", ref: "figure-3", thumb: "QUJD" });
    expect(el).toEqual({ sourceId: "PMC4521", modality: "image", ref: "figure-3", thumb: "QUJD" });
  });

  it("defaults to text modality when metadata is sparse", () => {
    const el = elementFromMetadata("DOC1", {});
    expect(el.modality).toBe("text");
    expect(el.ref).toBe("");
    expect(el.thumb).toBeUndefined();
  });
});
