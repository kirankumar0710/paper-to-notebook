// 4. Metrics: tokens, time, sizes and cell counts, from the progress and
// result events of POST /convert/stream.
import { $, fmtBytes, fmtDuration, fmtInt } from "../../shared.js";

export function initMetrics() {
  function showUsage(usage) {
    $("m-in").textContent = fmtInt(usage.input_tokens);
    $("m-out").textContent = fmtInt(usage.output_tokens);
    $("m-total").textContent = fmtInt(usage.total_tokens);
  }

  function showFile(file) {
    $("m-upload").textContent = fmtBytes(file.size);
    $("m-upload-d").textContent = file.name;
  }

  return {
    showFile,

    start(file, config) {
      for (const id of ["m-in", "m-out", "m-total", "m-time", "m-nb",
        "m-cells", "m-chars"]) $(id).textContent = "—";
      $("m-cells-d").textContent = "";
      $("m-time-d").textContent = "running…";
      showFile(file);
      $("metrics-note").textContent = config && config.provider
        ? `Running on ${config.provider} · ${config.model}` : "Running…";
    },

    tick(seconds) {
      $("m-time").textContent = fmtDuration(seconds);
    },

    progress(update) {
      if (update.output_chars) {
        $("m-chars").textContent = fmtInt(update.output_chars);
      }
      if (update.usage) showUsage(update.usage);
    },

    done(result) {
      if (result.usage) showUsage(result.usage);
      $("m-time").textContent = fmtDuration(result.elapsed);
      $("m-time-d").textContent = "upload → notebook";
      $("m-upload").textContent = fmtBytes(result.upload_bytes);
      $("m-nb").textContent = fmtBytes(result.notebook_bytes);
      $("m-cells").textContent = fmtInt(result.cells);
      $("m-cells-d").textContent =
        `${result.markdown_cells} markdown · ${result.code_cells} code`;
      $("metrics-note").textContent = `${result.provider} · ${result.model}` +
        (result.usage ? "" : " · token counts not reported by the provider");
    },

    fail() {
      $("m-time-d").textContent = "failed";
    },
  };
}
