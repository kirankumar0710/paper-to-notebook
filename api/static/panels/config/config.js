// 1. Configuration: provider, model, API key status and save folder,
// from GET /api/config.
import { $, el } from "../../shared.js";

const badge = (kind, text) => el("span", { className: `badge ${kind}`,
  textContent: text });
const sub = (text, extra = "") => el("span", { className: `sub ${extra}`,
  textContent: text });

function render(config) {
  $("cfg-provider").replaceChildren(...(config.provider
    ? [el("span", { textContent: config.provider })]
    : [badge("bad", "Not set"), sub("export PROVIDER=anthropic (or gemini)")]));

  $("cfg-model").replaceChildren(
    el("span", { className: "mono", textContent: config.model || "—" }),
    config.base_url ? sub(config.base_url, "mono") : "");

  const key = config.api_key;
  $("cfg-key").replaceChildren(
    key.found ? badge("ok", "Found") : badge("bad", "Not found"),
    sub(key.found ? `from $${key.source}`
      : key.env_vars.length ? `export ${key.env_vars.join(" or ")}`
      : "select a provider first"));

  const dir = config.output_dir;
  const dirBadge = !dir.path ? badge("bad", "Not set")
    : !dir.writable ? badge("bad", "Not writable")
    : !dir.exists ? badge("warn", "Will be created")
    : badge("ok", "Ready");
  $("cfg-dir").replaceChildren(dirBadge, sub(dir.path || "—", "mono"),
    dir.source ? sub(dir.source === "default"
      ? "default · export NOTEBOOK_OUTPUT_DIR to change"
      : `from ${dir.source}`) : "");

  const options = Object.entries(config.options || {});
  $("cfg-options").textContent = options.length
    ? options.map(([k, v]) => `${k}=${v}`).join(" · ") : "—";

  const problems = $("cfg-problems");
  problems.replaceChildren(...config.problems.map(
    (text) => el("li", { textContent: text })));
  problems.hidden = !config.problems.length;
}

// Loads the configuration now and on Refresh; calls onLoad(config) each time.
export function initConfig({ onLoad }) {
  async function load() {
    $("cfg-provider").textContent = "Loading…";
    let config;
    try {
      config = await (await fetch("/api/config")).json();
    } catch (_) {
      $("cfg-provider").textContent = "Could not load configuration.";
      return;
    }
    render(config);
    onLoad(config);
  }
  $("config-refresh").addEventListener("click", load);
  load();
}
