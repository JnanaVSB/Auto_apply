// Custom drop-downs: a list the site draws itself, on a board whose file says how it shows a pick. The answer's words
// go into the drop-down's text box, which opens its list, and the one option with those whole words gets a press. It
// loads before page.js, so it uses page.js only when called.
const MENU_MS = 2000;
// At the start only: "Are you allowed to work in this country?" is a yes or no; a question that starts with a place is
// the site's long list, so it's left
const PLACE = /^\W*(?:country|location|city|state|province|region)\b/i;
// A click on an option runs these too, if the option is one or sits inside one in the page's plain tree
const PRESSES = "a, area, button, input, label, select, summary, textarea, [role=button], [role=link]";

// What it shows as picked, "" when it says nothing is, and undefined when it can't say: the text box holds typed words.
// The control must be this box's own, inside the form: a part round the form or round other boxes isn't one drop-down.
function shownPick(box, root) {
  const shape = BOARD.dropdown;
  const control = shape && box.closest(shape.control);
  const own = control && control !== root && root.contains(control);
  if (!own || control.querySelectorAll("input, select, textarea").length !== 1) return undefined;
  const picked = control.querySelector(shape.picked);
  if (picked) return picked.textContent;
  return control.querySelector(shape.empty) ? "" : undefined;
}

// Only the list the box itself names, inside the form and apart from the box. A box naming none has no options.
function optionsOf(box, root) {
  const named = box.getAttribute("aria-controls");
  const list = named && document.getElementById(named);
  const apart = list && list !== root && root.contains(list) && !list.contains(box);
  return apart ? [...list.querySelectorAll("[role=option]")] : [];
}

async function menuShown(box, root) {
  const ends = Date.now() + MENU_MS;
  while (Date.now() < ends && !optionsOf(box, root).length) await wait(LOOK_MS);
}

// A click also runs through what an option is slotted into, and a closed part can't be read, so any shadow part counts.
function wouldPress(option) {
  if (option.closest(PRESSES)) return true;
  for (let node = option; node; node = node.parentElement) {
    if (chrome.dom.openOrClosedShadowRoot(node)) return true;
  }
  return false;
}

function allowed(option, box, root) {
  if (!option.isConnected || !optionsOf(box, root).includes(option) || wouldPress(option)) return false;
  return option.checkVisibility(SEEN) && option.getAttribute("aria-disabled") !== "true";
}

// Some lists pick on one and some on the other. Both go to the option itself, at no point on the screen, so nothing
// drawn over it can take the click, and an option below the screen's edge is still reached. The page's own mousedown
// can move the option, into a button or a link, so it's asked again before the click. A site that wants a real press
// won't take this: the read-back says so.
function press(option, box, root) {
  option.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, cancelable: true }));
  if (!allowed(option, box, root)) return false;
  option.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
  return true;
}

// Every option counts, fakes too, so a fake beside a real one is two alike. A stop seen here leaves this field;
// `fillLines` then makes the stop.
async function choose(line, box, root) {
  await menuShown(box, root);
  if (stopHere(landedAt(), box)) return "cant-set";
  const option = only(optionsOf(box, root), (found) => found.textContent, line.value);
  if (!option) return "no-choice";
  if (!allowed(option, box, root) || !press(option, box, root)) return "cant-set";
  return undefined;
}

// Blur is what closes the list, as no key is ever raised. After a pick the box is the site's, so its text stays.
async function pick(line, box, root) {
  const shown = shownPick(box, root);
  const locked = closed(box) || box.getAttribute("aria-readonly") === "true";
  if (line.kind !== "single" || locked || shown === undefined) return "custom";
  if (PLACE.test(line.key) || PLACE.test(question(box))) return "place";
  if (same(line.value, shown)) return undefined;
  let why = "cant-set"; // until set, so an exception still clears the words
  box.focus();
  try {
    why = setText(box, line.value) ?? (await choose(line, box, root));
    return why;
  } finally {
    if (why) setText(box, "");
    box.blur();
  }
}
