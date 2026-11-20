// 2. Upload: choose or drop a PDF and start the conversion.
import { $, fmtBytes } from "../../shared.js";

// onChoose(file) runs when a file is picked; onSubmit(file) on Convert.
export function initUpload({ onChoose, onSubmit }) {
  const input = $("file"), drop = $("drop"), submit = $("submit");
  let maxMb = 15;
  let busy = false;

  function setStatus(text, kind) {
    $("status").textContent = text;
    $("status").className = kind || "";
  }

  function showFile() {
    const file = input.files[0];
    $("file-name").textContent = file
      ? `${file.name} · ${fmtBytes(file.size)}`
      : `No file chosen (max ${maxMb} MB)`;
    setStatus("");
    const tooBig = Boolean(file) && file.size > maxMb * 1024 * 1024;
    if (tooBig) {
      setStatus(`This file is larger than the ${maxMb} MB limit.`, "error");
    }
    submit.disabled = busy || !file || tooBig;
    if (file) onChoose(file);
  }

  input.addEventListener("change", showFile);
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
    if (input.files[0]) onSubmit(input.files[0]);
  });

  return {
    setStatus,
    setMaxMb(mb) {
      maxMb = mb;
      if (!input.files[0]) showFile();
    },
    setBusy(value) {
      busy = value;
      submit.disabled = busy || !input.files[0];
    },
  };
}
