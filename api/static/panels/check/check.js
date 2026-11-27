// Run check: a side drawer, used while "Check that the notebook runs" is
// ticked. Draws each run of the notebook cell by cell, with the model's
// fixes between runs, from the `check` events in the progress updates of
// POST /convert/stream (see notebook_builder/verify.py).
//
// When Convert is clicked it starts collapsed, as a tab on the right edge
// ("Waiting for the model to write the notebook…"), since nothing runs
// until the notebook is written. It slides open by itself once, when the
// first check event arrives; the user can open it earlier from the tab,
// or close it at any time (Collapse button or Escape).
import { $, el } from "../../shared.js";

// Status kinds: "" (running, blue pulse), "idle" (waiting or skipped,
// grey), "ok", "warn" or "error".
const RESULT_KIND = { passed: "ok", failed: "warn", error: "error",
  skipped: "idle" };

export function initCheck() {
  let run = null;  // {box, items, note, attempt}
  let opened = false;  // opened by itself for this conversion yet
  const drawer = $("check-panel"), tab = $("check-tab");

  const isOpen = () => drawer.classList.contains("open");

  // Slide the drawer in or out; the tab shows while it is collapsed.
  function setOpen(open, { focus = false } = {}) {
    drawer.classList.toggle("open", open);
    tab.hidden = open || drawer.hidden;
    tab.setAttribute("aria-expanded", String(open));
    $("check-collapse").setAttribute("aria-expanded", String(open));
    if (focus) (open ? $("check-collapse") : tab).focus();
  }

  // Used for this conversion or not. When used, it starts collapsed: only
  // the tab shows until the check begins.
  function setUsed(used) {
    drawer.hidden = !used;
    drawer.classList.remove("open");
    tab.hidden = !used;
    tab.setAttribute("aria-expanded", "false");
  }

  // The check has begun: slide open, once per conversion. If the user
  // opened it already, or closes it later, this leaves it alone.
  function openOnce() {
    if (opened) return;
    opened = true;
    if (isOpen()) return;
    // Let the browser draw it off-screen first, so it slides in.
    requestAnimationFrame(() => requestAnimationFrame(() => setOpen(true)));
  }

  // kind: see RESULT_KIND above; also colours the tab's dot.
  function setStatus(text, kind = "") {
    $("check-status").textContent = text;
    $("check-status").className = kind === "idle" ? "" : kind;
    $("check-tab-dot").className = `dot ${kind || "running"}`;
    tab.title = `Run check: ${text}`;
  }

  tab.addEventListener("click", () => setOpen(true, { focus: true }));
  $("check-collapse").addEventListener("click",
    () => setOpen(false, { focus: true }));
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && isOpen()) {
      setOpen(false, { focus: true });
    }
  });

  // A <details> with a cell's output or error.
  function addDetail(li, label, text, open) {
    if (!text || !text.trim()) return;
    const box = el("details", { open }, el("summary",
      { textContent: label }), el("pre", { textContent: text.trim() }));
    li.append(box);
  }

  function startRun(plan) {
    // Earlier runs fold away; the latest stays open.
    document.querySelectorAll("#check-runs details.run")
      .forEach((d) => { d.open = false; });
    const summaryNote = el("span", { className: "sub",
      textContent: `${plan.cells.length} cells` });
    const summary = el("summary", {}, `Run ${plan.attempt}`, summaryNote);
    const items = plan.cells.map((cell) => el("li",
      { className: "pending", title: cell.source },
      el("div", { className: "cell-row" },
        el("span", { className: "dot", ariaHidden: "true" }),
        el("span", { className: "name", textContent: cell.label }),
        el("code", { textContent: cell.preview }),
        el("span", { className: "time" }))));
    const box = el("details", { className: "run", open: true }, summary,
      el("ol", { className: "cells" }, ...items));
    $("check-runs").append(box);
    run = { box, items, note: summaryNote, attempt: plan.attempt };
    $("check-attempt").textContent =
      `Run ${plan.attempt} of up to ${plan.max_attempts}`;
    setStatus(plan.attempt === 1 ? "Running the notebook with QUICK_RUN = " +
      "True…" : `Running again after fix ${plan.attempt - 1}…`);
  }

  function updateCell(event) {
    if (!run || event.attempt !== run.attempt) return;
    const li = event.index == null ? null : run.items[event.index];
    if (event.status === "failed") {
      const where = li ? li.querySelector(".name").textContent : "Setup";
      run.note.textContent = `failed at ${where}`;
      setStatus(`${where} failed: ${event.error}`, "error");
      if (!li) {
        const text = [event.error, event.details].filter(Boolean).join("\n");
        run.box.append(el("pre", { className: "run-error",
          textContent: text }));
        return;
      }
      li.className = "failed";
      li.querySelector(".time").textContent =
        event.timed_out ? "timed out" : "";
      addDetail(li, "Error", `${event.error}\n\n${event.details || ""}`,
        true);
      return;
    }
    if (!li) return;
    li.className = event.status;
    if (event.status === "running") {
      li.querySelector(".time").textContent = "running…";
      const done = run.items.filter((i) => i.className === "ok").length;
      run.note.textContent = `${done} of ${run.items.length} cells done`;
    } else if (event.status === "ok") {
      li.querySelector(".time").textContent = `${event.seconds.toFixed(1)} s`;
      addDetail(li, "Output", event.output, false);
    }
  }

  function addRepair(event) {
    if (event.status === "running") {
      setStatus("Asking the model to fix the failing cell…", "warn");
      return;
    }
    const failed = event.status === "failed";
    const changes = [
      event.cells && event.cells.length
        ? `changed ${event.cells.map((n) => `cell ${n}`).join(", ")}` : "",
      event.packages ? `packages: ${event.packages.join(", ") || "none"}`
        : "",
    ].filter(Boolean).join(" · ");
    $("check-runs").append(el("div",
      { className: `repair${failed ? " failed" : ""}` },
      el("strong", { textContent: failed
        ? "Could not fix it" : `Fix ${event.attempt}` }),
      el("span", { className: "sub", textContent: failed
        ? event.message : [event.explanation, changes]
          .filter(Boolean).join(" — ") })));
  }

  return {
    // enabled: the run check was requested for this conversion.
    start(enabled) {
      run = null;
      opened = false;
      $("check-runs").replaceChildren();
      $("check-attempt").textContent = "";
      $("check-setup").hidden = true;
      setStatus("Waiting for the model to write the notebook…", "idle");
      setUsed(enabled);
    },

    progress(update) {
      const check = update.check;
      if (!check || drawer.hidden) return;
      openOnce();
      if (check.type === "setup") {
        $("check-setup").textContent = check.message;
        $("check-setup").hidden = false;
      } else if (check.type === "plan") {
        $("check-setup").hidden = true;
        startRun(check);
      } else if (check.type === "cell") {
        updateCell(check);
      } else if (check.type === "repair") {
        addRepair(check);
      }
    },

    done(result) {
      if (drawer.hidden) return;
      const check = result.verification;
      if (!check) { setUsed(false); return; }
      setStatus(check.message, RESULT_KIND[check.status] || "");
      if (run && check.status === "passed") {
        run.note.textContent = "all cells passed";
      }
    },

    fail(message) {
      if (drawer.hidden) return;
      setStatus(`Stopped: ${message}`, "error");
      if (run) {
        run.items.filter((li) => li.className === "running")
          .forEach((li) => { li.className = "pending"; });
      }
    },
  };
}
