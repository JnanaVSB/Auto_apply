// The page part: it reads, sets and reads back a form's boxes. It never sees the key or the ticket, sends no address and
// presses nothing but a tick box and a drop-down's option. The waits are for the site, not to look human.
const ANCHOR_MS = 10000;
const LOOK_MS = 200;
const QUIET_MS = 1500;
const MOST_MS = 15000;
const MAX_VALUE = 20000;
const CONTROL = /[\x00-\x08\x0b-\x1f\x7f]/g;
const SKIPPED = new Set(["hidden", "password", "submit", "button", "reset", "image"]);
const DROPDOWN = "input[role=combobox]";
const TEXT_BOX = 'textarea, input:not([type="radio"], [type="checkbox"], [type="file"])';
const TICK_BOX = 'input[type="radio"], input[type="checkbox"]';
const LIMIT = /application limit|limit on applications|maximum number of applications|too many applications/i;
const INVISIBLE_CHECK = /size=invisible|checkbox-invisible/;
const SEEN = { checkOpacity: true, checkVisibilityCSS: true };
const CSS_ONLY = { checkVisibilityCSS: true }; // a site can draw a drop-down over a see-through box

let running = false;

const ask = (message) => chrome.runtime.sendMessage(message);
const wait = (ms) => new Promise((done) => setTimeout(done, ms));
const tidy = (text) => text.replace(CONTROL, "").split(/[\s\x85]+/).filter(Boolean).join(" ").toLowerCase();
const tidied = (value) => [value].flat().map(tidy).sort();
const same = (one, other) => JSON.stringify(tidied(one)) === JSON.stringify(tidied(other));
const cut = (value) => (Array.isArray(value) ? value.map(cut) : value.slice(0, MAX_VALUE).toWellFormed());
const closed = (box) => box.matches(":disabled") || box.readOnly;
const ownLabel = (box) => box.labels?.[0]?.textContent ?? "";
const realOptions = (box) => [...box.options].filter((option) => option.value && option.text);
const isGroup = (line, list) => ["single", "multi"].includes(line.kind) && list.every((box) => box.matches(TICK_BOX));
const anchored = () => document.querySelector(BOARD.anchor);
// Where the page really loaded from: its own script can rewrite the address, on the same site, but not this.
const landedAt = () => performance.getEntriesByType("navigation")[0]?.name;
const rootOf = (anchor) => anchor?.closest("form") ?? (anchor && BOARD.formless ? document.body : undefined);

function only(items, textOf, label) {
  const wanted = tidy(label);
  const found = items.filter((item) => tidy(textOf(item)) === wanted);
  return found.length === 1 ? found[0] : undefined;
}

async function report(state, kind, values = [], left = []) {
  return ask({ do: "done", state, kind, values, left });
}

function quiet() {
  return new Promise((done) => {
    let timer;
    const end = () => {
      watcher.disconnect();
      clearTimeout(timer);
      clearTimeout(limit);
      done();
    };
    const again = () => {
      clearTimeout(timer);
      timer = setTimeout(end, QUIET_MS);
    };
    const watcher = new MutationObserver(again);
    const limit = setTimeout(end, MOST_MS);
    watcher.observe(document.documentElement, { subtree: true, childList: true, attributes: true, characterData: true });
    again();
  });
}

function loaded(path) {
  return new Promise((done) => {
    const end = () => {
      watcher.disconnect();
      clearTimeout(limit);
      done();
    };
    const watcher = new PerformanceObserver((list) => {
      if (list.getEntries().some((entry) => new URL(entry.name).pathname === path)) end();
    });
    const limit = setTimeout(end, MOST_MS);
    watcher.observe({ type: "resource" });
  });
}

function isCheckFrame(frame) {
  let where;
  try {
    where = new URL(frame.src);
  } catch {
    return false;
  }
  const { hostname, pathname } = where;
  const google = (hostname === "www.google.com" || hostname === "www.recaptcha.net") && pathname.startsWith("/recaptcha/");
  const others = hostname === "hcaptcha.com" || hostname.endsWith(".hcaptcha.com") || hostname === "challenges.cloudflare.com";
  if (!(google || others) || INVISIBLE_CHECK.test(frame.src) || !frame.checkVisibility(SEEN)) return false;
  const { width, height } = frame.getBoundingClientRect();
  return width >= 100 && height >= 60;
}

function botCheck() {
  const codes = [...document.querySelectorAll('input[autocomplete="one-time-code"]')];
  return [...document.querySelectorAll("iframe")].some(isCheckFrame) || codes.some((box) => box.checkVisibility(SEEN));
}

