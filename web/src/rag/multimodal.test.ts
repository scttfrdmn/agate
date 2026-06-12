import { describe, expect, it } from "vitest";

import { novaEmbedBody, parseNovaEmbedding, MM_EMBED_DIMENSION } from "./multimodal";

describe("novaEmbedBody", () => {
  it("builds a text query body", () => {
    const body = novaEmbedBody({ text: "survival curve" }) as {
      taskType: string;
      singleEmbeddingParams: { embeddingPurpose: string; text: { value: string } };
    };
    expect(body.taskType).toBe("SINGLE_EMBEDDING");
    expect(body.singleEmbeddingParams.embeddingPurpose).toBe("GENERIC_QUERY");
    expect(body.singleEmbeddingParams.text.value).toBe("survival curve");
  });

  it("builds an image query body", () => {
    const body = novaEmbedBody({ imageB64: "QUJD", imageFormat: "png" }) as {
      singleEmbeddingParams: { image: { format: string; source: { bytes: string } } };
    };
    expect(body.singleEmbeddingParams.image.format).toBe("png");
    expect(body.singleEmbeddingParams.image.source.bytes).toBe("QUJD");
  });

  it("requires exactly one of text or image", () => {
    expect(() => novaEmbedBody({})).toThrow();
    expect(() => novaEmbedBody({ text: "x", imageB64: "y", imageFormat: "png" })).toThrow();
  });

  it("requires a format with an image", () => {
    expect(() => novaEmbedBody({ imageB64: "QUJD" })).toThrow();
  });
});

describe("parseNovaEmbedding", () => {
  it("pulls the vector from the response", () => {
    expect(parseNovaEmbedding({ embeddings: [{ embedding: [0.1, 0.2] }] })).toEqual([0.1, 0.2]);
  });

  it("throws on an empty response", () => {
    expect(() => parseNovaEmbedding({})).toThrow();
    expect(() => parseNovaEmbedding({ embeddings: [{}] })).toThrow();
  });
});

describe("multimodal dimension", () => {
  it("is 3072 (distinct from the 1024 text index)", () => {
    expect(MM_EMBED_DIMENSION).toBe(3072);
  });
});
