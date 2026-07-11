// Main-thread client for the pyodide code-cell worker (#200, slice 2). Owns a single lazily
// spawned module worker and serializes runs through it (one WASM interpreter, so concurrent
// runs would interleave global state). The worker — and the ~10 MB runtime it imports — is
// created only on the first run(), keeping the base SPA bundle light (NO CLOCKS: no server
// kernel; everything is client-side WASM).

import type { CodeRequest, CodeResponse, CodeRunOutcome } from "./protocol";

export type KernelStatus = "idle" | "loading" | "ready";

export interface KernelOptions {
  // Injectable so tests can supply a fake worker instead of the real module worker.
  spawn?: () => Worker;
  // Notified on loading/ready transitions (for a "Downloading Python runtime…" hint).
  onStatus?: (status: KernelStatus, detail?: string) => void;
}

function defaultSpawn(): Worker {
  // Vite resolves this to a hashed, self-contained module-worker chunk at build time.
  return new Worker(new URL("./pyodide.worker.ts", import.meta.url), { type: "module" });
}

export class CodeKernel {
  private worker: Worker | null = null;
  private status: KernelStatus = "idle";
  private seq = 0;
  private pending = new Map<string, (o: CodeRunOutcome) => void>();

  constructor(private readonly opts: KernelOptions = {}) {}

  get state(): KernelStatus {
    return this.status;
  }

  private ensureWorker(): Worker {
    if (this.worker) return this.worker;
    const w = (this.opts.spawn ?? defaultSpawn)();
    w.onmessage = (ev: MessageEvent<CodeResponse>) => this.onMessage(ev.data);
    this.worker = w;
    this.setStatus("loading");
    return w;
  }

  private setStatus(status: KernelStatus, detail?: string): void {
    this.status = status;
    this.opts.onStatus?.(status, detail);
  }

  private onMessage(msg: CodeResponse): void {
    if (msg.type === "loading") this.setStatus("loading", msg.detail);
    else if (msg.type === "ready") this.setStatus("ready");
    else if (msg.type === "result") {
      const resolve = this.pending.get(msg.id);
      if (resolve) {
        this.pending.delete(msg.id);
        resolve({ stdout: msg.stdout, stderr: msg.stderr, result: msg.result, error: msg.error });
      }
    }
  }

  /** Warm the runtime without running code (e.g. when a code cell first appears). */
  preload(): void {
    const w = this.ensureWorker();
    const req: CodeRequest = { type: "init" };
    w.postMessage(req);
  }

  /** Run one cell's code; resolves with its captured output. Runs are serialized by the
   *  single-threaded worker, so callers can await each in turn. */
  run(code: string): Promise<CodeRunOutcome> {
    const w = this.ensureWorker();
    const id = `run-${++this.seq}`;
    return new Promise<CodeRunOutcome>((resolve) => {
      this.pending.set(id, resolve);
      const req: CodeRequest = { type: "run", id, code };
      w.postMessage(req);
    });
  }

  /** Tear down the worker (frees the WASM heap). Pending runs are abandoned. */
  dispose(): void {
    this.worker?.terminate();
    this.worker = null;
    this.pending.clear();
    this.setStatus("idle");
  }
}
