// Helpers shared by the panels.

export const $ = (id) => document.getElementById(id);

export function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  Object.assign(node, props);
  node.append(...children);
  return node;
}

export const fmtInt = (n) => (n == null ? "—" : Number(n).toLocaleString());

export function fmtBytes(n) {
  if (n == null) return "—";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(2)} MB`;
}

export function fmtDuration(seconds) {
  const s = Math.round(seconds);
  return s < 60 ? `${s}s`
    : `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`;
}

export const clock = (d = new Date()) => d.toLocaleTimeString([], {
  hour: "2-digit", minute: "2-digit", second: "2-digit" });

// Scroll boxes follow new text, unless the reader has scrolled up.
export function atBottom(box) {
  return box.scrollHeight - box.scrollTop - box.clientHeight < 24;
}
export function follow(box, wasAtBottom) {
  if (wasAtBottom) box.scrollTop = box.scrollHeight;
}

// Reads a text/event-stream response and calls onEvent(name, data).
export async function readEvents(response, onEvent) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let boundary;
    while ((boundary = buffer.indexOf("\n\n")) >= 0) {
      const block = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      let name = "message", data = "";
      for (const line of block.split("\n")) {
        if (line.startsWith("event: ")) name = line.slice(7);
        else if (line.startsWith("data: ")) data += line.slice(6);
      }
      if (data) onEvent(name, JSON.parse(data));
    }
  }
}

// Tabs in a panel header (ARIA tabs pattern). `tablist` holds buttons with
// role="tab" whose aria-controls names their tab panel. Elements in the
// same panel with data-tab-for="<tab id>" (e.g. a tab's own Refresh in the
// header) show only while that tab is selected. Arrow keys, Home and End
// move between tabs. onShow(tabId) runs after each switch.
export function initTabs(tablist, { onShow } = {}) {
  const tabs = [...tablist.querySelectorAll('[role="tab"]')];
  const panel = tablist.closest("section");

  function show(id, { focus = false } = {}) {
    tabs.forEach((tab) => {
      const selected = tab.id === id;
      tab.setAttribute("aria-selected", String(selected));
      tab.tabIndex = selected ? 0 : -1;
      $(tab.getAttribute("aria-controls")).hidden = !selected;
      if (selected && focus) tab.focus();
    });
    panel.querySelectorAll("[data-tab-for]").forEach((node) => {
      node.hidden = node.dataset.tabFor !== id;
    });
    if (onShow) onShow(id);
  }

  tabs.forEach((tab, index) => {
    tab.addEventListener("click", () => show(tab.id));
    tab.addEventListener("keydown", (event) => {
      const moves = { ArrowRight: index + 1, ArrowLeft: index - 1,
        Home: 0, End: tabs.length - 1 };
      if (!(event.key in moves)) return;
      event.preventDefault();
      show(tabs[(moves[event.key] + tabs.length) % tabs.length].id,
        { focus: true });
    });
  });
  const selected = () =>
    tabs.find((tab) => tab.getAttribute("aria-selected") === "true").id;
  show(selected());
  return { show, selected };
}

// Shows `text` in a tab's badge, or hides the badge when text is empty.
export function setBadge(id, text) {
  $(id).hidden = !text;
  $(id).textContent = text || "";
}
