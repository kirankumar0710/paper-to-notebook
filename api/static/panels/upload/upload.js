// 2. Upload paper / specify link: choose or drop a PDF, or paste an arXiv
// link or ID, and start the conversion. Using one clears the other.
import { $, fmtBytes } from "../../shared.js";

// The same forms the server accepts (sources/arxiv.py): an ID such as
// 1706.03762 or hep-th/9901001, "arXiv:<id>", or an arxiv.org link.
const NEW_ID = String.raw`\d{4}\.\d{4,5}(?:v\d+)?`;
const OLD_ID = String.raw`[a-z]+(?:-[a-z]+)*(?:\.[A-Z]{2})?/\d{7}(?:v\d+)?`;
const ARXIV_REF = new RegExp(String.raw`^(?:arxiv:\s*)?(?:(?:https?://)?` +
  String.raw`(?:www\.|export\.)?arxiv\.org/(?:abs|pdf|html)/)?` +
  `(?:${NEW_ID}|${OLD_ID})(?:\\.pdf)?/?$`, "i");

export const looksLikeArxiv = (text) => ARXIV_REF.test(text.trim());

// What each notebook depth means, shown under the depth switch.
const DEPTH_HINTS = {
  beginner: "Explains the background and every equation step by step, " +
    "with short, well-commented code.",
  practitioner: "Balanced: explains the key ideas, then implements the " +
    "method.",
  expert: "Brief notes on what is new; a fuller, faithful implementation.",
};
const DEPTH_KEY = "p2n.depth";  // remembered in this browser

// onChoose(file) runs when a file is picked. onSubmit(source) on Convert,
// with {kind: "file", file} or {kind: "arxiv", ref}, plus verify: whether
// to check that the notebook runs, and depth: "beginner", "practitioner"
// or "expert".
export function initUpload({ onChoose, onSubmit }) {
  const input = $("file"), drop = $("drop"), submit = $("submit");
  const arxiv = $("arxiv"), verify = $("verify");
  const verifyHint = $("verify-hint").innerHTML;
  const depthInputs = [...document.querySelectorAll("input[name=depth]")];

  const depth = () =>
    (depthInputs.find((i) => i.checked) || {}).value || "practitioner";
  function showDepth() {
    $("depth-hint").textContent = DEPTH_HINTS[depth()];
  }
  try {
    const saved = localStorage.getItem(DEPTH_KEY);
    const input = depthInputs.find((i) => i.value === saved);
    if (input) input.checked = true;
  } catch (_) { /* storage unavailable: keep the default */ }
  showDepth();
  depthInputs.forEach((input) => input.addEventListener("change", () => {
    showDepth();
    try { localStorage.setItem(DEPTH_KEY, depth()); }
    catch (_) { /* not remembered; fine */ }
  }));
  let maxMb = 15;
  let busy = false;

  function setStatus(text, kind) {
    $("status").textContent = text;
    $("status").className = kind || "";
  }

  // What Convert would send, or null if nothing valid is entered.
  function current() {
    const file = input.files[0];
    if (file) {
      return file.size > maxMb * 1024 * 1024 ? null : { kind: "file", file };
    }
    const ref = arxiv.value.trim();
    return ref && looksLikeArxiv(ref) ? { kind: "arxiv", ref } : null;
  }

  function update() {
    submit.disabled = busy || !current();
  }

  function showFile() {
    const file = input.files[0];
    $("file-name").textContent = file
      ? `${file.name} · ${fmtBytes(file.size)}`
      : `No file chosen (max ${maxMb} MB)`;
    setStatus("");
    if (file) {
      arxiv.value = "";
      showArxivHint();
      if (file.size > maxMb * 1024 * 1024) {
        setStatus(`This file is larger than the ${maxMb} MB limit.`,
          "error");
      }
      onChoose(file);
    }
    update();
  }

  function showArxivHint() {
    const ref = arxiv.value.trim();
    const bad = Boolean(ref) && !looksLikeArxiv(ref);
    $("arxiv-hint").textContent = bad
      ? "Use an arXiv link like https://arxiv.org/abs/1706.03762, " +
        "or just the ID."
      : "Looked up with the arXiv MCP server; see the MCP log tab.";
    $("arxiv-hint").classList.toggle("error", bad);
    arxiv.setAttribute("aria-invalid", String(bad));
  }

  input.addEventListener("change", showFile);
  arxiv.addEventListener("input", () => {
    if (arxiv.value.trim() && input.files.length) {
      input.value = "";   // a link replaces the chosen file
      showFile();
    }
    setStatus("");
    showArxivHint();
    update();
  });
  ["dragenter", "dragover"].forEach((type) =>
    drop.addEventListener(type, (event) => {
      event.preventDefault();
      drop.classList.add("dragging");
    }));
  ["dragleave", "drop"].forEach((type) =>
    drop.addEventListener(type, () => drop.classList.remove("dragging")));
  drop.addEventListener("drop", (event) => {
    event.preventDefault();
    if (event.dataTransfer.files.length) {
      input.files = event.dataTransfer.files;
      showFile();
    }
  });
  $("form").addEventListener("submit", (event) => {
    event.preventDefault();
    const source = current();
    if (source) {
      onSubmit({ ...source, verify: verify.checked, depth: depth() });
    }
  });

  return {
    setStatus,
    // options: the "verify" part of GET /api/config.
    setVerify(options) {
      const available = Boolean(options && options.available);
      verify.disabled = !available;
      verify.checked = available && Boolean(options.default);
      $("verify-row").classList.toggle("disabled", !available);
      if (available) $("verify-hint").innerHTML = verifyHint;
      else {
        $("verify-hint").textContent = (options && options.reason) ||
          "Not available on this server.";
      }
    },

    setMaxMb(mb) {
      maxMb = mb;
      if (!input.files[0]) showFile();
    },
    setBusy(value) {
      busy = value;
      depthInputs.forEach((input) => { input.disabled = value; });
      update();
    },
  };
}
