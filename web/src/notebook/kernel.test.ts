import { describe, expect, it } from "vitest";

import { CodeKernel } from "./kernel";
import type { CodeRequest, CodeResponse } from "./protocol";

// A fake worker: records posted requests and lets the test push responses back. Mirrors the
// real module worker's message contract without loading pyodide.
class FakeWorker {
  onmessage: ((ev: MessageEvent<CodeResponse>) => void) | null = null;
  posted: CodeRequest[] = [];
  terminated = false;
  postMessage(req: CodeRequest): void {
    this.posted.push(req);
  }
  terminate(): void {
    this.terminated = true;
  }
  // Test helper — simulate the worker emitting a message.
  emit(msg: CodeResponse): void {
    this.onmessage?.({ data: msg } as MessageEvent<CodeResponse>);
  }
}

function make() {
  const worker = new FakeWorker();
  const statuses: Array<[string, string | undefined]> = [];
  const kernel = new CodeKernel({
    spawn: () => worker as unknown as Worker,
    onStatus: (s, d) => statuses.push([s, d]),
  });
  return { worker, statuses, kernel };
}

describe("CodeKernel", () => {
  it("spawns lazily: no worker until the first run", () => {
    const { worker, kernel } = make();
    expect(kernel.state).toBe("idle");
    expect(worker.posted).toHaveLength(0);
  });

  it("posts a run request and resolves with the matching result", async () => {
    const { worker, kernel } = make();
    const p = kernel.run("print('hi')");
    // The kernel sent exactly one run with a generated id.
    expect(worker.posted).toHaveLength(1);
    const req = worker.posted[0];
    expect(req.type).toBe("run");
    const id = (req as Extract<CodeRequest, { type: "run" }>).id;
    worker.emit({ type: "result", id, stdout: "hi\n", stderr: "", result: undefined });
    const out = await p;
    expect(out.stdout).toBe("hi\n");
    expect(out.error).toBeUndefined();
  });

  it("surfaces loading → ready status transitions", async () => {
    const { worker, statuses, kernel } = make();
    const p = kernel.run("1+1");
    // ensureWorker() sets loading synchronously.
    expect(statuses[0][0]).toBe("loading");
    worker.emit({ type: "loading", detail: "Downloading Python runtime…" });
    worker.emit({ type: "ready" });
    expect(kernel.state).toBe("ready");
    const id = (worker.posted[0] as Extract<CodeRequest, { type: "run" }>).id;
    worker.emit({ type: "result", id, stdout: "", stderr: "", result: "2" });
    expect((await p).result).toBe("2");
  });

  it("routes concurrent runs to their own promises by id", async () => {
    const { worker, kernel } = make();
    const p1 = kernel.run("a");
    const p2 = kernel.run("b");
    const [r1, r2] = worker.posted as Array<Extract<CodeRequest, { type: "run" }>>;
    // Resolve out of order — each promise gets its own result.
    worker.emit({ type: "result", id: r2.id, stdout: "B", stderr: "" });
    worker.emit({ type: "result", id: r1.id, stdout: "A", stderr: "" });
    expect((await p1).stdout).toBe("A");
    expect((await p2).stdout).toBe("B");
  });

  it("dispose terminates the worker and resets state", async () => {
    const { worker, kernel } = make();
    void kernel.run("x");
    kernel.dispose();
    expect(worker.terminated).toBe(true);
    expect(kernel.state).toBe("idle");
  });

  it("preload warms the worker with an init message", () => {
    const { worker, kernel } = make();
    kernel.preload();
    expect(worker.posted[0]).toEqual({ type: "init" } satisfies CodeRequest);
  });
});
