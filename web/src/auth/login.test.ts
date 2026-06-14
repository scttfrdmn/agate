import { describe, expect, it } from "vitest";
import { authorizeUrl, logoutUrl, tokenFromFragment, type LoginConfig } from "./login";

const cfg: LoginConfig = {
  domain: "https://agate-demo-123.auth.us-east-1.amazoncognito.com",
  clientId: "abc123",
  redirectUri: "https://d2a1.cloudfront.net/",
};

describe("tokenFromFragment", () => {
  it("reads the Cognito implicit-flow id_token", () => {
    expect(tokenFromFragment("#id_token=eyJABC&token_type=Bearer&expires_in=3600")).toBe("eyJABC");
  });

  it("reads a manual idp_token", () => {
    expect(tokenFromFragment("#idp_token=eyJXYZ")).toBe("eyJXYZ");
  });

  it("prefers id_token over idp_token when both present", () => {
    expect(tokenFromFragment("#id_token=fromcognito&idp_token=manual")).toBe("fromcognito");
  });

  it("tolerates a leading # or none", () => {
    expect(tokenFromFragment("id_token=t")).toBe("t");
  });

  it("returns empty string when no token", () => {
    expect(tokenFromFragment("#access_token=nope")).toBe("");
    expect(tokenFromFragment("")).toBe("");
  });
});

describe("authorizeUrl", () => {
  it("builds an implicit-flow login URL", () => {
    const u = new URL(authorizeUrl(cfg));
    expect(u.origin + u.pathname).toBe(
      "https://agate-demo-123.auth.us-east-1.amazoncognito.com/login",
    );
    expect(u.searchParams.get("client_id")).toBe("abc123");
    expect(u.searchParams.get("response_type")).toBe("token");
    expect(u.searchParams.get("scope")).toBe("openid profile");
    expect(u.searchParams.get("redirect_uri")).toBe("https://d2a1.cloudfront.net/");
  });

  it("does not double a trailing slash on the domain", () => {
    expect(authorizeUrl({ ...cfg, domain: cfg.domain + "/" })).toContain(
      "amazoncognito.com/login?",
    );
  });
});

describe("logoutUrl", () => {
  it("builds a logout URL with logout_uri", () => {
    const u = new URL(logoutUrl(cfg));
    expect(u.pathname).toBe("/logout");
    expect(u.searchParams.get("client_id")).toBe("abc123");
    expect(u.searchParams.get("logout_uri")).toBe("https://d2a1.cloudfront.net/");
  });
});
