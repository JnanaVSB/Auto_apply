// The watcher: once a fill's answers are out, it reads the form again and notices your press of Submit.
// It only listens: it presses, blocks and delays nothing. It loads before page.js, so it uses page.js only when called.
const REREAD_MS = 5000;
const SEND_WORDS = /\bsubmit\b|\bsend\b.*\bapplication\b/i;
const SECRET_BOXES = new Set(["current-password", "new-password", "one-time-code"]);

let watching = false;
let formRoot;
let lastKept;

const isControl = (node) =>
  ["BUTTON", "A"].includes(node.tagName) ||
  (node.tagName === "INPUT" && ["submit", "button"].includes(node.type)) ||
  node.getAttribute?.("role") === "button";
const wordsOf = (control) => tidy([control.textContent, control.value, control.getAttribute("aria-label")].join(" "));

// A custom drop-down's text box doesn't hold the pick, so "empty" would be false.
function readForm(root) {
  const values = [];
  for (const [key, list] of boxes(root)) {
    if (list.some((box) => SECRET_BOXES.has(box.autocomplete) || box.matches(DROPDOWN))) continue;
    values.push({ key, value: cut(shownValue({ key, kind: kindOf(key, list[0]), value: "ticked" }, list)) });
  }
  return values;
}

function heard(event) {
  if (!event.isTrusted) return;
  const control = event.composedPath().find(isControl);
  if (!control || !formRoot?.contains(control) || !SEND_WORDS.test(wordsOf(control))) return;
  ask({ do: "read", state: "clicked", values: readForm(formRoot) }).catch(() => {});
}

// Only what the app accepted is remembered, so a read it couldn't save goes again next time.
async function look() {
  if (location.href !== landedAt()) return false;
  if (!formRoot?.isConnected) formRoot = rootOf(anchored());
  if (!formRoot) return true;
  const values = readForm(formRoot);
  const sent = JSON.stringify(values);
  if (sent === lastKept) return true;
  const reply = await ask({ do: "read", state: "reread", values });
  if (reply?.stop) return false;
  if (reply?.kept) lastKept = sent;
  return true;
}

async function watch() {
  lastKept = undefined; // a new fill's row holds no read, even if a watcher is still running from the last one
  if (watching) return;
  watching = true;
  try {
    if (location.href !== landedAt() || !(await ask({ do: "watch" }))?.on) return;
    await anchorShown();
    await quiet();
    formRoot = rootOf(anchored());
    document.addEventListener("click", heard, true);
    while (await look()) await wait(REREAD_MS);
  } finally {
    watching = false;
  }
}
