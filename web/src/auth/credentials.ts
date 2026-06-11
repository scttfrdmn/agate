// CredentialManager — holds the current scoped STS session and refreshes it
// from the broker before it expires (design §2: short-lived creds, refreshed
// against the live IdP session; security memo §9: short TTL).
//
// The browser never holds a long-lived secret. Each refresh re-fetches the IdP
// token (re-validated server-side by the broker) and exchanges it for a fresh
// scoped credential. The soft cap (design §7.1) lives in the broker: once a user
// is over budget the broker declines to vend, so refresh simply fails closed.

import {
  fetchScopedCredentials,
  type BrokerResponse,
  type ScopedCredentials,
  type SessionScope,
} from "./index";

// Refresh this far before the hard expiry so an in-flight request never races
// the expiry boundary. STS minimum TTL is 15 min; 60s skew is comfortable.
export const DEFAULT_SKEW_MS = 60_000;

/**
 * Pure decision: should we refresh credentials now? Separated from all I/O so it
 * is unit-testable without a clock or network.
 *
 * @param expirationIso  ISO-8601 expiry from the broker (or null if none held)
 * @param nowMs          current time in epoch ms (injected, never read here)
 * @param skewMs         refresh this many ms before hard expiry
 */
export function shouldRefresh(
  expirationIso: string | null,
  nowMs: number,
  skewMs: number = DEFAULT_SKEW_MS,
): boolean {
  if (!expirationIso) return true;
  const expMs = Date.parse(expirationIso);
  if (Number.isNaN(expMs)) return true; // unparseable -> refresh to be safe
  return nowMs >= expMs - skewMs;
}

// A source of the campus-IdP token. In Phase 4 this is backed by the real IdP
// redirect/session; for Phase 2 the SPA supplies it (e.g. from the OIDC hash).
export type IdpTokenProvider = () => Promise<string>;

export class CredentialManager {
  private current: BrokerResponse | null = null;
  private inflight: Promise<BrokerResponse> | null = null;

  constructor(
    private readonly brokerUrl: string,
    private readonly idpToken: IdpTokenProvider,
    // Injected clock so tests are deterministic; defaults to Date.now.
    private readonly now: () => number = () => Date.now(),
    private readonly skewMs: number = DEFAULT_SKEW_MS,
  ) {}

  /** The non-authoritative scope echo for UI display (null until first fetch). */
  get scope(): SessionScope | null {
    return this.current?.scope ?? null;
  }

  /** Return valid scoped credentials, refreshing through the broker if needed. */
  async get(): Promise<ScopedCredentials> {
    const exp = this.current?.credentials.expiration ?? null;
    if (!shouldRefresh(exp, this.now(), this.skewMs)) {
      return this.current!.credentials;
    }
    // Coalesce concurrent refreshes so a burst of calls makes one broker request.
    if (!this.inflight) {
      this.inflight = this.refresh().finally(() => {
        this.inflight = null;
      });
    }
    const fresh = await this.inflight;
    return fresh.credentials;
  }

  private async refresh(): Promise<BrokerResponse> {
    const token = await this.idpToken();
    const resp = await fetchScopedCredentials(this.brokerUrl, token);
    this.current = resp;
    return resp;
  }
}
