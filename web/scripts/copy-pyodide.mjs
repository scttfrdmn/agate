// Copy the pyodide runtime we self-host (design decision #200: same-origin, no third-party
// script origin at runtime) from node_modules into public/pyodide/, which Vite publishes
// verbatim to dist/pyodide/. Lazy-loaded by the code-cell worker — NOT part of the base
// bundle, so the SPA stays light until a user actually runs a code cell.
//
// Runs as a prebuild step (see package.json "build"). Idempotent: copies only the files
// loadPyodide fetches from indexURL, skipping anything that's already identical in size.

import { copyFileSync, mkdirSync, existsSync, statSync, readFileSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const web = dirname(here);
const src = join(web, "node_modules", "pyodide");
const dst = join(web, "public", "pyodide");

// The exact set loadPyodide({indexURL}) needs: the loader module, the wasm, the stdlib
// archive, and the package lock (queried even when we load no extra packages).
const FILES = [
  "pyodide.mjs",
  "pyodide.asm.mjs",
  "pyodide.asm.wasm",
  "python_stdlib.zip",
  "pyodide-lock.json",
];

// Packages code cells may import (#200). Their full dependency closure is resolved from the
// lock and the wheels are DOWNLOADED AT BUILD TIME from the pinned pyodide release into
// dist/pyodide/, so at RUNTIME loadPackage fetches them from OUR origin — no third-party
// origin (holds the #200 posture). Lazy in the browser: a wheel is only fetched when a cell
// actually imports it.
const ROOT_PACKAGES = ["numpy", "matplotlib", "pandas"];

if (!existsSync(src)) {
  console.error(`[copy-pyodide] pyodide not installed at ${src} — run: npm install pyodide`);
  process.exit(1);
}

mkdirSync(dst, { recursive: true });
let copied = 0;
for (const f of FILES) {
  const from = join(src, f);
  const to = join(dst, f);
  if (existsSync(to) && statSync(to).size === statSync(from).size) continue;
  copyFileSync(from, to);
  copied++;
}
console.log(`[copy-pyodide] ${copied} runtime file(s) copied to public/pyodide/ (${FILES.length} total)`);

// --- package wheels (self-hosted) -------------------------------------------
const version = JSON.parse(readFileSync(join(src, "package.json"), "utf8")).version;
const lock = JSON.parse(readFileSync(join(src, "pyodide-lock.json"), "utf8"));
const pkgs = lock.packages;

// Resolve the transitive dependency closure of the requested root packages.
function closure(roots) {
  const seen = new Set();
  const stack = roots.map((r) => r.toLowerCase());
  while (stack.length) {
    const n = stack.pop();
    if (seen.has(n) || !pkgs[n]) continue;
    seen.add(n);
    for (const d of pkgs[n].depends ?? []) stack.push(d.toLowerCase());
  }
  return [...seen];
}

const wheels = closure(ROOT_PACKAGES)
  .map((k) => pkgs[k]?.file_name)
  .filter(Boolean);

async function fetchWheel(name) {
  const to = join(dst, name);
  if (existsSync(to)) return false; // already present (idempotent build)
  const url = `https://cdn.jsdelivr.net/pyodide/v${version}/full/${name}`;
  const resp = await fetch(url);
  if (!resp.ok) throw new Error(`fetch ${name} -> HTTP ${resp.status}`);
  writeFileSync(to, Buffer.from(await resp.arrayBuffer()));
  return true;
}

let fetched = 0;
for (const w of wheels) {
  if (await fetchWheel(w)) fetched++;
}
console.log(
  `[copy-pyodide] wheels: ${fetched} downloaded, ${wheels.length} total for [${ROOT_PACKAGES.join(", ")}] closure`,
);
