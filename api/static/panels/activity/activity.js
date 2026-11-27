// Activity: LLM output (default), MCP log and Metrics as tabs of one
// panel. The tabs' content is drawn by panels/output/, panels/mcp/ and
// panels/metrics/; this file switches tabs and counts MCP events that
// arrive while the MCP log tab is not shown.
import { $, initTabs, setBadge } from "../../shared.js";

const TABS = { output: "tab-output", mcp: "tab-mcp",
  metrics: "tab-metrics" };

export function initActivity() {
  let unseen = 0;

  function showBadge() {
    setBadge("mcp-badge", unseen ? (unseen > 99 ? "99+" : String(unseen))
      : "");
    $(TABS.mcp).setAttribute("aria-label",
      unseen ? `MCP log, ${unseen} new` : "MCP log");
  }

  const tabs = initTabs($("activity-tabs"), {
    onShow(id) {
      if (id === TABS.mcp) { unseen = 0; showBadge(); }
    },
  });

  return {
    // name: "output", "mcp" or "metrics".
    show(name) { tabs.show(TABS[name]); },

    // count new MCP events arrived; badge them unless the tab is shown.
    mcpEvents(count) {
      if (tabs.selected() !== TABS.mcp) {
        unseen += count;
        showBadge();
      }
    },
  };
}
