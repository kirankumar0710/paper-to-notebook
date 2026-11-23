// 3. LLM output: stages, activity log and the model's reasoning summary,
// from the progress events of POST /convert/stream.
import { $, atBottom, clock, el, fmtBytes, fmtDuration, follow }
  from "../../shared.js";

const STEPS = ["uploaded", "waiting", "thinking", "writing", "building"];
// Fetching an arXiv paper is part of the first step, "Paper".
const STEP_OF = { retrying: "waiting", fetching: "uploaded" };

export function initOutput() {
  let lastLogStage = null;

  function log(text, kind, stage) {
    const list = $("log");
    const stick = atBottom(list);
    // Repeated updates from the same stage replace the previous line.
    if (stage && stage === lastLogStage && list.lastChild) {
      list.lastChild.lastChild.textContent = text;
    } else {
      list.append(el("li", { className: kind || "" },
        el("time", { textContent: clock() }),
        el("span", { textContent: text })));
    }
    lastLogStage = stage || null;
    follow(list, stick);
  }

  function showStep(stage) {
    const index = STEPS.indexOf(STEP_OF[stage] || stage);
    if (index < 0) return;
    document.querySelectorAll("#steps li").forEach((li, i) => {
      li.className = i < index ? "complete" : i === index ? "active" : "";
    });
  }

  return {
    // source: {kind: "file", name, size} or {kind: "arxiv", ref}.
    start(source) {
      $("output-empty").hidden = true;
      $("output-body").hidden = false;
      $("log").replaceChildren();
      lastLogStage = null;
      $("thinking").textContent = "";
      $("thinking-wrap").hidden = true;
      $("elapsed").textContent = "";
      $("stage").className = "";
      showStep("uploaded");
      if (source.kind === "arxiv") {
        $("stage-text").textContent = `Fetching ${source.ref} from arXiv…`;
        log(`Fetching ${source.ref} from arXiv (see the MCP activity log)`);
      } else {
        $("stage-text").textContent = `Uploading ${source.name}…`;
        log(`Uploading ${source.name} (${fmtBytes(source.size)})`, "",
          "upload");
      }
    },

    tick(seconds) {
      $("elapsed").textContent = `· ${fmtDuration(seconds)}`;
    },

    progress(update) {
      showStep(update.stage);
      $("stage-text").textContent = update.message;
      // Each arXiv fetch step gets its own line; other repeated updates
      // from one stage replace the previous line.
      log(update.message, "",
        update.stage === "fetching" ? null : update.stage);
      // Append each new piece, so the whole reasoning summary can be
      // scrolled; fall back to the excerpt if no deltas are sent.
      const box = $("thinking");
      const text = update.thinking_delta ||
        (update.thinking && !box.textContent ? update.thinking : "");
      if (text) {
        const stick = atBottom(box) || $("thinking-wrap").hidden;
        $("thinking-wrap").hidden = false;
        box.append(text);
        follow(box, stick);
      }
    },

    done(result) {
      document.querySelectorAll("#steps li")
        .forEach((li) => { li.className = "complete"; });
      $("elapsed").textContent = "";
      const text = `Done in ${fmtDuration(result.elapsed)} — ` +
        `${result.cells} cells.`;
      $("stage-text").textContent = text;
      $("stage").className = "ok";
      log(text, "ok");
    },

    fail(message) {
      $("stage-text").textContent = "Conversion failed.";
      $("stage").className = "error";
      log(message, "error");
    },
  };
}
