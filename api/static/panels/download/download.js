// 5. Download: the new notebook, plus every notebook generated since the
// server started (GET /api/notebooks).
import { $, clock, el, fmtBytes } from "../../shared.js";

// How each run check result reads under the download link.
const VERIFY_TEXT = {
  passed: (v) => `✓ Checked: all ${v.code_cells} code cells ran` +
    (v.repairs ? ` after ${v.repairs} automatic ` +
      `fix${v.repairs === 1 ? "" : "es"}` : ""),
  failed: (v) => `⚠ Checked: ${v.message} Review it before running.`,
  skipped: (v) => v.message,
  error: (v) => v.message,
};
const VERIFY_KIND = { passed: "ok", failed: "warn", error: "error" };

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
      const check = result.verification, note = $("verify-result");
      note.hidden = !check;
      if (check) {
        note.textContent = VERIFY_TEXT[check.status](check);
        note.className = VERIFY_KIND[check.status] || "";
      }
      $("current").hidden = false;
      link.click();
      refresh();
    },
  };
}
