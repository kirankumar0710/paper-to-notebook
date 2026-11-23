// MCP activity log: polls GET /api/mcp/events for what the server did with
// the arXiv MCP server (start, tool calls, results, the PDF download,
// errors) and lists it, newest at the bottom.
import { $, atBottom, clock, el, follow } from "../../shared.js";

const KIND_LABELS = {
  server: "Server", call: "Tool call", result: "Result", http: "HTTP",
  error: "Error",
};
const IDLE_MS = 3000;     // poll interval when nothing is running
const ACTIVE_MS = 700;    // while a conversion is running

export function initMcp() {
  const list = $("mcp-events");
  let after = 0;          // last event id received
  let lastRun = null;
  let active = false;
  let timer = null;

  function render(event) {
    // A heading whenever a new lookup starts.
    if (event.run && event.run !== lastRun) {
      list.append(el("li", { className: "run", textContent: event.run }));
      lastRun = event.run;
    }
    const row = el("div", { className: "row" },
      el("time", { textContent: clock(new Date(event.time * 1000)) }),
      el("span", { className: `kind ${event.kind}`,
        textContent: KIND_LABELS[event.kind] || event.kind }),
      el("span", { className: "msg", textContent: event.message }));
    if (event.duration_ms != null) {
      row.append(el("span", { className: "ms",
        textContent: `${Math.round(event.duration_ms)} ms` }));
    }
    const item = el("li", { className: event.kind }, row);
    if (event.detail) {
      item.append(el("span", { className: "detail",
        textContent: event.detail }));
    }
    list.append(item);
  }

  async function poll() {
    try {
      const response = await fetch(`/api/mcp/events?after=${after}`);
      if (!response.ok) throw new Error(response.statusText);
      const data = await response.json();
      const stick = atBottom(list);
      // Two polls can overlap (setActive polls at once); skip repeats.
      const fresh = data.events.filter((event) => event.id > after);
      fresh.forEach(render);
      after = Math.max(after, data.last_id);
      $("mcp-empty").hidden = list.children.length > 0;
      if (fresh.length) follow(list, stick);
      setLive(active ? "active" : "", active ? "Polling" : "Live");
    } catch (_) {
      setLive("off", "Offline");
    }
    schedule();
  }

  function setLive(state, text) {
    $("mcp-live").className = `live ${state}`;
    $("mcp-live-text").textContent = text;
  }

  function schedule() {
    clearTimeout(timer);
    timer = setTimeout(poll, active ? ACTIVE_MS : IDLE_MS);
  }

  $("mcp-clear").addEventListener("click", () => {
    // Clears this view only; later events still arrive.
    list.replaceChildren();
    lastRun = null;
    $("mcp-empty").hidden = false;
  });

  poll();

  return {
    // Poll faster while a conversion runs, then once more at the end.
    setActive(value) {
      active = value;
      clearTimeout(timer);
      poll();
    },
  };
}
