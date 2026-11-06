// The options page: it passes the pasted key to the background part, and shows only whether one is kept.
const WORDS = new Map([
  ["paired", "Connected: a key is kept."],
  ["not-connected", "Not connected yet."],
  ["bad-key", "That doesn't look like a key. Copy it again from Settings → Browser plugin."],
  ["no-dashboard", "Can't reach the dashboard: start it; this plugin only talks to port 8765."],
  ["no-key", "The dashboard doesn't know that key. Add this browser again in Settings → Browser plugin."],
  ["wrong-plugin", "The key belongs to another plugin. Remove that browser in Settings → Browser plugin and add it again."],
  ["old-plugin", "This plugin is too old for the dashboard: update it."],
  ["storage", "The key couldn't be saved, so nothing new was kept. Try again."],
]);
const FAILED = "Something went wrong, so nothing new was kept. Try again.";
const key = document.getElementById("key");
const line = document.getElementById("status");

async function ask(message) {
  const answer = await chrome.runtime.sendMessage(message);
  line.textContent = WORDS.get(answer?.say) ?? FAILED;
}

document.getElementById("connect").addEventListener("click", async () => {
  const pasted = key.value.trim();
  key.value = "";
  await ask({ do: "pair", key: pasted });
});

ask({ do: "paired" });
