// 5. Download: the new notebook, plus every notebook generated since the
// server started (GET /api/notebooks).
import { $, clock, el, fmtBytes } from "../../shared.js";

export function initDownload() {
  async function refresh() {
    let files = [];
    try { files = await (await fetch("/api/notebooks")).json(); }
    catch (_) { /* keep empty */ }
    $("files").replaceChildren(...files.map((f) => el("li", {},
      el("span", { className: "meta" },
        el("span", { className: "name", textContent: f.filename,
          title: f.path }),
        el("span", { className: "sub", textContent:
          `${clock(new Date(f.created * 1000))} · ` +
          (f.exists ? fmtBytes(f.size) : "file moved or deleted") })),
      f.exists ? el("a", { href: f.download_url, textContent: "Download",
        download: f.filename }) : "")));
    $("files-empty").hidden = files.length > 0;
  }

  $("files-refresh").addEventListener("click", refresh);
  refresh();

  return {
    refresh,

    done(result) {
      const link = $("download-link");
      if (link.href.startsWith("blob:")) URL.revokeObjectURL(link.href);
      // Keep a visible link: browsers may block the automatic download
      // below because it happens long after the button was clicked.
      link.href = URL.createObjectURL(new Blob([result.notebook],
        { type: "application/x-ipynb+json" }));
      link.download = result.filename;
      link.textContent = `Download ${result.filename}`;
      $("saved-path").textContent = result.saved_path
        ? `Also saved to ${result.saved_path}`
        : "Could not save a server copy.";
      $("current").hidden = false;
      link.click();
      refresh();
    },
  };
}
