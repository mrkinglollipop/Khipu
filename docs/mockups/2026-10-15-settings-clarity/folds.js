// Mock-only: makes the folds and Open all / Close all work so the mocks can be tried with a keyboard.
document.addEventListener("click", (e) => {
  const btn = e.target.closest("button.fold-btn");
  if (btn) return setFold(btn, btn.getAttribute("aria-expanded") !== "true");
  const all = e.target.closest("button[data-folds]");
  if (all) {
    const open = all.dataset.folds === "open";
    document.querySelectorAll(".route-group > button.fold-btn").forEach((b) => setFold(b, open));
  }
});
function setFold(btn, open) {
  btn.setAttribute("aria-expanded", String(open));
  btn.parentElement.dataset.open = String(open);
  const body = document.getElementById(btn.getAttribute("aria-controls"));
  if (body) body.hidden = !open;
}
