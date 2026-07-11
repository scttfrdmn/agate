// Copy the pyodide runtime we self-host (design decision #200: same-origin, no third-party
// script origin at runtime) from node_modules into public/pyodide/, which Vite publishes
// verbatim to dist/pyodide/. Lazy-loaded by the code-cell worker — NOT part of the base
// bundle, so the SPA stays light until a user actually runs a code cell.
//
// Runs as a prebuild step (see package.json "build"). Idempotent: copies only the files
// loadPyodide fetches from indexURL, skipping anything that's already identical in size.

import { copyFileSync, mkdirSync, existsSync, statSync } from "node:fs";
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
console.log(`[copy-pyodide] ${copied} file(s) copied to public/pyodide/ (${FILES.length} total)`);