function stopHere(address, root) {
  if (location.href !== address || !root.isConnected) return "unexpected-page";
  return botCheck() ? "bot-check" : undefined;
}

async function anchorShown() {
  const ends = Date.now() + ANCHOR_MS;
  while (Date.now() < ends && !anchored() && !botCheck()) await wait(LOOK_MS);
}

const whyNoForm = (anchor) => (!anchor && LIMIT.test(document.body.innerText) ? "limit" : "unexpected-page");

function boxes(root) {
  const found = new Map();
  for (const box of root.querySelectorAll("input, select, textarea")) {
    const key = box.name || box.id;
    const shown = box.type === "file" || box.checkVisibility(box.matches(DROPDOWN) ? CSS_ONLY : SEEN);
    if (key && shown && !SKIPPED.has(box.type)) found.set(key, [...(found.get(key) ?? []), box]);
  }
  return found;
}

function questionPart(box) {
  const asked = BOARD.question && box.closest(BOARD.question)?.querySelector(BOARD.label);
  if (asked) return asked;
  const legend = box.matches(TICK_BOX) ? box.closest("fieldset")?.querySelector("legend") : undefined;
  return legend ?? box.labels?.[0];
}

const question = (box) => questionPart(box)?.textContent ?? box.getAttribute("aria-label") ?? "";

function isRequired(box) {
  const marked = questionPart(box)?.querySelector(".required");
  return box.required || box.getAttribute("aria-required") === "true" || Boolean(marked) || /[*✱]\s*$/.test(question(box));
}

function kindOf(key, box) {
  const fixed = BOARD.kindOf?.(key);
  if (fixed) return fixed;
  if (box.matches(DROPDOWN) || box.type === "radio") return "single";
  if (box.type === "checkbox") return "multi";
  if (box.tagName === "SELECT") return box.multiple ? "multi" : "single";
  if (box.tagName === "TEXTAREA") return "long_text";
  return box.type === "file" ? "file" : "text";
}

function choicesOf(kind, list) {
  const [box] = list;
  if (!["single", "multi"].includes(kind) || box.matches(DROPDOWN)) return [];
  if (box.tagName === "SELECT") return realOptions(box).map((option) => ({ label: option.text, value: option.value }));
  return list.map((choice) => ({ label: ownLabel(choice), value: choice.value }));
}

function fieldOf(key, list) {
  const [box] = list;
  const kind = kindOf(key, box);
  const accept = kind === "file" && box.accept ? box.accept.split(",").map((type) => type.trim()) : [];
  return { key, label: question(box), kind, required: list.some(isRequired), choices: choicesOf(kind, list), accept };
}

function raise(box) {
  box.dispatchEvent(new Event("input", { bubbles: true }));
  box.dispatchEvent(new Event("change", { bubbles: true }));
}

// React hears a tick through click, so setting `checked` wouldn't reach it.
function tick(box) {
  if (box.matches(TICK_BOX) && !box.checked) box.click();
}

function setText(box, value) {
  if (!box.matches(TEXT_BOX)) return "cant-set";
  const text = box.tagName === "TEXTAREA" ? value.replace(/\r\n/g, "\n") : value.replace(/\r\n|\r|\n/g, " ");
  box.value = text; // a page's own value tracking lives in its world, not this one, so `input` reaches it as a change
  raise(box);
  return undefined;
}

function setOptions(box, labels) {
  const options = labels.map((label) => only(realOptions(box), (option) => option.text, label));
  if (options.includes(undefined)) return "no-choice";
  for (const option of options) option.selected = true;
  raise(box);
  return undefined;
}

function setTicks(list, labels) {
  const chosen = labels.map((label) => only(list, ownLabel, label));
  if (chosen.includes(undefined)) return "no-choice";
  if (chosen.some(closed)) return "cant-set";
  chosen.forEach(tick);
  return undefined;
}

function setLine(line, list) {
  if (!list || (list.length > 1 && !isGroup(line, list))) return "no-box";
  const [box] = list;
  if (box.matches(DROPDOWN)) return pick(line, box);
  if (line.kind === "location") return "place";
  if (list.every(closed)) return "cant-set";
  if (line.kind === "text" || line.kind === "long_text") return setText(box, line.value);
  if (line.kind === "consent") return box.matches('input[type="checkbox"]') ? tick(box) : "cant-set";
  if (line.kind !== "single" && line.kind !== "multi") return "cant-set";
  if (box.tagName === "SELECT") return setOptions(box, [line.value].flat());
  const sort = line.kind === "multi" ? 'input[type="checkbox"]' : 'input[type="radio"]';
  return box.matches(sort) ? setTicks(list, [line.value].flat()) : "cant-set";
}

