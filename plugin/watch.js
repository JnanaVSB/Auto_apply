// The watcher: once a fill's answers are out, it reads the form again and notices your press of Submit.
// It only listens: it presses, blocks and delays nothing. It loads before dropdown.js and page.js, so it uses them only
// when called.
const REREAD_MS = 5000;
const SECRET_BOXES = new Set(["current-password", "new-password", "one-time-code"]);
// The same as the app's
const SECRET_QUESTION = /password|passcode|\bpin\b/i;
const HOLDS_A_BOX = "input, select, textarea, label";

let watching = false;
let formRoot;
let lastKept;

// The attribute, as the property reads empty for words it doesn't know.
const isSecret = (box) =>
  (box.getAttribute("autocomplete") ?? "").toLowerCase().split(/\s+/).some((word) => SECRET_BOXES.has(word)) ||
  SECRET_QUESTION.test(question(box));

// A select on its placeholder is empty, and a ticked box with no words of its own is still ticked. A drop-down that
// can't say what it shows gives undefined.
function shownHere(key, list, root) {
  if (list[0].matches(DROPDOWN)) return shownPick(list[0], root);
  if (list[0].tagName === "SELECT" && !list[0].value) return "";
  const kind = kindOf(key, list[0]);
  if (!isGroup({ kind }, list)) return shownValue({ kind, value: "ticked" }, list, root);
  const ticked = list.filter((box) => box.checked).map((box) => ownLabel(box).trim() || box.value || "ticked");
  return kind === "multi" ? ticked : (ticked[0] ?? "");
}

function readForm(root) {
  const values = [];
  for (const [key, list] of boxes(root)) {
    if (list.some(isSecret)) continue;
    const value = shownHere(key, list, root);
    if (value !== undefined) values.push({ key, value: cut(value) });
  }
  return values;
}

// A press is only the board's named Submit, or, with none named, a real type=submit of this form. Words decide nothing,
// as a page chooses them. Where a board can't say, nothing is noticed: "I submitted it" still works, and fewer is safe.
// The attribute, not `.type`, which says "submit" for a <button> that never said so.
function isSubmit(node) {
  if (node.nodeType !== Node.ELEMENT_NODE) return false;
  if (BOARD.submit) return node.matches(BOARD.submit);
  const real = ["INPUT", "BUTTON"].includes(node.tagName) && node.getAttribute("type")?.toLowerCase() === "submit";
  return real && node.form === formRoot;
}

// An element wrapped round the boxes, or one you can't see, doesn't count. A page can still draw a real Submit of its
// own form on something else, and that can't be told.
function heard(event) {
  if (!event.isTrusted) return;
  const control = event.composedPath().find(isSubmit);
  if (!control || !formRoot?.contains(control)) return;
  if (!control.checkVisibility(SEEN) || control.querySelector(HOLDS_A_BOX)) return;
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
    if (location.href !== landedAt()) return;
    if (!(await ask({ do: "watch" }))?.on) return;
    await anchorShown();
    await quiet();
    formRoot = rootOf(anchored());
    document.addEventListener("click", heard, true);
    while (await look()) await wait(REREAD_MS);
  } finally {
    watching = false;
  }
}
