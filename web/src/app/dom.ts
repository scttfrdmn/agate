// Small, dependency-free DOM helpers shared across the app shell (#221 — first step of
// decomposing the main.ts orchestration monolith into an app/ composition layer + feature
// controllers). These reference only the document, not main()'s closure state, so they live
// here cleanly; feature-specific coordinators will follow.

/** Render the session's verified access as chips (tier / tenant / role / course) into
 *  #scope-chips. Pure DOM; textContent only (never innerHTML). */
export function renderScopeChips(scope: {
  tier?: string;
  tenant?: string;
  affiliation?: string;
  courses?: string[];
}): void {
  const host = document.getElementById("scope-chips");
  if (!host) return;
  const chips: Array<[string, string]> = [];
  if (scope.tier) chips.push(["tier", scope.tier]);
  if (scope.tenant) chips.push(["tenant", scope.tenant]);
  if (scope.affiliation) chips.push(["role", scope.affiliation]);
  for (const c of scope.courses ?? []) chips.push(["course", c]);
  host.replaceChildren(
    ...chips.map(([k, v]) => {
      const chip = document.createElement("span");
      chip.className = "scope-chip";
      const key = document.createElement("span");
      key.className = "scope-chip-key";
      key.textContent = k;
      const val = document.createElement("span");
      val.className = "scope-chip-val";
      val.textContent = v;
      chip.append(key, val);
      return chip;
    }),
  );
}

/** Show the recalled "what I remember about you" block in the empty-chat state (#194
 *  follow-up), so a returning user sees continuity before asking. Replaces any prior seed. */
export function renderMemorySeed(text: string): void {
  const empty = document.getElementById("empty");
  if (!empty || empty.hidden) return;
  let seed = document.getElementById("memory-seed");
  if (!seed) {
    seed = document.createElement("div");
    seed.id = "memory-seed";
    seed.className = "memory-seed";
    empty.appendChild(seed);
  }
  const title = document.createElement("div");
  title.className = "memory-seed-title";
  title.textContent = "From your earlier sessions";
  const body = document.createElement("div");
  body.className = "memory-seed-body";
  body.textContent = text.replace(/^Relevant remembered context:\n/, "");
  seed.replaceChildren(title, body);
}

/** Announce an error assertively (role=alert) so a screen reader interrupts, rather than
 *  waiting for the polite answer queue. */
export function renderError(out: HTMLElement, message: string): void {
  const box = document.createElement("p");
  box.className = "error-msg";
  box.setAttribute("role", "alert");
  box.textContent = `Error: ${message}`;
  out.replaceChildren(box);
}
