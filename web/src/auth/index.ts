// Auth — IdP redirect, Cognito identity exchange, and scoped-credential refresh
// (design §11 web/src/auth). The browser holds NO long-lived secret; it exchanges
// the campus-IdP session for short-lived scoped STS credentials via the broker.
//
// SKELETON (Phase 0). Phase 2 wires the redirect + identity exchange; the broker
// (infra/functions/broker) already mints the scoped credentials server-side.

export interface ScopedCredentials {
  accessKeyId: string;
  secretAccessKey: string;
  sessionToken: string;
  expiration: string; // ISO-8601
}

// The non-authoritative scope echo the broker returns alongside creds — used for
// UI display only (which models/tenant the session may touch). NOT authority.
export interface SessionScope {
  affiliation: string;
  tenant: string;
  courses: string[];
  tier: "oss" | "mid" | "frontier";
}

export interface BrokerResponse {
  credentials: ScopedCredentials;
  scope: SessionScope;
}

export async function fetchScopedCredentials(
  brokerUrl: string,
  idpToken: string,
): Promise<BrokerResponse> {
  const resp = await fetch(brokerUrl, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ idp_token: idpToken }),
  });
  if (!resp.ok) {
    throw new Error(`broker refused credentials: ${resp.status}`);
  }
  return (await resp.json()) as BrokerResponse;
}
