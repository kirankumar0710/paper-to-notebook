// Wires the dashboard panels together and runs a conversion.
//
// Each panel (panels/<name>/<name>.js) owns its own part of the page; this
// file only passes data between them. The Upload panel starts a conversion
// from a PDF or an arXiv link, and the events from POST /convert/stream go
// to the LLM output, Metrics, Download and (when the run check is used)
// Run check panels. The MCP activity log
// polls on its own and only needs to know when a conversion is running.
import { readEvents } from "./shared.js";
import { initConfig } from "./panels/config/config.js";
import { initUpload } from "./panels/upload/upload.js";
import { initOutput } from "./panels/output/output.js";
import { initMetrics } from "./panels/metrics/metrics.js";
import { initDownload } from "./panels/download/download.js";
import { initMcp } from "./panels/mcp/mcp.js";
import { initCheck } from "./panels/check/check.js";

let config = null;
const output = initOutput();
const metrics = initMetrics();
const download = initDownload();
const mcp = initMcp();
const check = initCheck();
const upload = initUpload({
  onChoose: (file) => metrics.showFile(file),
  onSubmit: convert,
});
initConfig({
  onLoad(loaded) {
    config = loaded;
    upload.setMaxMb(loaded.max_upload_mb);
    upload.setVerify(loaded.verify);
  },
});

// Returns the error message from a non-2xx response.
async function errorDetail(response) {
  let detail = response.statusText;
  try { detail = (await response.json()).detail || detail; }
  catch (_) { /* keep statusText */ }
  return typeof detail === "string" ? detail : JSON.stringify(detail);
}

// source: {kind: "file", file} or {kind: "arxiv", ref} from the Upload
// panel, with verify: whether to check that the notebook runs.
async function convert(source) {
  const isArxiv = source.kind === "arxiv";
  // What the output and metrics panels show for the paper.
  const paper = isArxiv
    ? { kind: "arxiv", ref: source.ref, name: `arXiv ${source.ref}` }
    : { kind: "file", name: source.file.name, size: source.file.size };
  upload.setBusy(true);
  upload.setStatus((isArxiv
    ? "Fetching the paper from arXiv, then converting… this usually " +
      "takes a minute or more."
    : "Converting… this usually takes a minute or more.") +
    (source.verify ? " Checking that it runs adds a few minutes." : ""));
  output.start(paper, { verify: source.verify });
  check.start(Boolean(source.verify));
  metrics.start(paper, config);
  if (isArxiv) mcp.setActive(true);

  const startedAt = Date.now();
  const timer = setInterval(() => {
    const seconds = (Date.now() - startedAt) / 1000;
    output.tick(seconds);
    metrics.tick(seconds);
  }, 1000);

  try {
    const body = new FormData();
    if (isArxiv) body.append("arxiv", source.ref);
    else body.append("file", source.file);
    body.append("verify", source.verify ? "true" : "false");
    const response = await fetch("/convert/stream", { method: "POST", body });
    if (!response.ok) throw new Error(await errorDetail(response));

    let finished = false;
    await readEvents(response, (name, data) => {
      if (name === "progress") {
        output.progress(data);
        metrics.progress(data);
        check.progress(data);
      } else if (name === "result") {
        finished = true;
        clearInterval(timer);
        output.done(data);
        metrics.done(data);
        download.done(data);
        check.done(data);
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
    check.fail(error.message);
    download.refresh();
  } finally {
    clearInterval(timer);
    upload.setBusy(false);
    if (isArxiv) mcp.setActive(false);
  }
}
