/** Active Jobs board — queued / running workflows and pipelines. */

document.addEventListener("DOMContentLoaded", () => {
  const root = document.getElementById("jobsApp");
  const list = document.getElementById("jobsList");
  const empty = document.getElementById("jobsEmpty");
  const countPill = document.getElementById("jobsCountPill");
  if (!list || !empty) return;

  let timer = null;

  function escapeHtml(value) {
    const div = document.createElement("div");
    div.textContent = value == null ? "" : String(value);
    return div.innerHTML;
  }

  function formatWhen(iso) {
    if (!iso) return "—";
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return String(iso);
    return d.toLocaleString(undefined, {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  }

  function formatElapsed(seconds) {
    if (seconds == null || Number.isNaN(Number(seconds))) return "";
    const total = Math.max(0, Math.round(Number(seconds)));
    if (total < 60) return `${total}s`;
    const m = Math.floor(total / 60);
    const s = total % 60;
    return `${m}m ${s}s`;
  }

  function paramPreview(params) {
    const entries = Object.entries(params || {}).filter(
      ([k, v]) =>
        k !== "run_name" &&
        v !== null &&
        v !== undefined &&
        String(v).trim() !== ""
    );
    if (!entries.length) return "";
    return entries
      .slice(0, 6)
      .map(([k, v]) => {
        const text = String(v);
        const short = text.length > 48 ? `${text.slice(0, 45)}…` : text;
        return `<span class="jobs-param"><strong>${escapeHtml(k)}</strong> ${escapeHtml(short)}</span>`;
      })
      .join("");
  }

  function render(jobs) {
    const count = jobs.length;
    if (countPill) {
      countPill.textContent = count === 0 ? "Idle" : `${count} active`;
    }
    window.AppUI?.setActiveJobsCount?.(count);

    if (!count) {
      list.innerHTML = "";
      empty.classList.remove("d-none");
      return;
    }
    empty.classList.add("d-none");
    list.innerHTML = jobs
      .map((job) => {
        const status = job.status || "queued";
        const workflow = job.workflow_name || job.script_name || job.script_id || "Job";
        const name = job.display_name || job.run_name || workflow;
        const showWorkflow = Boolean(job.run_name || job.display_name) && name !== workflow;
        const label = job.progress_label || status;
        const kind = job.kind === "pipeline" ? "Chain" : "Workflow";
        const queue =
          status === "queued" && job.queue_position
            ? ` · Queue #${job.queue_position}`
            : "";
        const elapsed = formatElapsed(job.duration_seconds);
        const monitor = job.monitor_url || `/history/${job.job_id}`;
        const starter = job.started_by || "unknown";
        const paramsHtml = paramPreview(job.parameters);
        return `
          <article class="jobs-card jobs-card-${escapeHtml(status)}">
            <div class="jobs-card-main">
              <div class="jobs-card-topline">
                <strong>${escapeHtml(name)}</strong>
                <span class="jobs-card-status">${escapeHtml(status)}</span>
                ${job.is_mine ? `<span class="jobs-card-mine">Yours</span>` : ""}
              </div>
              <p class="jobs-card-meta">
                ${showWorkflow ? `<span class="jobs-card-workflow">${escapeHtml(workflow)}</span>` : ""}
                <span>${escapeHtml(kind)}</span>
                <span>Started by <strong>${escapeHtml(starter)}</strong></span>
                <span>${escapeHtml(label)}${escapeHtml(queue)}</span>
                <span>${escapeHtml(formatWhen(job.started_at || job.created_at))}</span>
                ${elapsed ? `<span>${escapeHtml(elapsed)} elapsed</span>` : ""}
              </p>
              ${
                paramsHtml
                  ? `<div class="jobs-params" aria-label="Settings in use">${paramsHtml}</div>`
                  : ""
              }
            </div>
            <div class="jobs-card-actions">
              <a href="${escapeHtml(monitor)}" class="btn btn-sm btn-primary">Watch</a>
              ${
                job.cancellable
                  ? `<button type="button" class="btn btn-sm btn-outline-danger" data-cancel="${escapeHtml(
                      job.job_id
                    )}">Cancel</button>`
                  : job.status === "queued" || job.status === "running"
                    ? `<span class="jobs-card-note">Only ${escapeHtml(starter)} can stop</span>`
                    : ""
              }
            </div>
          </article>`;
      })
      .join("");
  }

  async function refresh() {
    if (window.AppUI?.isAuthenticated && !window.AppUI.isAuthenticated()) {
      if (timer) {
        clearInterval(timer);
        timer = null;
      }
      return;
    }
    try {
      const res = await fetch("/api/jobs/active");
      if (res.status === 401) {
        if (timer) {
          clearInterval(timer);
          timer = null;
        }
        window.AppUI?.stopAuthPolling?.();
        if (!window.location.pathname.startsWith("/login")) {
          const next = encodeURIComponent(window.location.pathname + window.location.search);
          window.location.href = `/login?next=${next}`;
        }
        return;
      }
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Could not load active jobs");
      render(data.jobs || []);
    } catch (err) {
      if (countPill) countPill.textContent = "Unavailable";
      window.AppUI?.showToast(err.message || "Could not load active jobs.", "danger");
    }
  }

  list.addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-cancel]");
    if (!btn) return;
    const jobId = btn.dataset.cancel;
    if (!jobId) return;
    if (!confirm("Cancel this job?")) return;
    btn.disabled = true;
    try {
      const res = await fetch(`/api/jobs/${encodeURIComponent(jobId)}/cancel`, {
        method: "POST",
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Could not cancel");
      window.AppUI?.showToast("Cancel requested.", "warning");
      await refresh();
    } catch (err) {
      window.AppUI?.showToast(err.message, "danger");
      btn.disabled = false;
    }
  });

  refresh();
  timer = setInterval(refresh, 2500);
  window.addEventListener("beforeunload", () => {
    if (timer) clearInterval(timer);
  });
});
