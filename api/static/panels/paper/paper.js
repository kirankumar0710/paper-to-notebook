// Paper: Upload, Download and Configuration as tabs of one panel. The
// tabs' content is drawn by panels/upload/, panels/download/ and
// panels/config/; this file switches tabs and flags configuration
// problems on the Configuration tab.
import { $, initTabs, setBadge } from "../../shared.js";

const TABS = { upload: "tab-upload", download: "tab-download",
  config: "tab-config" };

export function initPaper() {
  const tabs = initTabs($("paper-tabs"));
  let checked = false;

  return {
    // name: "upload", "download" or "config".
    show(name) { tabs.show(TABS[name]); },

    // problems: the messages from GET /api/config. They are flagged on
    // the Configuration tab, which is opened once, on the first load,
    // since nothing can be converted until they are fixed.
    configProblems(problems) {
      setBadge("config-badge", problems.length ? "!" : "");
      $(TABS.config).title = problems.length
        ? `${problems.length} setting(s) missing` : "";
      if (problems.length && !checked) tabs.show(TABS.config);
      checked = true;
    },
  };
}
