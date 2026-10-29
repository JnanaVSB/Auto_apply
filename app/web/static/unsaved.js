// The browser's "Leave page?" warning for a form marked data-unsaved, once something in it changed and wasn't saved.
for (const form of document.querySelectorAll("form[data-unsaved]")) {
  let changed = false;
  form.addEventListener("input", () => { changed = true; });
  form.addEventListener("submit", () => { changed = false; });
  window.addEventListener("beforeunload", (event) => { if (changed) event.preventDefault(); });
}
