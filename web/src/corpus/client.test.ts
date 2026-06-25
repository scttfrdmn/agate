import { describe, expect, it } from "vitest";

import { bytesToBase64, responseToList, responseToUpload } from "./client";

describe("responseToList", () => {
  it("maps a 200 listing to documents", () => {
    const r = responseToList(200, {
      ok: true,
      prefix: "chem/chemistry/chem-101/",
      documents: [
        { name: "notes.txt", key: "chem/chemistry/chem-101/notes.txt", size: 12, modified: "2026-06-25T00:00:00Z" },
        { name: "paper.pdf", key: "chem/chemistry/chem-101/paper.pdf", size: 99, modified: null },
      ],
    });
    expect(r.ok).toBe(true);
    expect(r.prefix).toBe("chem/chemistry/chem-101/");
    expect(r.documents.map((d) => d.name)).toEqual(["notes.txt", "paper.pdf"]);
    expect(r.documents[0].size).toBe(12);
  });

  it("maps a 403 to a rejected result with the detail reason", () => {
    const r = responseToList(403, { error: "not_entitled", detail: "cannot scope session" });
    expect(r.ok).toBe(false);
    expect(r.reason).toBe("cannot scope session");
    expect(r.documents).toEqual([]);
  });

  it("tolerates a missing documents array", () => {
    const r = responseToList(200, { ok: true, prefix: "chem/" });
    expect(r.documents).toEqual([]);
  });
});

describe("responseToUpload", () => {
  it("maps a 200 to an ok result with the key + bytes", () => {
    const r = responseToUpload(200, { ok: true, key: "chem/wk3/x.txt", bytes: 42 });
    expect(r).toEqual({ ok: true, reason: "", key: "chem/wk3/x.txt", bytes: 42 });
  });

  it("maps a non-200 to a rejected result", () => {
    const r = responseToUpload(500, { error: "corpus_error" });
    expect(r.ok).toBe(false);
    expect(r.reason).toBe("corpus_error");
  });
});

describe("bytesToBase64", () => {
  it("round-trips ascii", () => {
    const bytes = new TextEncoder().encode("hello world");
    expect(bytesToBase64(bytes)).toBe(btoa("hello world"));
  });

  it("handles bytes across the chunk boundary without overflow", () => {
    const big = new Uint8Array(0x8000 + 10).fill(65); // 'A'
    const out = bytesToBase64(big);
    // decodes back to the same length
    expect(atob(out).length).toBe(big.length);
  });
});
