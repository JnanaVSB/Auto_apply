// The Chrome notification for a Writer that waited for the graphics card. The job's box carries data-notify once, when
// the Writer starts. Chrome asks your permission at your first Yes or No in "Scoring is currently running".
const able = "Notification" in window;  // not on every phone
document.addEventListener("click", (event) => {
  if (able && event.target.closest("[data-ask-notify]") && Notification.permission === "default") {
    Notification.requestPermission();
  }
});
document.addEventListener("htmx:afterSettle", () => {
  for (const marker of document.querySelectorAll("[data-notify]")) {
    if (able && Notification.permission === "granted") new Notification("Writer started", { body: marker.dataset.notify });
    marker.removeAttribute("data-notify");
  }
});
