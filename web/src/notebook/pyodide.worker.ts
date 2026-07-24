// Pyodide code-cell worker (#200, slice 2) — runs a notebook code cell's Python entirely
// client-side in WASM. A MODULE worker (pyodide throws in classic workers); the runtime is
// lazily imported from our SELF-HOSTED /pyodide/ path on first run, so the ~10 MB download
// never touches the base SPA bundle and never hits a third-party origin (design #200).
//
// Isolation: the worker has no DOM and grants no arbitrary network — a cell computes over the
// Python stdlib plus a fixed, SELF-HOSTED set of packages (numpy/matplotlib/pandas + deps),
// whose wheels are fetched from OUR origin (packageBaseUrl = /pyodide/), never a third-party
// CDN. stdout/stderr are captured; the last expression's repr() is the "value" (REPL feel);
// matplotlib figures are captured as inline PNGs. Errors come back as the Python traceback.

import type { CodeRequest, CodeResponse } from "./protocol";

// Loaded lazily; `any` because pyodide's types aren't imported into the worker bundle.
let pyodide: unknown = null;
let loading: Promise<unknown> | null = null;

function post(msg: CodeResponse): void {
  (self as unknown as Worker).postMessage(msg);
}

async function getPyodide(): Promise<any> {
  if (pyodide) return pyodide;
  if (!loading) {
    post({ type: "loading", detail: "Downloading Python runtime…" });
    loading = (async () => {
      // Same-origin, self-hosted (copied into dist/pyodide/ at build time). The path only
      // exists at runtime in the published bundle, so tsc can't resolve it — that's expected.
      // @ts-expect-error runtime-only self-hosted module path
      const mod = await import(/* @vite-ignore */ "/pyodide/pyodide.mjs");
      // packageBaseUrl pins package wheels to OUR origin (self-hosted, #200) — loadPackage
      // never reaches a third-party CDN.
      const py = await mod.loadPyodide({ indexURL: "/pyodide/", packageBaseUrl: "/pyodide/" });
      pyodide = py;
      post({ type: "ready" });
      return py;
    })();
  }
  return loading;
}

// Run one cell. Redirect stdout/stderr into buffers, run the code, and — REPL-style — if the
// last statement is a bare expression, capture its repr as the result value. We do this in
// Python via the `code` module so multi-line inputs and syntax errors are handled correctly.
async function run(id: string, code: string): Promise<void> {
  let py: any;
  try {
    py = await getPyodide();
  } catch (e) {
    post({ type: "result", id, stdout: "", stderr: "", error: `Runtime failed to load: ${String(e)}` });
    return;
  }

  // Load any importable self-hosted packages the source needs BEFORE running (numpy, pandas,
  // matplotlib + deps). Fetches wheels from our origin; a package we don't ship just yields a
  // normal ModuleNotFoundError at import time. Best-effort — a load failure shouldn't abort
  // the run (the import error will surface in the traceback).
  try {
    post({ type: "loading", detail: "Loading packages…" });
    await py.loadPackagesFromImports(code);
  } catch {
    /* fall through — import will raise in the harness if truly missing */
  }

  // Set the source, then execute a fixed harness that captures streams + last-expr repr, and
  // any matplotlib figures as base64 PNGs. We force the Agg backend (no DOM in the worker) and
  // pull open figures after the cell runs.
  py.globals.set("__agate_src", code);
  const harness = `
import sys, io, ast
__agate_out = io.StringIO()
__agate_err = io.StringIO()
__agate_result = None
__agate_error = None
__agate_images = []
__agate_old = (sys.stdout, sys.stderr)
sys.stdout, sys.stderr = __agate_out, __agate_err
# Force a non-interactive backend if matplotlib is present (the worker has no DOM/canvas).
if "matplotlib" in sys.modules or "matplotlib" in __agate_src:
    try:
        import matplotlib
        matplotlib.use("Agg")
    except Exception:
        pass
try:
    __agate_mod = ast.parse(__agate_src, mode="exec")
    __agate_last = None
    if __agate_mod.body and isinstance(__agate_mod.body[-1], ast.Expr):
        __agate_last = ast.Expression(__agate_mod.body.pop().value)
    exec(compile(__agate_mod, "<cell>", "exec"), globals())
    if __agate_last is not None:
        __agate_val = eval(compile(__agate_last, "<cell>", "eval"), globals())
        if __agate_val is not None:
            __agate_result = repr(__agate_val)
    # Capture any open matplotlib figures as PNG data URIs, then close them.
    if "matplotlib.pyplot" in sys.modules:
        import base64 as __b64
        __plt = sys.modules["matplotlib.pyplot"]
        for __num in __plt.get_fignums():
            __fig = __plt.figure(__num)
            __buf = io.BytesIO()
            __fig.savefig(__buf, format="png", bbox_inches="tight")
            __agate_images.append("data:image/png;base64," + __b64.b64encode(__buf.getvalue()).decode())
        __plt.close("all")
except Exception:
    import traceback
    __agate_error = traceback.format_exc()
finally:
    sys.stdout, sys.stderr = __agate_old
`;
  try {
    await py.runPythonAsync(harness);
    const images = py.globals.get("__agate_images");
    post({
      type: "result",
      id,
      stdout: py.globals.get("__agate_out").getvalue(),
      stderr: py.globals.get("__agate_err").getvalue(),
      result: py.globals.get("__agate_result") ?? undefined,
      images: images ? images.toJs() : undefined,
      error: py.globals.get("__agate_error") ?? undefined,
    });
  } catch (e) {
    // A harness-level failure (not the user's code) — surface it rather than hang the cell.
    post({ type: "result", id, stdout: "", stderr: "", error: String(e) });
  }
}

self.onmessage = (ev: MessageEvent<CodeRequest>) => {
  const msg = ev.data;
  if (msg.type === "init") void getPyodide();
  else if (msg.type === "run") void run(msg.id, msg.code);
};
