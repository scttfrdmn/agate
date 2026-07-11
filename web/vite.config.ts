import { defineConfig } from "vite";

// Static SPA build — output is uploaded to S3 + served via CloudFront (design §11).
// No dev server in production; this is purely a static asset pipeline.
//
// The pyodide runtime is SELF-HOSTED at /pyodide/ (copied into dist/ by
// scripts/copy-pyodide.mjs) and imported dynamically by the code-cell worker at runtime
// (#200). It must NOT be bundled — externalize the runtime-only path so Rollup leaves the
// import as-is and the browser fetches it from our own origin. The worker gets its OWN
// Rollup pass, so the external must be declared for both the main build and the worker.
const PYODIDE_EXTERNAL = ["/pyodide/pyodide.mjs"];

export default defineConfig({
  build: {
    outDir: "dist",
    sourcemap: true,
    rollupOptions: { external: PYODIDE_EXTERNAL },
  },
  worker: {
    format: "es", // pyodide requires a module worker (throws in a classic worker)
    rollupOptions: { external: PYODIDE_EXTERNAL },
  },
});
