// Cognito Hosted UI login (OIDC implicit flow) for the demo.
//
// A campus deployment federates the campus IdP and the SPA receives a token via
// the institution's existing SSO. For the self-contained demo there's no campus
// IdP, so we redirect to the demo pool's Cognito Hosted UI; it returns the
// id_token in the URL fragment (implicit flow — no client secret, nothing leaves
// the browser to a token endpoint). The broker verifies that token server-side
// (RS256/JWKS) exactly as it would a campus token.
//
// Token precedence (highest first):
//   1. a token captured from the redirect fragment, stashed in sessionStorage
//   2. a manual `#idp_token=<jwt>` in the hash (operator paste / scripted demo)
// The pure parsing is split out so it's unit-testable without a browser.

const STORAGE_KEY = "agate.idp_token";

export interface LoginConfig {
  // Hosted-UI base, e.g. https://agate-demo-123.auth.us-east-1.amazoncognito.com
  domain: string;
  clientId: string;
  // Where Cognito redirects back to (must be a registered callback URL).
  redirectUri: string;
}

/** Pull `id_token` (Cognito implicit flow) or `idp_token` (manual) from a URL
 *  fragment string (the part after `#`). Returns "" when absent. Pure. */
export function tokenFromFragment(fragment: string): string {
  const p = new URLSearchParams(fragment.replace(/^#/, ""));
  return p.get("id_token") ?? p.get("idp_token") ?? "";
}

/** Is this JWT expired (or unreadable)? Decodes the `exp` claim (seconds since
 *  epoch) from the payload — no signature check, that's the broker's job; this is
 *  only so the SPA stops treating a dead token as a live session. A token with no
 *  readable `exp` is treated as NOT expired (the server remains the authority).
 *  `nowMs` defaults to the current time; injectable for tests. Pure. */
export function isTokenExpired(token: string, nowMs: number = Date.now()): boolean {
  const parts = token.split(".");
  if (parts.length < 2) return false;
  try {
    const json = atob(parts[1].replace(/-/g, "+").replace(/_/g, "/"));
    const exp = (JSON.parse(json) as { exp?: number }).exp;
    if (typeof exp !== "number") return false;
    return nowMs >= exp * 1000;
  } catch {
    return false;
  }
}

/** A human-readable sign-in label from the id_token — the first present of the usual identity
 *  claims (preferred_username, cognito:username, email, then the local part of email, then sub).
 *  No signature check (display only; the server is the authority). Returns "" if unreadable. Pure. */
export function identityFromToken(token: string): string {
  const parts = token.split(".");
  if (parts.length < 2) return "";
  try {
    const json = atob(parts[1].replace(/-/g, "+").replace(/_/g, "/"));
    const c = JSON.parse(json) as Record<string, unknown>;
    const s = (k: string): string => (typeof c[k] === "string" ? (c[k] as string) : "");
    const email = s("email");
    return (
      s("preferred_username") ||
      s("cognito:username") ||
      email ||
      (email.includes("@") ? email.split("@")[0] : "") ||
      s("sub")
    );
  } catch {
    return "";
  }
}

/** Normalize the Hosted-UI base to an absolute https origin (no trailing slash).
 *  VITE_COGNITO_DOMAIN is often set bare (`x.auth.region.amazoncognito.com`); without
 *  a scheme `location.assign()` treats the login URL as RELATIVE and appends it to the
 *  current page instead of navigating to Cognito. Pure. */
function hostedUiBase(domain: string): string {
  const trimmed = domain.replace(/\/$/, "");
  return /^https?:\/\//.test(trimmed) ? trimmed : `https://${trimmed}`;
}

/** Build the Hosted-UI authorize URL for the implicit flow. Pure. */
export function authorizeUrl(cfg: LoginConfig): string {
  const q = new URLSearchParams({
    client_id: cfg.clientId,
    response_type: "token", // implicit flow → id_token in the fragment
    scope: "openid profile",
    redirect_uri: cfg.redirectUri,
  });
  return `${hostedUiBase(cfg.domain)}/login?${q.toString()}`;
}

/** Build the Hosted-UI logout URL. Pure. */
export function logoutUrl(cfg: LoginConfig): string {
  const q = new URLSearchParams({
    client_id: cfg.clientId,
    logout_uri: cfg.redirectUri,
  });
  return `${hostedUiBase(cfg.domain)}/logout?${q.toString()}`;
}

/** Capture a token from the current location's fragment into sessionStorage and
 *  scrub it from the URL bar (so the JWT isn't left in history / shared links).
 *  Returns the captured token, or "" if the fragment held none. */
export function captureTokenFromUrl(): string {
  const tok = tokenFromFragment(location.hash);
  if (tok) {
    sessionStorage.setItem(STORAGE_KEY, tok);
    // Drop the fragment without reloading.
    history.replaceState(null, "", location.pathname + location.search);
  }
  return tok;
}

/** The current IdP token: a captured/stored one, else whatever capture finds now.
 *  An EXPIRED stored token is dropped and treated as absent — otherwise the SPA
 *  would keep showing "logged in" and sending a dead token the broker now 403s. */
export function currentToken(): string {
  const tok = sessionStorage.getItem(STORAGE_KEY) || captureTokenFromUrl();
  if (tok && isTokenExpired(tok)) {
    sessionStorage.removeItem(STORAGE_KEY);
    return "";
  }
  return tok;
}

export function isLoggedIn(): boolean {
  return currentToken() !== "";
}

export function login(cfg: LoginConfig): void {
  location.assign(authorizeUrl(cfg));
}

export function logout(cfg: LoginConfig): void {
  sessionStorage.removeItem(STORAGE_KEY);
  location.assign(logoutUrl(cfg));
}
