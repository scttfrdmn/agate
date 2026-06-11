import { describe, expect, it } from "vitest";

import { CredentialManager, DEFAULT_SKEW_MS, shouldRefresh } from "./credentials";
import type { BrokerResponse } from "./index";

describe("shouldRefresh", () => {
  const now = 1_000_000_000_000; // fixed epoch ms
  const iso = (ms: number) => new Date(ms).toISOString();

  it("refreshes when no credential is held", () => {
    expect(shouldRefresh(null, now)).toBe(true);
  });

  it("does not refresh a credential comfortably in the future", () => {
    expect(shouldRefresh(iso(now + 10 * 60_000), now)).toBe(false);
  });

  it("refreshes once inside the skew window", () => {
    expect(shouldRefresh(iso(now + DEFAULT_SKEW_MS - 1), now)).toBe(true);
  });

  it("refreshes an already-expired credential", () => {
    expect(shouldRefresh(iso(now - 1), now)).toBe(true);
  });

  it("refreshes on an unparseable expiration (fail safe)", () => {
    expect(shouldRefresh("not-a-date", now)).toBe(true);
  });
});

describe("CredentialManager", () => {
  const resp = (expIso: string): BrokerResponse => ({
    credentials: {
      accessKeyId: "AK",
      secretAccessKey: "SK",
      sessionToken: "TOK",
      expiration: expIso,
    },
    scope: { affiliation: "student", tenant: "chem", courses: [], tier: "oss" },
  });

  function fakeFetch(seq: BrokerResponse[]) {
    let i = 0;
    return {
      calls: () => i,
      // monkeypatch target: replaces fetchScopedCredentials via the broker URL
      next: () => seq[Math.min(i++, seq.length - 1)],
    };
  }

  it("coalesces concurrent refreshes into one broker call", async () => {
    const t0 = 1_000_000_000_000;
    const fake = fakeFetch([resp(new Date(t0 + 30 * 60_000).toISOString())]);

    // Build a manager whose idpToken provider records calls; we stub the network
    // by overriding refresh via a subclass-free seam: a token provider that the
    // fake fetch keys off. Here we simulate by replacing global fetch.
    const realFetch = globalThis.fetch;
    let netCalls = 0;
    globalThis.fetch = (async () => {
      netCalls++;
      return {
        ok: true,
        json: async () => fake.next(),
      } as unknown as Response;
    }) as typeof fetch;

    try {
      const mgr = new CredentialManager(
        "https://broker.example/vend",
        async () => "idp-token",
        () => t0,
      );
      const [a, b, c] = await Promise.all([mgr.get(), mgr.get(), mgr.get()]);
      expect(a.accessKeyId).toBe("AK");
      expect(b).toEqual(a);
      expect(c).toEqual(a);
      expect(netCalls).toBe(1); // one network call despite three concurrent gets
      expect(mgr.scope?.tier).toBe("oss");
    } finally {
      globalThis.fetch = realFetch;
    }
  });

  it("re-fetches once the held credential enters the skew window", async () => {
    const realFetch = globalThis.fetch;
    let netCalls = 0;
    let clock = 1_000_000_000_000;
    const exp1 = new Date(clock + 5 * 60_000).toISOString();
    const exp2 = new Date(clock + 60 * 60_000).toISOString();
    const seq = [resp(exp1), resp(exp2)];
    globalThis.fetch = (async () => {
      const body = seq[Math.min(netCalls++, seq.length - 1)];
      return { ok: true, json: async () => body } as unknown as Response;
    }) as typeof fetch;

    try {
      const mgr = new CredentialManager(
        "https://broker.example/vend",
        async () => "idp-token",
        () => clock,
      );
      await mgr.get();
      expect(netCalls).toBe(1);
      // still valid -> no new call
      await mgr.get();
      expect(netCalls).toBe(1);
      // advance past (exp1 - skew) -> refresh
      clock += 5 * 60_000;
      await mgr.get();
      expect(netCalls).toBe(2);
    } finally {
      globalThis.fetch = realFetch;
    }
  });
});
