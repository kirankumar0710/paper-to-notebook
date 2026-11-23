// 4. Metrics: tokens, cost, output budget, time per stage, output speed,
// paper and notebook facts, and totals since the server started.
// Per-run values come from the progress and result events of
// POST /convert/stream; totals from GET /api/metrics/session.
import { $, el, fmtBytes, fmtDuration, fmtInt } from "../../shared.js";

const STAGE_LABELS = {
  upload: "Upload", fetching: "arXiv fetch", waiting: "Waiting", retrying: "Retrying",
  thinking: "Thinking", writing: "Writing", building: "Building",
};
// Budget use above this share shows as a warning: the next run may be
// cut off at max_tokens.
const BUDGET_WARN = 0.8;

const fmtUsd = (n) => (n == null ? "—"
  : n < 0.01 ? `$${n.toFixed(4)}` : `$${n.toFixed(2)}`);

const RUN_IDS = ["m-in", "m-out", "m-total", "m-cost", "m-budget", "m-time",
  "m-speed", "m-pages", "m-nb", "m-cells", "m-code", "m-pkgs", "m-gpu"];
const RUN_DETAIL_IDS = ["m-cost-d", "m-budget-d", "m-speed-d", "m-pages-d",
  "m-chars-d", "m-cells-d", "m-pkgs-d"];

