// Corpus screen (#191) — upload documents + browse what's in scope. Framework-free DOM.
// All authority is server-side (the endpoint derives tenant/scope from the verified token);
// this view just drives upload/list and renders the result.

import type { CorpusClient, CorpusDoc } from "./client";

function el(tag: string, cls = "", text?: string): HTMLElement {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

function humanSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function renderDocList(docs: CorpusDoc[], host: HTMLElement, prefix: string): void {
  host.replaceChildren();
  if (!docs.length) {
    host.appendChild(
      el("p", "empty-hint", "No documents in your scope yet. Upload one to get started."),
    );
    return;
  }
  const scopeLine = el("div", "sources-title", `In scope: ${prefix || "your tenant"}`);
  const list = el("ul", "corpus-list");
  for (const d of docs) {
    const li = el("li", "corpus-item");
    li.append(
      el("span", "corpus-name", d.name),
      el("span", "corpus-meta", `${humanSize(d.size)}${d.modified ? " · " + d.modified.slice(0, 10) : ""}`),
    );
    list.appendChild(li);
  }
  host.append(scopeLine, list);
}

/** Render the Corpus screen into `outEl`, wired to `client`. */
export function renderCorpus(client: CorpusClient, outEl: HTMLElement): void {
  outEl.replaceChildren();

  const panel = el("section", "panel");
  panel.setAttribute("aria-label", "Your documents");
  panel.appendChild(el("div", "panel-title", "Your documents"));

  // Upload control
  const uploader = el("div", "corpus-upload");
  const fileInput = document.createElement("input");
  fileInput.type = "file";
  fileInput.className = "corpus-file";
  fileInput.setAttribute("aria-label", "Choose a document to upload");
  const uploadBtn = el("button", "btn", "Upload") as HTMLButtonElement;
  uploadBtn.type = "button";
  const status = el("div", "corpus-status");
  uploader.append(fileInput, uploadBtn, status);

  const listHost = el("div", "corpus-docs");

  const refresh = async () => {
    listHost.setAttribute("aria-busy", "true");
    const r = await client.list();
    listHost.removeAttribute("aria-busy");
    if (!r.ok) {
      listHost.replaceChildren(el("p", "error-msg", `Could not list documents: ${r.reason}`));
      return;
    }
    renderDocList(r.documents, listHost, r.prefix);
  };

  uploadBtn.onclick = async () => {
    const file = fileInput.files?.[0];
    if (!file) {
      status.textContent = "Choose a file first.";
      return;
    }
    uploadBtn.disabled = true;
    status.textContent = `Uploading ${file.name}…`;
    try {
      const buf = await file.arrayBuffer();
      const res = await client.upload(file.name, buf, file.type || "application/octet-stream");
      if (res.ok) {
        status.textContent = `Uploaded ${res.key} (${humanSize(res.bytes)}). Indexing…`;
        fileInput.value = "";
        await refresh();
      } else {
        status.textContent = `Upload refused: ${res.reason}`;
      }
    } catch (err) {
      status.textContent = `Upload failed: ${(err as Error).message}`;
    } finally {
      uploadBtn.disabled = false;
    }
  };

  panel.append(uploader, listHost);
  outEl.appendChild(panel);
  void refresh();
}
