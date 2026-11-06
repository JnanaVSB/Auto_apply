// The watcher: once a fill's answers are out, it reads the form again and notices your press of Submit.
// It only listens: it presses, blocks and delays nothing. It loads before page.js, so it uses page.js only when called.
const REREAD_MS = 5000;
const SEND_WORDS = /^(submit|send)( my| your)?( job)?( application)?$/;
const SECRET_BOXES = new Set(["current-password", "new-password", "one-time-code"]);
const SECRET_QUESTION = /password/i;
const HOLDS_A_BOX = "input, select, textarea, label";
const THANKS_LOOKS = 60;
const THANKS_MS = 1000;

let watching = false;
let formRoot;
let lastKept;
let thanksSign; // the app's own words for this site, never a page's
let lookingForThanks = false;

const isControl = (node) =>
  node.tagName === "BUTTON" ||
  (node.tagName === "INPUT" && ["submit", "button"].includes(node.type)) ||
  node.getAttribute?.("role") === "button";
// Its whole words, as a person sees them. Other words aren't noticed, which is the safe side.
const saysSubmit = (control) =>
  [control.textContent, control.value].some((words) => SEND_WORDS.test(tidy(words ?? "")));

const isSecret = (box) =>
  box.autocomplete.split(/\s+/).some((word) => SECRET_BOXES.has(word)) || SECRET_QUESTION.test(question(box));

// A select on its placeholder is empty, and a ticked box with no words of its own is still ticked.
function shownHere(key, list) {
  if (list[0].tagName === "SELECT" && !list[0].value) return "";
  const kind = kindOf(key, list[0]);
  if (!isGroup({ kind }, list)) return shownValue({ key, kind, value: "ticked" }, list);
  const ticked = list.filter((box) => box.checked).map((box) => ownLabel(box).trim() || box.value || "ticked");
  return kind === "multi" ? ticked : (ticked[0] ?? "");
}

// A custom drop-down's text box doesn't hold the pick, so "empty" would be false.
function readForm(root) {
  const values = [];
  for (const [key, list] of boxes(root)) {
    if (list.some((box) => isSecret(box) || box.matches(DROPDOWN))) continue;
    values.push({ key, value: cut(shownHere(key, list)) });
  }
  return values;
}

// A page chooses what its controls say, so an element wrapped round the boxes, or one you can't see, doesn't count.
// This can't stop a page that draws a real button with Submit's words on something else.
function heard(event) {
  if (!event.isTrusted) return;
  const control = event.composedPath().find(isControl);
  if (!control || !formRoot?.contains(control) || !saysSubmit(control)) return;
  if (!control.checkVisibility(SEEN) || control.querySelector(HOLDS_A_BOX)) return;
  const saved = ask({ do: "read", state: "clicked", values: readForm(formRoot) });
  const already = shownBeforePress();
  saved.then((reply) => (already ? undefined : thanksAfter(reply))).catch(() => {});
}

const thanksShown = () =>
  [...document.querySelectorAll(thanksSign.selector)].some((element) =>
    tidy(element.textContent).includes(tidy(thanksSign.words)),
  );

// A sign on the page before the press says nothing of it. A bad selector counts as shown, so it is never looked for.
function shownBeforePress() {
  try {
    return thanksShown();
  } catch {
    return true;
  }
}

async function thanksAfter(reply) {
  if (!reply?.kept || !thanksSign || lookingForThanks) return;
  lookingForThanks = true;
  try {
    for (let looks = 0; looks < THANKS_LOOKS; looks += 1) {
      if (thanksShown()) {
        await ask({ do: "thanked" });
        return;
      }
      await wait(THANKS_MS);
    }
  } finally {
    lookingForThanks = false;
  }
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
    const told = await ask({ do: "watch" });
    if (!told?.on) return;
    thanksSign = told.thanks;
    await anchorShown();
    await quiet();
    formRoot = rootOf(anchored());
    document.addEventListener("click", heard, true);
    while (await look()) await wait(REREAD_MS);
  } finally {
    watching = false;
  }
}
