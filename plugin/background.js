// The background part: the only part with the key, the ticket and the dashboard's address. It logs nothing.
const DASHBOARD = "http://127.0.0.1:8765";
const PROTOCOL = 1;
const TRIES = 6;
const WAIT_MS = 500;
const KEY_SHAPE = /^[A-Za-z0-9_-]{32,128}$/;
const JOB_PAGE = /^\/jobs\/\d+$/;
const TRUSTED = { accessLevel: "TRUSTED_CONTEXTS" };
const NO_DASHBOARD = { status: 0, body: { error: "no-dashboard" } };
const MANIFEST = chrome.runtime.getManifest();
const FORM_ORIGINS = new Set(MANIFEST.content_scripts.flatMap((script) => script.matches.map(originOf)));
const POPUP_PAGE = chrome.runtime.getURL("popup.html");
const OPTIONS_PAGE = chrome.runtime.getURL("options.html");
const READ_STATES = new Set(["reread", "clicked"]);
const MODES = new Set(["fill", "read"]);
// By tab, a press still on its way to the app, so that a tab leaving says so after it. This is only in memory: a worker
// that restarted has none, and then a thank-you address can be missed, which records nothing.
const pressing = new Map();

function originOf(address) {
  try {
    return new URL(address).origin;
  } catch {
    return "";
  }
}

const wait = (ms) => new Promise((done) => setTimeout(done, ms));
const storedKey = async () => (await chrome.storage.local.get("key")).key;
const recordName = (tabId) => `tab:${tabId}`;
const keep = (tabId, record) => chrome.storage.session.set({ [recordName(tabId)]: record });
const flag = (tabId, text) => chrome.action.setBadgeText({ tabId, text });
const inProgress = (record) => Boolean(record?.ticket) && !record.said?.state && !record.said?.problem;

// After a reload the form may be empty, but a check or a limit shows again, so its own words stay.
function afterReload(said) {
  const word = said?.kind ?? said?.problem;
  return { problem: ["bot-check", "limit"].includes(word) ? word : "reloaded" };
}

function saidHere(record, here) {
  if (!here) return undefined;
  if (record.said?.state) return afterReload(record.said);
  return record.said;
}

async function recordOf(tabId) {
  const name = recordName(tabId);
  return (await chrome.storage.session.get(name))[name];
}

async function call(route, body, key) {
  try {
    const answer = await fetch(`${DASHBOARD}/plugin/${route}`, {
      method: "POST",
      credentials: "omit",
      redirect: "error",
      cache: "no-store",
      headers: { Authorization: `Bearer ${key}`, "Content-Type": "application/json" },
      body: JSON.stringify({ ...body, protocol: PROTOCOL }),
    });
    return { status: answer.status, body: await answer.json() };
  } catch {
    return NO_DASHBOARD;
  }
}

async function activeTab() {
  const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  return tab;
}

async function onForm(tabId, record) {
  const tab = await chrome.tabs.get(tabId);
  return tab.url === record.address;
}

// The press and the tab race each other, so the ticket may not exist yet.
async function askForTicket(address, key) {
  for (let tries = 1; ; tries += 1) {
    const answer = await call("start", { address }, key);
    if (answer.body.error !== "no-ticket" || tries === TRIES) return answer;
    await wait(WAIT_MS);
  }
}

async function open(message, sender) {
  const tabId = sender.tab.id;
  const address = sender.tab.url;
  const record = await recordOf(tabId);
  const key = await storedKey();
  const here = record?.address === address;
  if (record?.ticket && here && record.answered) {
    await keep(tabId, { ...record, said: afterReload(record.said) });
    return { go: false };
  }
  if (record?.ticket && here) {
    // `said: {}` is what inProgress reads: the fill is running again
    await keep(tabId, { ...record, said: {} });
    return { go: true };
  }
  if (record?.ticket) {
    await call("report", { ticket: record.ticket, state: "stopped", kind: "unexpected-page", values: [], left: [] }, key);
  }
  const said = saidHere(record, here);
  await keep(tabId, { address, said });
  if (!key) return { go: false };
  const answer = await askForTicket(address, key);
  if (answer.status === 200 && MODES.has(answer.body.mode) && typeof answer.body.ticket === "string") {
    const { ticket, mode } = answer.body;
    const thanks = answer.body.thanks ?? null;
    await keep(tabId, { ticket, mode, address, slots: [], answered: false, thanks, said: {} });
    return { go: true };
  }
  const problem = answer.body.error ?? "app-error";
  await keep(tabId, { address, said: problem === "no-ticket" && said ? said : { problem } });
  return { go: false };
}

