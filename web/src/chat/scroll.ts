// Chat-anchored scroll model (Canvas #242) — the make-or-break piece. The whole surface is one
// bottom-anchored column: newest work at the bottom, compose at the bottom, autoscroll to new
// content as it streams in. But a user who scrolls UP to re-read must NOT be yanked back down —
// so once they scroll away from the bottom we stop auto-pinning and show a "↓ new" pill; clicking
// it (or scrolling back to the bottom) resumes anchoring. This is the standard chat pattern, and
// the Canvas doc names it the decision: chat-style anchoring with addressable cells.
//
// One ScrollAnchor governs the single scroll container (the main column) and is shared by BOTH
// renderers — the chat transcript and the cell view — so scrolling behaves identically whichever
// costume a turn is wearing. Framework-free; no timers beyond rAF.

// How close to the bottom (px) still counts as "pinned". A little slack absorbs sub-pixel /
// fractional scroll heights and in-flight layout.
const BOTTOM_SLACK = 24;
// How far up (px) a user must scroll before we treat it as "they want to read" and stop yanking.
const AWAY_THRESHOLD = 120;

export class ScrollAnchor {
  private pinned = true;
  private lastTop = 0;
  private pill: HTMLButtonElement | null = null;

  // `scrollHost` is the element that actually scrolls (the main column). `pillHost` is where the
  // "↓ new" pill mounts (defaults to the scrollHost's parent so it can position over the column).
  constructor(
    private readonly scrollHost: HTMLElement,
    private readonly pillHost: HTMLElement = scrollHost.parentElement ?? scrollHost,
  ) {
    this.watch();
  }

  private fromBottom(): number {
    const h = this.scrollHost;
    return h.scrollHeight - h.scrollTop - h.clientHeight;
  }

  private watch(): void {
    this.scrollHost.addEventListener(
      "scroll",
      () => {
        const fromBottom = this.fromBottom();
        // Scrolling UP away from the bottom un-pins (the user wants to read); returning to the
        // bottom re-pins. Only an upward move un-pins, so streaming content growing the height
        // (which increases fromBottom without the user acting) never falsely un-pins.
        if (this.scrollHost.scrollTop < this.lastTop && fromBottom > AWAY_THRESHOLD) {
          this.setPinned(false);
        }
        if (fromBottom < BOTTOM_SLACK) this.setPinned(true);
        this.lastTop = this.scrollHost.scrollTop;
      },
      { passive: true },
    );
  }

  private setPinned(pinned: boolean): void {
    if (this.pinned === pinned) return;
    this.pinned = pinned;
    if (this.pill) this.pill.hidden = pinned;
  }

  /** True while the column is anchored to the bottom (auto-scroll active). */
  get isPinned(): boolean {
    return this.pinned;
  }

  /** Call when a brand-new turn/cell is appended (a new question resumes anchoring, like a chat). */
  onNewTurn(): void {
    this.setPinned(true);
    this.toBottom();
  }

  /** Re-anchor to the bottom instantly — for switching to another chat or flipping the view, so
   *  each surface opens pinned at its own bottom (and the "↓ New" pill can't linger from the last
   *  one). Instant, not smooth, since the content changed wholesale. */
  reset(): void {
    this.setPinned(true);
    requestAnimationFrame(() => this.toBottom(false));
  }

  /** Scroll to the bottom if (and only if) still pinned. Safe to call on every stream delta /
   *  repaint — a scrolled-up reader is left in place. Double-rAF so it runs after layout. Uses
   *  instant ("auto") follow so rapid deltas don't queue overlapping smooth animations. */
  maybeScroll(): void {
    if (!this.pinned) return;
    requestAnimationFrame(() =>
      requestAnimationFrame(() => {
        if (this.pinned) this.toBottom(false);
      }),
    );
  }

  private toBottom(smooth = true): void {
    this.scrollHost.scrollTo({ top: this.scrollHost.scrollHeight, behavior: smooth ? "smooth" : "auto" });
  }

  /** Mount the "↓ new" pill (idempotent). Clicking it re-pins and jumps to the newest content. */
  mountPill(): void {
    if (this.pill) return;
    const pill = document.createElement("button");
    pill.type = "button";
    pill.className = "scroll-new-pill";
    pill.textContent = "↓ New";
    pill.setAttribute("aria-label", "Jump to newest");
    pill.hidden = this.pinned;
    pill.addEventListener("click", () => {
      this.setPinned(true);
      this.toBottom();
    });
    this.pillHost.appendChild(pill);
    this.pill = pill;
  }
}