async function upload(line, root) {
  const list = boxes(root).get(line.key);
  if (list?.length !== 1 || !list[0].matches('input[type="file"]')) return { why: "no-box" };
  const [box] = list;
  if (closed(box)) return { why: "cant-set" };
  const reply = await ask({ do: "file", slot: line.value.slot });
  if (reply?.stop) return reply;
  if (!reply?.name) return { why: "no-file" };
  const bytes = Uint8Array.from(atob(reply.data), (character) => character.charCodeAt(0));
  const shelf = new DataTransfer();
  shelf.items.add(new File([bytes], reply.name, { type: "application/pdf" }));
  const parsed = BOARD.parse ? loaded(BOARD.parse) : undefined; // watching before the file goes in, or the call is missed
  box.files = shelf.files;
  raise(box);
  await parsed;
  await quiet();
  return { name: reply.name };
}

async function setOne(line, root) {
  try {
    if (line.kind === "file") return await upload(line, root);
    const why = await setLine(line, boxes(root).get(line.key));
    return why ? { why } : {};
  } catch {
    return { why: "cant-set" };
  }
}

function shownValue(line, list) {
  const [box] = list;
  if (box.matches(DROPDOWN)) return shownPick(box) ?? "";
  if (line.kind === "file") return box.files?.[0]?.name ?? "";
  if (line.kind === "consent") return box.checked ? line.value : "";
  if (box.matches(TEXT_BOX)) return box.value;
  const picked = box.tagName === "SELECT"
    ? [...box.selectedOptions].map((option) => option.text)
    : list.filter((choice) => choice.checked).map(ownLabel);
  return line.kind === "multi" ? picked : (picked[0] ?? "");
}

function readBack(done, left, root) {
  const found = boxes(root);
  const values = [];
  let stuck = 0;
  for (const { line, name } of done) {
    const list = found.get(line.key);
    const value = list ? shownValue(line, list) : "";
    values.push({ key: line.key, value: cut(value) });
    if (list && same(name ?? line.value, value)) stuck += 1;
    else left.push({ key: line.key, why: line.kind === "file" ? "file-refused" : "didnt-stick" });
  }
  return { values, stuck };
}

function stopAt(kind, waiting, done, left, root) {
  const { values } = readBack(done, left, root);
  const rest = waiting.map((line) => ({ key: line.key, why: "not-reached" }));
  return report("stopped", kind, values, [...left, ...rest]);
}

async function fillLines(pack, address, root) {
  const lines = [...pack.filter((line) => line.kind === "file"), ...pack.filter((line) => line.kind !== "file")];
  const done = [];
  const left = [];
  for (const [at, line] of lines.entries()) {
    const stop = stopHere(address, root);
    if (stop) return stopAt(stop, lines.slice(at), done, left, root);
    const result = await setOne(line, root);
    if (result.stop === "report") return stopAt("unexpected-page", lines.slice(at), done, left, root);
    if (result.stop) return undefined;
    if (result.why) left.push({ key: line.key, why: result.why });
    else done.push({ line, name: result.name });
  }
  await quiet();
  const late = stopHere(address, root);
  if (late) return stopAt(late, [], done, left, root);
  const { values, stuck } = readBack(done, left, root);
  return stuck > 0 ? report("filled", undefined, values, left) : report("stopped", "cant-set", values, left);
}

async function fillTheForm() {
  const address = landedAt();
  if (location.href !== address) return ask({ do: "moved" });
  if (!(await ask({ do: "open" }))?.go) return undefined;
  await anchorShown();
  await quiet(); // the page may still swap its form, so the root is taken after this and kept from here on
  const anchor = anchored();
  const root = rootOf(anchor);
  const stop = stopHere(address, root ?? document.body) ?? (root ? undefined : whyNoForm(anchor));
  if (stop) return report("stopped", stop);
  const fields = [...boxes(root)].map(([key, list]) => fieldOf(key, list));
  const reply = await ask({ do: "form", fields });
  if (reply?.pack) return fillLines(reply.pack, address, root);
  return reply?.stop === "report" ? report("stopped", "unexpected-page") : undefined;
}

async function run() {
  if (running) return;
  running = true;
  try {
    await fillTheForm();
  } catch {
    await report("stopped", "cant-set").catch(() => {});
  } finally {
    running = false;
  }
  await watch().catch(() => {});
}

chrome.runtime.onMessage.addListener((message, sender, reply) => {
  if (sender.id === chrome.runtime.id && message?.do === "begin") run();
  reply({ moved: location.href !== landedAt() });
});

run();
