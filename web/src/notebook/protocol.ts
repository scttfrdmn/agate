// Wire protocol between the main thread and the pyodide code-cell worker (#200, slice 2).
// Kept in its own module so both sides (and tests) share one definition. The worker runs
// Python entirely client-side in WASM — no server, no network — so this is a local IPC only.

// Main thread → worker.
export type CodeRequest =
  | { type: "init" } // warm the runtime (optional; run also inits lazily)
  | { type: "run"; id: string; code: string };

// Worker → main thread.
export type CodeResponse =
  | { type: "ready" } // runtime finished loading
  | { type: "loading"; detail: string } // progress while the ~10 MB runtime downloads
  | {
      type: "result";
      id: string;
      stdout: string;
      stderr: string;
      // repr() of the last expression when the final statement is an expression (REPL-style),
      // else undefined. Kept separate from stdout so the UI can present it as "the value".
      result?: string;
      // base64 PNG data URIs for any matplotlib figures the cell produced (#200 packages).
      images?: string[];
      // A Python traceback string when the code raised; the run is not "ok".
      error?: string;
    };

export interface CodeRunOutcome {
  stdout: string;
  stderr: string;
  result?: string;
  images?: string[];
  error?: string;
}
