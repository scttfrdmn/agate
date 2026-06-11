// SPA entry point. Phase 0 skeleton — renders a placeholder and proves the
// transport interface + auth types compile. Phase 2 wires the Tier 0 chat path.

import { BedrockTransport } from "./transport/bedrock";
import type { Transport, TransportConfig } from "./transport";

function makeTransport(cfg: TransportConfig): Transport {
  switch (cfg.tier) {
    case "bedrock":
      return new BedrockTransport(cfg.region, async () => {
        throw new Error("credential provider wired in Phase 2");
      });
    default:
      throw new Error(`transport tier ${cfg.tier} not wired yet`);
  }
}

const app = document.getElementById("app");
if (app) {
  const transport = makeTransport({ tier: "bedrock", region: "us-east-1" });
  app.textContent = `agg — transport tier: ${transport.tier} (Phase 2 wires chat)`;
}
