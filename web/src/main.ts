// SPA entry — Tier 0 browser-direct chat (design §12 Phase 2).
//
// Flow: campus IdP token -> broker -> scoped STS creds -> BedrockTransport
// ConverseStream, streamed into a minimal UI. No server in the path, no secret in
// the client. History is in-memory only (persistence is a later phase).

import { CredentialManager } from "./auth/credentials";
import { ChatSession, type ContextProvider } from "./chat/session";
import { config } from "./config";
import { withContext } from "./rag/context";
import { Retriever } from "./rag/retriever";
import { BedrockTransport } from "./transport/bedrock";

// Phase 2 placeholder IdP token provider. Phase 4 replaces this with the real
// campus-IdP redirect/session; the broker validates the token server-side either
// way. We read it from the URL hash (#idp_token=...) for local end-to-end testing.
function idpTokenFromHash(): Promise<string> {
  const hash = new URLSearchParams(location.hash.slice(1));
  const token = hash.get("idp_token") ?? "";
  return Promise.resolve(token);
}

function render(app: HTMLElement): void {
  app.innerHTML = `
    <main style="max-width:48rem;margin:2rem auto;font-family:system-ui">
      <h1>agg</h1>
      <p id="scope" style="color:#666"></p>
      <div id="log" style="white-space:pre-wrap;border:1px solid #ddd;padding:1rem;min-height:8rem"></div>
      <form id="f" style="display:flex;gap:.5rem;margin-top:1rem">
        <input id="q" style="flex:1;padding:.5rem" placeholder="Ask…" autocomplete="off" />
        <button>Send</button>
      </form>
    </main>`;
}

function main(): void {
  const app = document.getElementById("app");
  if (!app) return;
  render(app);

  if (!config.brokerUrl) {
    document.getElementById("scope")!.textContent =
      "Set VITE_BROKER_URL to the deployed broker to enable chat.";
    return;
  }

  const creds = new CredentialManager(config.brokerUrl, idpTokenFromHash);
  const transport = new BedrockTransport(config.region, () => creds.get());

  // RAG is enabled when a vector bucket is configured. The tenant index is
  // derived from the session scope the broker returns — the credential can only
  // read the index its agg:tenant tag matches, so retrieval scope == access scope.
  let contextProvider: ContextProvider | undefined;
  if (config.vectorBucketName) {
    contextProvider = async (query: string) => {
      await creds.get(); // ensure scope is populated
      const tenant = creds.scope?.tenant;
      if (!tenant) return [];
      const retriever = new Retriever(
        {
          region: config.region,
          vectorBucketName: config.vectorBucketName,
          indexName: `agg-${tenant}`,
        },
        () => creds.get(),
      );
      const chunks = await retriever.retrieve(query);
      return withContext([], chunks);
    };
  }

  const session = new ChatSession(
    transport,
    config.defaultModelId,
    undefined,
    undefined,
    contextProvider,
  );

  const log = document.getElementById("log")!;
  const form = document.getElementById("f") as HTMLFormElement;
  const input = document.getElementById("q") as HTMLInputElement;

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const q = input.value.trim();
    if (!q) return;
    input.value = "";
    log.textContent += `\n> ${q}\n`;

    try {
      let reasoningShown = false;
      await session.send(q, {
        onReasoning: () => {
          // Show a single lightweight "thinking…" marker for reasoning models.
          if (!reasoningShown) {
            log.textContent += "[thinking…] ";
            reasoningShown = true;
          }
        },
        onDelta: (delta) => {
          log.textContent += delta;
        },
      });
      const s = creds.scope;
      if (s) {
        document.getElementById("scope")!.textContent =
          `tier=${s.tier} · tenant=${s.tenant} · ${s.affiliation}`;
      }
    } catch (err) {
      log.textContent += `\n[error: ${(err as Error).message}]\n`;
    }
  });
}

main();