async function form(message, sender) {
  const tabId = sender.tab.id;
  const record = await recordOf(tabId);
  if (!record?.ticket || !Array.isArray(message.fields)) return { stop: "known" };
  if (!(await onForm(tabId, record))) return { stop: "report" };
  const answer = await call("form", { ticket: record.ticket, fields: message.fields }, await storedKey());
  const { go, pack, reason, error } = answer.body;
  if (answer.status === 200 && go && pack.length > 0) {
    const slots = pack.filter((line) => line.kind === "file").map((line) => line.value.slot);
    await keep(tabId, { ...record, answered: true, slots });
    return { pack };
  }
  if (answer.status === 200 && reason === "read") {
    await flag(tabId, "OK");
    await keep(tabId, { address: record.address, said: { problem: "read" } });
    return { stop: "known" };
  }
  if (answer.status === 400 || answer.status === 413) return { stop: "report" }; // it reports this, so the row isn't left open
  await flag(tabId, "!");
  if (error === "not-logged-in" || answer.status === 0 || answer.status >= 500) {
    await keep(tabId, { ...record, said: { problem: error ?? "app-error" } });
    return { stop: "wait" };
  }
  const problem = answer.status === 200 ? (reason ?? "nothing-to-fill") : (error ?? "app-error");
  await keep(tabId, { address: record.address, said: { problem } });
  return { stop: "known" };
}

async function file(message, sender) {
  const tabId = sender.tab.id;
  const record = await recordOf(tabId);
  if (!record?.ticket || !record.slots.includes(message.slot)) return { why: "no-file" };
  if (!(await onForm(tabId, record))) return { stop: "report" };
  const answer = await call("file", { ticket: record.ticket, slot: message.slot }, await storedKey());
  const { name, data, error } = answer.body;
  if (answer.status === 200 && typeof name === "string" && typeof data === "string") return { name, data };
  if (error !== "no-ticket") return { why: "no-file" };
  await flag(tabId, "!");
  await keep(tabId, { address: record.address, said: { problem: "ended" } });
  return { stop: "known" };
}

async function done(message, sender) {
  const tabId = sender.tab.id;
  const record = await recordOf(tabId);
  if (!record?.ticket) return {};
  const { state, kind, values, left } = message;
  const body = { ticket: record.ticket, state, kind, values, left };
  const answer = await call("report", body, await storedKey());
  const unfilled = new Set(left.map((line) => line.key));
  const filled = new Set(values.map((line) => line.key).filter((key) => !unfilled.has(key))).size;
  const said = { state, kind, filled, left: unfilled.size };
  await flag(tabId, state === "filled" && answer.status === 200 ? "OK" : "!");
  const ended = state === "stopped" && answer.status === 200;
  await keep(tabId, ended ? { address: record.address, said } : { ...record, said });
  return {};
}

async function watch(message, sender) {
  const tabId = sender.tab.id;
  const record = await recordOf(tabId);
  const on = Boolean(record?.ticket && record.answered) && (await onForm(tabId, record));
  const thanks = on && record.thanks?.selector ? record.thanks : undefined;
  return { on, thanks };
}

function read(message, sender) {
  const going = sendRead(message, sender);
  if (message.state !== "clicked") return going;
  const tabId = sender.tab.id;
  const ended = going.catch(() => {}).finally(() => {
    if (pressing.get(tabId) === ended) pressing.delete(tabId);
  });
  pressing.set(tabId, ended);
  return going;
}

async function sendRead(message, sender) {
  const tabId = sender.tab.id;
  const record = await recordOf(tabId);
  const { state, values } = message;
  if (!record?.ticket || !READ_STATES.has(state) || !Array.isArray(values)) return { stop: true };
  // The tab may already be leaving after a press, and the address it happened on is what counts.
  const here = state === "clicked" ? sender.url === record.address : await onForm(tabId, record);
  if (!here) return { stop: true };
  const answer = await call("report", { ticket: record.ticket, state, values, left: [] }, await storedKey());
  const latest = await recordOf(tabId); // the tab can leave during the call, and an old copy would bring its ticket back
  if (latest?.ticket !== record.ticket) return { stop: true };
  if (answer.body.error === "no-ticket") {
    await keep(tabId, { address: latest.address, said: latest.said });
    return { stop: true };
  }
  if (answer.status !== 200) {
    if (state === "reread") await keep(tabId, { ...latest, said: { ...latest.said, unsaved: true } });
    return {};
  }
  const looks = state === "clicked" ? { looks: true } : {};
  const pressed = state === "clicked" ? { clickedAt: Date.now() } : {};
  await keep(tabId, { ...latest, ...pressed, said: { ...latest.said, unsaved: false, ...looks } });
  return { kept: true };
}

async function saidThanks(tabId, record) {
  const body = { ticket: record.ticket, state: "thanked", values: [], left: [] };
  const answer = await call("report", body, await storedKey());
  if (answer.body.recorded !== true) return;
  await flag(tabId, "OK");
  const latest = await recordOf(tabId);
  if (latest) await keep(tabId, { ...latest, said: { ...latest.said, recorded: true } });
}

async function thanked(message, sender) {
  const tabId = sender.tab.id;
  const record = await recordOf(tabId);
  if (!record?.ticket || !record.thanks?.selector || !record.clickedAt || !(await onForm(tabId, record))) return {};
  await saidThanks(tabId, record);
  return {};
}

function atThanksPage(record, tab) {
  if (!record.thanks?.path || !record.clickedAt) return false;
  return originOf(tab.url) === originOf(record.address) && new URL(tab.url).pathname.endsWith(record.thanks.path);
}

