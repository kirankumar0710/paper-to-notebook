// Wires the dashboard panels together and runs a conversion.
//
// Each panel (panels/<name>/<name>.js) owns its own part of the page; this
// file only passes data between them. The Upload panel starts a conversion,
// and the events from POST /convert/stream go to the LLM output, Metrics
// and Download panels.
import { readEvents } from "./shared.js";
import { initConfig } from "./panels/config/config.js";
import { initUpload } from "./panels/upload/upload.js";
import { initOutput } from "./panels/output/output.js";
import { initMetrics } from "./panels/metrics/metrics.js";
import { initDownload } from "./panels/download/download.js";

let config = null;
const output = initOutput();
const metrics = initMetrics();
const download = initDownload();
const upload = initUpload({
  onChoose: (file) => metrics.showFile(file),
  onSubmit: convert,
});
initConfig({
  onLoad(loaded) {
    config = loaded;
    upload.setMaxMb(loaded.max_upload_mb);
  },
});

// Returns the error message from a non-2xx response.
async function errorDetail(response) {
  let detail = response.statusText;
  try { detail = (await response.json()).detail || detail; }
  catch (_) { /* keep statusText */ }
  return typeof detail === "string" ? detail : JSON.stringify(detail);
}

async function convert(file) {
  upload.setBusy(true);
  upload.setStatus("Converting… this usually takes a minute or more.");
  output.start(file);
  metrics.start(file, config);

  const startedAt = Date.now();
  const timer = setInterval(() => {
    const seconds = (Date.now() - startedAt) / 1000;
    output.tick(seconds);
    metrics.tick(seconds);
  }, 1000);

  try {
    const body = new FormData();
    body.append("file", file);
    const response = await fetch("/convert/stream", { method: "POST", body });
    if (!response.ok) throw new Error(await errorDetail(response));

    let finished = false;
    await readEvents(response, (name, data) => {
      if (name === "progress") {
        output.progress(data);
        metrics.progress(data);
      } else if (name === "result") {
        finished = true;
        clearInterval(timer);
        output.done(data);
        metrics.done(data);
        download.done(data);
        upload.setStatus("Your notebook is ready. If it did not download " +
          "automatically, use the Download panel.", "ok");
      } else if (name === "error") {
        finished = true;
        throw new Error(data.detail);
      }
    });
    if (!finished) {
      throw new Error("The connection closed before the notebook was " +
        "ready. It may still be saved on the server.");
    }
  } catch (error) {
    upload.setStatus(`Conversion failed: ${error.message}`, "error");
    output.fail(error.message);
    metrics.fail();
    download.refresh();
  } finally {
    clearInterval(timer);
    upload.setBusy(false);
  }
}
