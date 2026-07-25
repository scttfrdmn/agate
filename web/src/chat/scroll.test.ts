// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from "vitest";

import { ScrollAnchor } from "./scroll";

afterEach(() => {
  document.body.innerHTML = "";
  vi.restoreAllMocks();
});

// happy-dom doesn't lay out, so scrollHeight/clientHeight are 0 and scrollTo is a stub. We drive
// scroll state by setting those props directly and dispatching a "scroll" event.
function makeHost(): HTMLElement {
  const parent = document.createElement("div");
  const host = document.createElement("div");
  parent.appendChild(host);
  document.body.appendChild(parent);
  return host;
}

function setGeometry(host: HTMLElement, top: number, scrollHeight: number, clientHeight: number): void {
  Object.defineProperty(host, "scrollTop", { value: top, writable: true, configurable: true });
  Object.defineProperty(host, "scrollHeight", { value: scrollHeight, writable: true, configurable: true });
  Object.defineProperty(host, "clientHeight", { value: clientHeight, writable: true, configurable: true });
}

describe("ScrollAnchor", () => {
  it("starts pinned; the pill mounts hidden", () => {
    const host = makeHost();
    const a = new ScrollAnchor(host);
    a.mountPill();
    expect(a.isPinned).toBe(true);
    const pill = host.parentElement!.querySelector<HTMLButtonElement>(".scroll-new-pill")!;
    expect(pill).not.toBeNull();
    expect(pill.hidden).toBe(true);
  });

  it("un-pins and shows the pill when the user scrolls up away from the bottom", () => {
    const host = makeHost();
    const a = new ScrollAnchor(host);
    a.mountPill();
    // Sit at the bottom of a tall column, then scroll UP far.
    setGeometry(host, 1000, 2000, 500); // fromBottom = 500 → pinned toggles false only on up-move
    host.dispatchEvent(new Event("scroll")); // establishes lastTop
    setGeometry(host, 200, 2000, 500); // moved up; fromBottom = 1300 > threshold
    host.dispatchEvent(new Event("scroll"));
    expect(a.isPinned).toBe(false);
    const pill = host.parentElement!.querySelector<HTMLButtonElement>(".scroll-new-pill")!;
    expect(pill.hidden).toBe(false);
  });

  it("re-pins when the user returns to the bottom", () => {
    const host = makeHost();
    const a = new ScrollAnchor(host);
    a.mountPill();
    setGeometry(host, 1000, 2000, 500);
    host.dispatchEvent(new Event("scroll"));
    setGeometry(host, 200, 2000, 500); // up → un-pin
    host.dispatchEvent(new Event("scroll"));
    expect(a.isPinned).toBe(false);
    setGeometry(host, 1490, 2000, 500); // fromBottom = 10 < slack → re-pin
    host.dispatchEvent(new Event("scroll"));
    expect(a.isPinned).toBe(true);
    const pill = host.parentElement!.querySelector<HTMLButtonElement>(".scroll-new-pill")!;
    expect(pill.hidden).toBe(true);
  });

  it("onNewTurn re-pins even after the user had scrolled up", () => {
    const host = makeHost();
    const a = new ScrollAnchor(host);
    setGeometry(host, 1000, 2000, 500);
    host.dispatchEvent(new Event("scroll"));
    setGeometry(host, 100, 2000, 500);
    host.dispatchEvent(new Event("scroll"));
    expect(a.isPinned).toBe(false);
    a.onNewTurn();
    expect(a.isPinned).toBe(true);
  });

  it("clicking the pill re-pins and hides it", () => {
    const host = makeHost();
    const a = new ScrollAnchor(host);
    a.mountPill();
    setGeometry(host, 1000, 2000, 500);
    host.dispatchEvent(new Event("scroll"));
    setGeometry(host, 100, 2000, 500);
    host.dispatchEvent(new Event("scroll"));
    const pill = host.parentElement!.querySelector<HTMLButtonElement>(".scroll-new-pill")!;
    expect(pill.hidden).toBe(false);
    pill.click();
    expect(a.isPinned).toBe(true);
    expect(pill.hidden).toBe(true);
  });

  it("growing content height alone (no upward move) does not un-pin", () => {
    const host = makeHost();
    const a = new ScrollAnchor(host);
    setGeometry(host, 1000, 2000, 500);
    host.dispatchEvent(new Event("scroll"));
    // Content grew (scrollHeight up) but scrollTop didn't move up → still pinned.
    setGeometry(host, 1000, 5000, 500);
    host.dispatchEvent(new Event("scroll"));
    expect(a.isPinned).toBe(true);
  });
});
