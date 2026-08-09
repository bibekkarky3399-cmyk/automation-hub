/** Metrics page — animated charts. */

document.addEventListener("DOMContentLoaded", () => {
  initWorkflowPicker();
  initAnimatedPies();
  initCalendar();
});

function initWorkflowPicker() {
  const root = document.querySelector("[data-metrics-wf-picker]");
  if (!root) return;

  const search = root.querySelector("#metricsWfSearch");
  const results = root.querySelector("#metricsWfResults");
  const empty = root.querySelector("#metricsWfEmpty");
  const options = Array.from(root.querySelectorAll(".metrics-wf-option"));
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

  search.addEventListener("focus", open);
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

  applyFilter();
}

function initAnimatedPies() {
  const layouts = Array.from(document.querySelectorAll("[data-pie-animate]"));
  if (!layouts.length) return;

  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  const play = (layout) => {
    if (layout.classList.contains("is-pie-live")) return;
    layout.classList.add("is-pie-live");
    animatePieCounts(layout, reduceMotion ? 0 : 900);
  };

  observeAndPlay(layouts, play, reduceMotion);
}

function initCalendar() {
  const calendars = Array.from(document.querySelectorAll(".metrics-cal[data-day-animate]"));
  if (!calendars.length) return;

  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  observeAndPlay(
    calendars,
    (calendar) => {
      calendar.classList.add("is-day-live");
    },
    reduceMotion
  );
}

function observeAndPlay(elements, play, reduceMotion) {
  if (reduceMotion || !("IntersectionObserver" in window)) {
    elements.forEach(play);
    return;
  }

  const observer = new IntersectionObserver(
    (entries) => {
      entries.forEach((entry) => {
        if (entry.isIntersecting) {
          play(entry.target);
          observer.unobserve(entry.target);
        }
      });
    },
    { threshold: 0.25 }
  );

  elements.forEach((el) => observer.observe(el));
}

function animatePieCounts(root, duration) {
  const nodes = root.querySelectorAll("[data-pie-count]");
  nodes.forEach((node) => {
    const target = Number(node.dataset.pieCount || 0);
    const suffix = node.dataset.pieSuffix || "";
    if (!duration || !Number.isFinite(target)) {
      node.textContent = `${target}${suffix}`;
      return;
    }

    const start = performance.now();
    const from = 0;
    const isFloat = !Number.isInteger(target);

    const tick = (now) => {
      const t = Math.min(1, (now - start) / duration);
      const eased = 1 - Math.pow(1 - t, 3);
      const value = from + (target - from) * eased;
      node.textContent = `${isFloat ? value.toFixed(1) : Math.round(value)}${suffix}`;
      if (t < 1) requestAnimationFrame(tick);
      else node.textContent = `${isFloat ? target : Math.round(target)}${suffix}`;
    };

    requestAnimationFrame(tick);
  });
}