export function initMetrics() {
  // Stage timeline for the current run: [{stage, start}] in ms, plus end.
  let stages = [];
  let endedAt = null;
  let outputChars = 0;

  function set(id, text, warn = false) {
    $(id).textContent = text;
    $(id).classList.toggle("warn", warn);
  }

  // ---- 2. time per stage ----
  function enterStage(stage) {
    // "done" is the instant the reply ends; building starts right after.
    if (stage === "done" || stage === "uploaded") return;
    const last = stages[stages.length - 1];
    if (last && last.stage === stage) return;
    stages.push({ stage, start: Date.now() });
  }
  function durations() {
    const end = endedAt || Date.now();
    const totals = {};
    stages.forEach((s, i) => {
      const next = i + 1 < stages.length ? stages[i + 1].start : end;
      totals[s.stage] = (totals[s.stage] || 0) + (next - s.start) / 1000;
    });
    return totals;
  }
  function renderStages() {
    const totals = durations();
    const all = Object.values(totals).reduce((a, b) => a + b, 0) || 1;
    const order = Object.keys(STAGE_LABELS).filter((s) => s in totals);
    $("m-stages").replaceChildren(...order.map((s) => el("span", {
      className: `stage-${s}`, title: STAGE_LABELS[s],
      style: `width: ${(100 * totals[s]) / all}%` })));
    $("m-stage-legend").replaceChildren(...order.map((s) => el("li", {},
      el("i", { className: `stage-${s}` }),
      `${STAGE_LABELS[s]} ${fmtDuration(totals[s])}`)));
  }

  // ---- 5. output speed ----
  function renderSpeed(usage) {
    const totals = durations();
    const writing = totals.writing || 0;
    const generating = writing + (totals.thinking || 0);
    if (usage && usage.output_tokens && generating > 0) {
      set("m-speed", `${fmtInt(Math.round(usage.output_tokens / generating))}`
        + " tok/s");
      $("m-speed-d").textContent = writing > 0
        ? `${fmtInt(Math.round(outputChars / writing))} chars/s while writing`
        : "thinking + writing";
    } else if (writing > 0 && outputChars) {
      set("m-speed", `${fmtInt(Math.round(outputChars / writing))} ch/s`);
      $("m-speed-d").textContent = "while writing";
    }
  }

  // ---- 1, 3. tokens, cost and budget ----
  function showUsage(usage) {
    $("m-in").textContent = fmtInt(usage.input_tokens);
    $("m-out").textContent = fmtInt(usage.output_tokens);
    $("m-total").textContent = fmtInt(usage.total_tokens);
  }
  function showCost(result) {
    const cost = result.cost;
    if (cost) {
      set("m-cost", fmtUsd(cost.total_usd));
      $("m-cost-d").textContent = `${cost.speed === "fast" ? "fast · " : ""}` +
        `$${cost.input_rate} / $${cost.output_rate} per M`;
    } else {
      set("m-cost", "—");
      $("m-cost-d").textContent = result.usage
        ? `no price for ${result.served.model} in config.toml`
        : "no token counts";
    }
  }
  function showBudget(result) {
    const max = result.max_output_tokens;
    const used = result.usage && result.usage.output_tokens;
    const bar = $("m-budget-bar");
    if (!max || used == null) {
      set("m-budget", "—");
      $("m-budget-d").textContent = max ? "" : "no fixed budget";
      bar.style.width = "0";
      return;
    }
    const share = used / max;
    set("m-budget", `${Math.round(share * 100)}%`, share >= BUDGET_WARN);
    bar.style.width = `${Math.min(100, share * 100)}%`;
    bar.className = share >= 1 ? "bad" : share >= BUDGET_WARN ? "warn" : "";
    $("m-budget-d").textContent = `${fmtInt(used)} of ${fmtInt(max)}`;
  }

  // ---- 4. what actually ran ----
  function showServed(result) {
    const served = result.served;
    const note = $("metrics-served");
    const speed = served.speed ? ` · ${served.speed} speed` : "";
    const warnings = [];
    if (served.model_fallback) {
      warnings.push(`answered by fallback model ${served.model} ` +
        `(asked for ${result.model})`);
    }
    if (served.speed_fallback) {
      warnings.push("fast mode was busy, so it ran at standard speed");
    }
    note.textContent = `Served by ${result.provider} · ${served.model}` +
      speed + (warnings.length ? ` — ${warnings.join("; ")}` : "");
    note.className = warnings.length ? "warn" : "";
    note.hidden = false;
  }

  // ---- 6, 7. paper and notebook ----
  function showPaper(result) {
    $("m-pages").textContent = fmtInt(result.pages);
    $("m-pages-d").textContent = result.pages && result.usage
      ? `${fmtInt(Math.round(result.usage.input_tokens / result.pages))} ` +
        "tokens/page"
      : result.pages == null ? "could not count" : "";
    $("m-upload").textContent = fmtBytes(result.upload_bytes);
    if (result.source && result.source.type === "arxiv") {
      $("m-upload-d").textContent = `arXiv ${result.source.paper_id} · ` +
        result.source.title;
    }
    $("m-nb").textContent = fmtBytes(result.notebook_bytes);
    $("m-chars-d").textContent = outputChars
      ? `${fmtInt(outputChars)} characters written` : "";
    $("m-cells").textContent = fmtInt(result.cells);
    $("m-cells-d").textContent =
      `${result.markdown_cells} markdown · ${result.code_cells} code`;
    $("m-code").textContent = fmtInt(result.code_lines);
    const pkgs = result.pip_packages, dropped = result.dropped_packages;
    set("m-pkgs", fmtInt(pkgs.length), dropped.length > 0);
    $("m-pkgs-d").textContent = [
      pkgs.join(", "),
      dropped.length ? `${dropped.length} unsafe dropped` : "",
    ].filter(Boolean).join(" · ");
    $("m-gpu").textContent = result.requires_gpu ? "Needed" : "No";
  }

  // ---- 8. since the server started ----
  function showSession(session) {
    if (!session) return;
    $("s-count").textContent = fmtInt(session.conversions);
    $("s-count-d").textContent = session.failed
      ? `${session.failed} failed` : "";
    $("s-tokens").textContent = fmtInt(session.total_tokens);
    $("s-tokens-d").textContent = `${fmtInt(session.input_tokens)} in · ` +
      `${fmtInt(session.output_tokens)} out`;
    $("s-cost").textContent = fmtUsd(session.cost_usd);
    $("s-cost-d").textContent = session.unpriced
      ? `${session.unpriced} without a price` : "";
  }
  async function loadSession() {
    try {
      showSession(await (await fetch("/api/metrics/session")).json());
    } catch (_) { /* leave as is */ }
  }
  loadSession();

  // paper: a File, or {name, size} (size unknown until an arXiv PDF
  // has been downloaded).
  function showFile(paper) {
    $("m-upload").textContent = fmtBytes(paper.size);
    $("m-upload-d").textContent = paper.name;
  }

  return {
    showFile,

    start(paper, config) {
      RUN_IDS.forEach((id) => set(id, "—"));
      RUN_DETAIL_IDS.forEach((id) => { $(id).textContent = ""; });
      $("m-budget-bar").style.width = "0";
      $("m-time-d").textContent = "running…";
      $("metrics-served").hidden = true;
      stages = [{ stage: paper.kind === "arxiv" ? "fetching" : "upload",
        start: Date.now() }];
      endedAt = null;
      outputChars = 0;
      renderStages();
      showFile(paper);
      $("metrics-note").textContent = config && config.provider
        ? `Running on ${config.provider} · ${config.model}` : "Running…";
      $("metrics-note").hidden = false;
    },

    tick(seconds) {
      $("m-time").textContent = fmtDuration(seconds);
      renderStages();
      renderSpeed(null);
    },

    progress(update) {
      enterStage(update.stage);
      if (update.output_chars) {
        outputChars = update.output_chars;
      }
      if (update.usage) showUsage(update.usage);
    },

    done(result) {
      endedAt = Date.now();
      if (result.usage) showUsage(result.usage);
      renderStages();
      renderSpeed(result.usage);
      $("m-time").textContent = fmtDuration(result.elapsed);
      $("m-time-d").textContent = "upload → notebook";
      showCost(result);
      showBudget(result);
      showPaper(result);
      showServed(result);
      showSession(result.session);
      $("metrics-note").textContent = result.usage
        ? "" : "Token counts were not reported by the provider.";
      $("metrics-note").hidden = Boolean(result.usage);
    },

    fail() {
      endedAt = Date.now();
      renderStages();
      $("m-time-d").textContent = "failed";
      loadSession();
    },
  };
}
