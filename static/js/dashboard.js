/** Homepage — workflow/chain search + preview. */

document.addEventListener("DOMContentLoaded", () => {
  initWorkflowSearch();
  initWorkflowPreview();
});

function initWorkflowSearch() {
  const input = document.getElementById("workflowSearch");
  const scriptGrid = document.getElementById("scriptGrid");
  const pipelineGrid = document.getElementById("pipelineGrid");
  const pipelineSection = document.getElementById("pipelineSection");
  const workflowsSection = scriptGrid?.closest(".home-workflows");
  const meta = document.getElementById("workflowSearchMeta");
  const empty = document.getElementById("workflowSearchEmpty");
  if (!input || !scriptGrid) return;

  const scriptItems = Array.from(scriptGrid.querySelectorAll(".workflow-item"));
  const pipelineItems = pipelineGrid
    ? Array.from(pipelineGrid.querySelectorAll(".workflow-item"))
    : [];
  const items = [...scriptItems, ...pipelineItems];
  if (!items.length) return;

  const apply = () => {
    const q = input.value.trim().toLowerCase();
    let scriptVisible = 0;
    let pipelineVisible = 0;

    scriptItems.forEach((el) => {
      const show = !q || (el.dataset.search || "").includes(q);
      el.classList.toggle("d-none", !show);
      if (show) scriptVisible += 1;
    });

    pipelineItems.forEach((el) => {
      const show = !q || (el.dataset.search || "").includes(q);
      el.classList.toggle("d-none", !show);
      if (show) pipelineVisible += 1;
    });

    const visible = scriptVisible + pipelineVisible;

    scriptGrid.classList.toggle("d-none", scriptVisible === 0 && !!q);
    if (workflowsSection) {
      const header = workflowsSection.querySelector(".section-header");
      header?.classList.toggle("d-none", scriptVisible === 0 && !!q && pipelineVisible > 0);
    }

    if (pipelineSection) {
      pipelineSection.classList.toggle("d-none", pipelineVisible === 0 && !!q);
    }
    pipelineGrid?.classList.toggle("d-none", pipelineVisible === 0 && !!q);

    if (meta) {
      if (q) {
        meta.classList.remove("d-none");
        meta.textContent = `${visible} of ${items.length} match “${input.value.trim()}”`;
      } else {
        meta.classList.add("d-none");
        meta.textContent = "";
      }
    }

    empty?.classList.toggle("d-none", visible > 0);
  };

  input.addEventListener("input", apply);
  input.addEventListener("search", apply);
}

function initWorkflowPreview() {
  const modal = document.getElementById("workflowPreviewModal");
  if (!modal) return;

  const titleEl = document.getElementById("workflowPreviewTitle");
  const descEl = document.getElementById("workflowPreviewDesc");
  const metaEl = document.getElementById("workflowPreviewMeta");
  const stepsEl = document.getElementById("workflowPreviewSteps");
  const badgeEl = document.getElementById("workflowPreviewBadge");
  const iconWrap = document.getElementById("workflowPreviewIcon");
  const launchEl = document.getElementById("workflowPreviewLaunch");
  const historyEl = document.getElementById("workflowPreviewHistory");
  const metricsEl = document.getElementById("workflowPreviewMetrics");
  const closers = modal.querySelectorAll("[data-workflow-close]");
  let lastFocus = null;

  function openPreview(card) {
    lastFocus = document.activeElement;
    const name = card.dataset.name || "Workflow";
    const description = card.dataset.description || "";
    const icon = card.dataset.icon || "bi-terminal";
    const motion = card.dataset.motion || "default";
    const badge = card.dataset.badge || "";
    const badgeClass = card.dataset.badgeClass || "badge-live";
    const meta = card.dataset.meta || "";
    const steps = card.dataset.steps || "";
    const launch = card.dataset.launch || "";
    const history = card.dataset.history || "#";
    const metrics = card.dataset.metrics || "";

    if (titleEl) titleEl.textContent = name;
    if (descEl) descEl.textContent = description;
    if (metaEl) {
      metaEl.textContent = meta;
      metaEl.classList.toggle("d-none", !meta);
    }
    if (stepsEl) {
      stepsEl.textContent = steps ? `Steps: ${steps}` : "";
      stepsEl.classList.toggle("d-none", !steps);
    }
    if (badgeEl) {
      badgeEl.textContent = badge;
      badgeEl.className = `workflow-badge ${badgeClass}`;
      badgeEl.classList.toggle("d-none", !badge);
    }
    if (iconWrap) {
      iconWrap.dataset.motion = motion;
      iconWrap.innerHTML = `
        <span class="workflow-icon-glow"></span>
        <span class="workflow-icon-fx"></span>
        <i class="bi ${icon}" aria-hidden="true"></i>
      `;
    }
    if (launchEl) {
      if (launch) {
        launchEl.href = launch;
        launchEl.classList.remove("d-none");
      } else {
        launchEl.classList.add("d-none");
      }
    }
    if (historyEl) historyEl.href = history || "#";
    if (metricsEl) {
      if (metrics) {
        metricsEl.href = metrics;
        metricsEl.classList.remove("d-none");
      } else {
        metricsEl.classList.add("d-none");
      }
    }

    modal.hidden = false;
    document.body.classList.add("workflow-preview-open");
    requestAnimationFrame(() => {
      modal.classList.add("is-visible");
      modal.querySelector(".workflow-modal-close")?.focus();
    });
  }

  function closePreview() {
    modal.classList.remove("is-visible");
    document.body.classList.remove("workflow-preview-open");
    window.setTimeout(() => {
      if (!modal.classList.contains("is-visible")) {
        modal.hidden = true;
      }
    }, 220);
    if (lastFocus && typeof lastFocus.focus === "function") {
      lastFocus.focus();
    }
  }

  document.querySelectorAll("[data-workflow-preview]").forEach((card) => {
    card.addEventListener("click", (event) => {
      if (event.target.closest("a, button")) return;
      openPreview(card);
    });
    card.addEventListener("keydown", (event) => {
      if (event.target !== card) return;
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        openPreview(card);
      }
    });
  });

  closers.forEach((el) => el.addEventListener("click", closePreview));
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && document.body.classList.contains("workflow-preview-open")) {
      closePreview();
    }
  });
}
