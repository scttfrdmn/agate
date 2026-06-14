// Shared app chrome: a top bar with a hamburger toggle + a pop-out (off-canvas)
// side-navigation drawer. Used by both the main SPA and the admin console so the
// look + a11y behaviour are identical.
//
// Accessibility is built in: the toggle is a real button with aria-expanded /
// aria-controls; the drawer is a labelled dialog; Esc closes it; focus moves into
// the drawer on open and returns to the toggle on close; the scrim closes on click.

export interface NavItem {
  label: string;
  href?: string; // anchor target (hash route) — omit for a button-style action
  onSelect?: () => void; // called when chosen (and the drawer closes)
  current?: boolean; // marks aria-current="page"
  icon?: string; // optional leading glyph
}

export interface ChromeOptions {
  brand: string;
  tag?: string; // muted text after the brand (e.g. "admin console")
  items: NavItem[];
  // Optional right-aligned controls (e.g. a login/logout button) appended to the bar.
  actions?: HTMLElement[];
}

// Build the top bar + drawer + scrim, returning the topbar element to mount and a
// handful of controls. The drawer/scrim are appended to <body> (fixed-positioned).
export function mountChrome(opts: ChromeOptions): { topbar: HTMLElement } {
  const drawerId = "agate-nav-drawer";

  const toggle = elBtn("nav-toggle", "☰");
  toggle.setAttribute("aria-label", "Open navigation");
  toggle.setAttribute("aria-controls", drawerId);
  toggle.setAttribute("aria-expanded", "false");

  const brand = el("div", "brand");
  brand.append(document.createTextNode(opts.brand));
  if (opts.tag) brand.appendChild(el("span", "tag", opts.tag));

  const topbar = el("header", "topbar");
  topbar.setAttribute("role", "banner");
  topbar.append(toggle, brand, el("div", "spacer"));
  for (const a of opts.actions ?? []) topbar.appendChild(a);

  // Drawer (a labelled dialog) + scrim.
  const scrim = el("div", "nav-scrim");
  const drawer = el("nav", "nav-drawer");
  drawer.id = drawerId;
  drawer.setAttribute("aria-label", "Primary");
  drawer.setAttribute("role", "dialog");
  drawer.setAttribute("aria-modal", "true");
  drawer.hidden = true;

  const head = el("div", "nav-head");
  head.appendChild(el("strong", "", opts.brand));
  const close = elBtn("nav-toggle", "✕");
  close.setAttribute("aria-label", "Close navigation");
  head.appendChild(close);
  drawer.appendChild(head);

  for (const item of opts.items) {
    const node = item.href
      ? (el("a", "") as HTMLAnchorElement)
      : (elBtn("nav-link", "") as HTMLButtonElement);
    if (item.href) (node as HTMLAnchorElement).href = item.href;
    if (item.current) node.setAttribute("aria-current", "page");
    node.textContent = (item.icon ? `${item.icon}  ` : "") + item.label;
    node.addEventListener("click", () => {
      item.onSelect?.();
      closeDrawer();
    });
    drawer.appendChild(node);
  }

  document.body.append(scrim, drawer);

  function openDrawer(): void {
    drawer.hidden = false;
    // next frame so the transform transition runs
    requestAnimationFrame(() => {
      drawer.classList.add("open");
      scrim.classList.add("open");
    });
    toggle.setAttribute("aria-expanded", "true");
    (drawer.querySelector("a, button") as HTMLElement | null)?.focus();
    document.addEventListener("keydown", onKey);
  }
  function closeDrawer(): void {
    drawer.classList.remove("open");
    scrim.classList.remove("open");
    toggle.setAttribute("aria-expanded", "false");
    document.removeEventListener("keydown", onKey);
    // hide after the transition so it leaves the tab order
    window.setTimeout(() => {
      if (!drawer.classList.contains("open")) drawer.hidden = true;
    }, 200);
    toggle.focus();
  }
  function onKey(e: KeyboardEvent): void {
    if (e.key === "Escape") closeDrawer();
  }

  toggle.addEventListener("click", () => (drawer.hidden ? openDrawer() : closeDrawer()));
  close.addEventListener("click", closeDrawer);
  scrim.addEventListener("click", closeDrawer);

  return { topbar };
}

function el(tag: string, cls = "", text?: string): HTMLElement {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}
function elBtn(cls: string, text: string): HTMLButtonElement {
  const b = document.createElement("button");
  b.type = "button";
  b.className = cls;
  b.textContent = text;
  return b;
}
