// The popup: it asks the background part and draws the answer. Titles and companies are outside text: textContent only.
const WORDS = new Map([
  ["not-connected", "Not connected yet: paste the key from Settings → Browser plugin."],
  ["no-dashboard", "Can't reach the dashboard: start it; this plugin only talks to port 8765."],
  ["not-logged-in", "Log in to the dashboard first, then reload this tab: the same fill still works."],
  ["no-key", "The key no longer works: add this browser again in Settings → Browser plugin."],
  ["wrong-plugin", "The key belongs to another plugin: remove this browser in Settings → Browser plugin and add it again."],
  ["old-plugin", "This plugin is too old for the dashboard: update it."],
  ["no-job", "This page isn't the form of one of your jobs."],
  ["no-form", "This tab isn't a job's form."],
  ["not-ready", "This job can't be filled yet: answer its open decisions, or see why on its page."],
  ["no-ticket", "No fill was waiting for this tab. If you pressed Open and fill, it may have gone to another tab or browser: check Settings → Browser plugin."],
  ["reload", "Reload this tab, then press Fill again."],
  ["filling", "Filling this tab…"],
  ["known", ""],
  ["honest", "Filling shares these answers and your PDF with the site, even before you press Submit. Other plugins in this browser can read them too."],
  ["app-error", "Something went wrong. Try again."],
  ["unexpected-page", "This page didn't look like the job's form, so the fill stopped: check the tab."],
  ["nothing-to-fill", "None of your answers could go on this form."],
  ["bot-check", "The site wants to check you are a person: finish it in the open tab."],
  ["limit", "The site says you've reached its limit on applications."],
  ["cant-set", "The form wouldn't let the plugin set any box: fill it in yourself."],
]);

const part = (id) => document.getElementById(id);

function show(id, text) {
  part(id).textContent = text;
  part(id).hidden = false;
}

const words = (kind) => WORDS.get(kind) ?? WORDS.get("app-error");
const counted = (said) => `Filled ${said.filled}, left ${said.left} for you.`;

function saidWords(said) {
  if (said.state === "filled") return `${counted(said)} Check the form, then press Submit yourself.`;
  if (said.state === "stopped") return `${words(said.kind)} ${counted(said)}`;
  return said.problem ? words(said.problem) : "";
}

function draw(answer) {
  part("say").textContent = words(answer?.say);
  part("fill").hidden = answer?.say !== "known";
  part("honest").hidden = answer?.say !== "known";
  part("connect").hidden = answer?.say !== "not-connected";
  part("job").hidden = !answer?.link;
  if (answer?.link) part("job").href = answer.link;
  if (answer?.say === "known") {
    show("title", answer.title);
    show("company", answer.company);
    show("honest", words("honest"));
  }
  const said = answer?.said && answer.said.problem !== answer.say ? saidWords(answer.said) : "";
  part("said").textContent = said;
  part("said").hidden = !said;
}

part("fill").addEventListener("click", async () => {
  part("fill").disabled = true;
  draw(await chrome.runtime.sendMessage({ do: "fill" }));
  part("fill").disabled = false;
});
part("connect").addEventListener("click", () => chrome.runtime.openOptionsPage());

chrome.runtime.sendMessage({ do: "status" }).then(draw);
