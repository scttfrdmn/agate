// Pyodide code-cell worker (#200, slice 2) — runs a notebook code cell's Python entirely
// client-side in WASM. A MODULE worker (pyodide throws in classic workers); the runtime is
// lazily imported from our SELF-HOSTED /pyodide/ path on first run, so the ~10 MB download
// never touches the base SPA bundle and never hits a third-party origin (design #200).
//
// Isolation: the worker has no DOM and we grant no network — a cell can compute over the
// Python stdlib but cannot fetch, so there's no new server/SSRF surface. stdout/stderr are
// captured; if the final statement is an expression its repr() is returned as the "value"
// (REPL feel). Errors come back as the Python traceback string, never thrown across the wire.

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
      const py = await mod.loadPyodide({ indexURL: "/pyodide/" });
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

  // Set the source, then execute a fixed harness that captures streams + last-expr repr.
  py.globals.set("__agate_src", code);
  const harness = `
import sys, io, ast
__agate_out = io.StringIO()
__agate_err = io.StringIO()
__agate_result = None
__agate_error = None
__agate_old = (sys.stdout, sys.stderr)
sys.stdout, sys.stderr = __agate_out, __agate_err
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
except Exception:
    import traceback
    __agate_error = traceback.format_exc()
finally:
    sys.stdout, sys.stderr = __agate_old
`;
  try {
    await py.runPythonAsync(harness);
    post({
      type: "result",
      id,
      stdout: py.globals.get("__agate_out").getvalue(),
      stderr: py.globals.get("__agate_err").getvalue(),
      result: py.globals.get("__agate_result") ?? undefined,
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
