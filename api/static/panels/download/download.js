// Download (a tab of the Paper panel): the new notebook, plus every
// notebook generated since the server started (GET /api/notebooks), each
// with the recipe it was made with: source, model, options, depth, run
// check, time, tokens and cost.
import { $, clock, el, fmtBytes, fmtDuration, fmtInt } from "../../shared.js";

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

const DEPTHS = { beginner: "Beginner", practitioner: "Practitioner",
  expert: "Expert" };
const CHECKS = { passed: "✓ checked", failed: "⚠ check failed",
  skipped: "check skipped", error: "check error" };

const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "es"}`;

function sourceText(source) {
  if (!source) return "—";
  if (source.type === "arxiv") {
    return `arXiv ${source.paper_id}` +
      (source.title ? ` · ${source.title}` : "");
  }
  return `Upload · ${source.filename || "PDF"}`;
}

function checkText(check) {
  if (!check) return "Off";
  const label = { passed: "Passed", failed: "Failed", skipped: "Skipped",
    error: "Error" }[check.status] || check.status;
  return check.repairs ? `${label} after ${plural(check.repairs, "fix")}`
    : label;
}

// One line under a notebook, e.g. "anthropic · claude-opus-5-5 · Expert ·
// ✓ checked", opening to the full recipe.
function recipeView(recipe) {
  if (!recipe || !recipe.provider) return "";
  const summary = [recipe.provider, recipe.model,
    DEPTHS[recipe.depth] || recipe.depth,
    recipe.run_check ? CHECKS[recipe.run_check.status] : ""]
    .filter(Boolean).join(" · ");
  const options = Object.entries(recipe.options || {})
    .map(([k, v]) => `${k}=${v}`).join(" · ");
  const run = [
    recipe.elapsed != null ? fmtDuration(recipe.elapsed) : "",
    recipe.total_tokens != null ? `${fmtInt(recipe.total_tokens)} tokens`
      : "",
    recipe.cost_usd != null ? `$${recipe.cost_usd.toFixed(2)}` : "",
  ].filter(Boolean).join(" · ");
  const rows = [
    ["Source", sourceText(recipe.source)],
    ["Model", `${recipe.provider} · ${recipe.model}` +
      (recipe.speed ? ` (${recipe.speed})` : "")],
    ["Options", options || "—"],
    ["Depth", DEPTHS[recipe.depth] || recipe.depth || "—"],
    ["Run check", checkText(recipe.run_check)],
    ["Run", run || "—"],
  ];
  return el("details", { className: "recipe" },
    el("summary", { textContent: summary, title: "How it was made" }),
    el("dl", {}, ...rows.flatMap(([k, v]) => [
      el("dt", { textContent: k }), el("dd", { textContent: v })])));
}

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
          (f.exists ? fmtBytes(f.size) : "file moved or deleted") }),
        recipeView(f.recipe)),
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