async function leftForm(tabId) {
  const record = await recordOf(tabId);
  if (record?.ticket) await call("report", { ticket: record.ticket, state: "gone", values: [], left: [] }, await storedKey());
}

async function moved(message, sender) {
  const tabId = sender.tab.id;
  const record = await recordOf(tabId);
  await flag(tabId, "!");
  await keep(tabId, { ...record, address: record?.address ?? sender.tab.url, said: { problem: "moved" } });
  return {};
}

async function status() {
  const tab = await activeTab();
  const key = await storedKey();
  if (!key) return { say: "not-connected" };
  if (!FORM_ORIGINS.has(originOf(tab?.url))) return { say: "no-form" };
  const record = await recordOf(tab.id);
  const said = record?.said;
  const answer = await call("known", { address: tab.url }, key);
  if (answer.status !== 200) return { say: answer.body.error ?? "app-error", said };
  if (!answer.body.job) return { say: "no-job", said };
  const going = record?.mode === "read" ? "reading" : "filling";
  return { say: inProgress(record) ? going : "known", title: answer.body.title, company: answer.body.company, said };
}

async function fill() {
  const tab = await activeTab();
  const key = await storedKey();
  if (!key) return { say: "not-connected" };
  if (!FORM_ORIGINS.has(originOf(tab?.url))) return { say: "no-form" };
  const record = await recordOf(tab.id);
  if (inProgress(record)) return { say: "filling" };
  const answer = await call("fill", { address: tab.url }, key);
  if (answer.status === 409 && answer.body.error === "not-ready") {
    return { say: "not-ready", link: JOB_PAGE.test(answer.body.job) ? `${DASHBOARD}${answer.body.job}` : undefined };
  }
  if (answer.body.ok !== true) return { say: answer.body.error ?? "app-error" };
  if (record?.ticket) await keep(tab.id, { address: record.address, said: record.said }); // the press ended that fill
  try {
    const reply = await chrome.tabs.sendMessage(tab.id, { do: "begin" }, { frameId: 0 });
    return { say: reply?.moved ? "moved" : "filling" };
  } catch {
    return { say: "reload" };
  }
}

async function pair(message) {
  const { key } = message;
  if (typeof key !== "string" || !KEY_SHAPE.test(key)) return { say: "bad-key" };
  try {
    await chrome.storage.local.setAccessLevel(TRUSTED); // before the key is saved: until then a page's part could read it
  } catch {
    return { say: "storage" };
  }
  const answer = await call("hello", { version: MANIFEST.version }, key);
  if (answer.status !== 200 || answer.body.app !== "auto_apply" || answer.body.protocol !== PROTOCOL) {
    return { say: answer.body.error ?? "app-error" };
  }
  try {
    await chrome.storage.local.set({ key });
    if ((await storedKey()) !== key) return { say: "storage" };
    return { say: "paired" };
  } catch {
    return { say: "storage" };
  }
}

async function paired() {
  return { say: (await storedKey()) ? "paired" : "not-connected" };
}

// Maps, not objects: `do` comes from outside, and "constructor" must find nothing.
const PAGE = new Map([
  ["open", open], ["form", form], ["file", file], ["done", done], ["moved", moved], ["watch", watch], ["read", read],
  ["thanked", thanked],
]);
const POPUP = new Map([["status", status], ["fill", fill]]);
const OPTIONS = new Map([["pair", pair], ["paired", paired]]);

function handlerFor(message, sender) {
  if (sender.id !== chrome.runtime.id) return undefined;
  if (sender.url === POPUP_PAGE) return POPUP.get(message?.do);
  if (sender.url === OPTIONS_PAGE) return OPTIONS.get(message?.do);
  if (sender.frameId === 0 && sender.tab?.url && FORM_ORIGINS.has(sender.origin)) return PAGE.get(message?.do);
  return undefined;
}

chrome.runtime.onMessage.addListener((message, sender, reply) => {
  const handler = handlerFor(message, sender);
  if (!handler) return false;
  handler(message, sender).then(reply, () => reply({}));
  return true;
});

chrome.tabs.onRemoved.addListener(async (tabId) => {
  await pressing.get(tabId);
  await leftForm(tabId);
  await chrome.storage.session.remove(recordName(tabId));
});

// A reload has the same address, so it isn't leaving. Off the form hosts the tab's address can't be seen, so a new status
// is what tells: `tab.url` is then empty.
chrome.tabs.onUpdated.addListener(async (tabId, change, tab) => {
  if (!change.url && !change.status) return;
  await pressing.get(tabId);
  const record = await recordOf(tabId);
  if (!record?.ticket || tab.url === record.address) return;
  if (atThanksPage(record, tab)) await saidThanks(tabId, record);
  await leftForm(tabId);
  const latest = await recordOf(tabId); // it may have landed on another form with a new ticket during that call
  if (latest?.ticket === record.ticket) await keep(tabId, { address: latest.address, said: latest.said });
});

chrome.runtime.onInstalled.addListener(async () => {
  const key = await storedKey();
  if (key) await call("hello", { version: MANIFEST.version }, key);
});

chrome.storage.local.setAccessLevel(TRUSTED).catch(() => {});
