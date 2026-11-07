// Custom drop-downs: a list the site draws itself, on a board whose file says how it shows a pick. The answer's words
// go into the drop-down's text box, which opens its list, and the one option with those whole words gets a press. It
// loads before page.js, so it uses page.js only when called.
const MENU_MS = 2000;
// At the start only: "Are you allowed to work in this country?" is a yes or no, and the site's own place lists aren't
const PLACE = /^\W*(?:country|location|city|state|province|region)\b/i;
// A click on an option runs these too, if the option is one or sits inside one in the page's plain tree
const PRESSES = "a, area, button, input, label, select, summary, textarea, [role=button], [role=link]";

// What it shows as picked, "" when it says nothing is, and undefined when it can't say: the text box holds typed words.
function shownPick(box) {
  const shape = BOARD.dropdown;
  const control = shape && box.closest(shape.control);
  if (!control) return undefined;
  const picked = control.querySelector(shape.picked);
  if (picked) return picked.textContent;
  return control.querySelector(shape.empty) ? "" : undefined;
}

// Only the list the box itself names, and a box naming none has no options.
function optionsOf(box) {
  const named = box.getAttribute("aria-controls");
  const list = named && document.getElementById(named);
  return list ? [...list.querySelectorAll("[role=option]")] : [];
}

async function menuShown(box) {
  const ends = Date.now() + MENU_MS;
  while (Date.now() < ends && !optionsOf(box).length) await wait(LOOK_MS);
}

// Some lists pick on one and some on the other. A site that wants a real press won't take this: the read-back says so.
function press(option) {
  for (const type of ["mousedown", "click"]) {
    option.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true }));
  }
}

// A click also runs through what an option is slotted into, and a closed part can't be read, so any shadow part counts.
function wouldPress(option) {
  if (option.closest(PRESSES)) return true;
  for (let node = option; node; node = node.parentElement) {
    if (chrome.dom.openOrClosedShadowRoot(node)) return true;
  }
  return false;
}

// Every option counts, fakes too, so a fake beside a real one is two alike. A stop found here is made by `fillLines`.
async function choose(line, box) {
  await menuShown(box);
  if (stopHere(landedAt(), box)) return "cant-set";
  const option = only(optionsOf(box), (found) => found.textContent, line.value);
  if (!option) return "no-choice";
  if (wouldPress(option) || !option.checkVisibility(SEEN)) return "cant-set";
  if (option.getAttribute("aria-disabled") === "true") return "cant-set";
  press(option);
  return undefined;
}

// Blur is what closes the list, as no key is ever raised. After a pick the box is the site's, so its text stays.
async function pick(line, box) {
  const shown = shownPick(box);
  const locked = closed(box) || box.getAttribute("aria-readonly") === "true";
  if (line.kind !== "single" || locked || shown === undefined) return "custom";
  if (PLACE.test(line.key) || PLACE.test(question(box))) return "place";
  if (same(line.value, shown)) return undefined;
  let why = "cant-set";
  box.focus();
  try {
    why = setText(box, line.value) ?? (await choose(line, box));
    return why;
  } finally {
    if (why) setText(box, "");
    box.blur();
  }
}
