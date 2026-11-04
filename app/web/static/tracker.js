// Drag a Tracker card to another lane: the same as choosing that lane in the card's "Move to" menu and pressing
// Move, so the server's checks are the same. Off on narrow screens (the lanes are drawers; the menu is the way).
if (window.Sortable && matchMedia("(width >= 40rem)").matches) {
  const still = matchMedia("(prefers-reduced-motion: reduce)").matches;
  for (const list of document.querySelectorAll(".board .cards")) {
    Sortable.create(list, {
      group: "tracker", draggable: ".tcard", filter: "button, select, input, label", preventOnFilter: false,
      delay: 150, delayOnTouchOnly: true, animation: still ? 0 : 150,
      onEnd({ item, from, to }) {
        if (from === to) return;
        const select = item.querySelector("select[name=to]");
        select.value = to.dataset.stage;
        select.form.requestSubmit();
      },
    });
  }
}
