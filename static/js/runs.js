/** History page — searchable job picker. */

document.addEventListener("DOMContentLoaded", () => {
  initJobPicker();
});

function initJobPicker() {
  const root = document.querySelector("[data-runs-job-picker]");
  if (!root) return;

  const search = root.querySelector("#runsJobSearch");
  const results = root.querySelector("#runsJobResults");
  const empty = root.querySelector("#runsJobEmpty");
  const options = Array.from(root.querySelectorAll(".runs-job-option"));
  if (!search || !results || !options.length) return;

  const applyFilter = () => {
    const q = (search.value || "").trim().toLowerCase();
    let visible = 0;

    options.forEach((opt) => {
      const name = (opt.dataset.name || "").toLowerCase();
      const show = !q || name.includes(q);
      opt.classList.toggle("d-none", !show);
      if (show) visible += 1;
    });

    if (empty) empty.classList.toggle("d-none", visible > 0);
  };

  const open = () => root.classList.add("is-open");
  const close = () => root.classList.remove("is-open");

  search.addEventListener("focus", () => {
    open();
    applyFilter();
  });
  search.addEventListener("input", () => {
    open();
    applyFilter();
  });

  document.addEventListener("pointerdown", (event) => {
    if (!root.contains(event.target)) close();
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      close();
      search.blur();
    }
  });
}