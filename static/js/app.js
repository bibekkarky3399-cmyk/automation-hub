/** Shared UI helpers — plain script (no ES modules). */

let activeJobsTimer = null;
let notificationsTimer = null;
let authRedirecting = false;

function isAuthenticated() {
  return document.body?.dataset?.authenticated === "1";
}

function stopAuthPolling() {
  if (activeJobsTimer) {
    clearInterval(activeJobsTimer);
    activeJobsTimer = null;
  }
  if (notificationsTimer) {
    clearInterval(notificationsTimer);
    notificationsTimer = null;
  }
}

function redirectToLogin() {
  if (authRedirecting) return;
  if (window.location.pathname.startsWith("/login")) return;
  authRedirecting = true;
  stopAuthPolling();
  document.body?.removeAttribute("data-authenticated");
  const next = encodeURIComponent(window.location.pathname + window.location.search);
  showPageTransit();
  window.location.href = `/login?next=${next}`;
}

function handleAuthFailure(res) {
  if (res && res.status === 401) {
    redirectToLogin();
    return true;
  }
  return false;
}

function showToast(message, variant = "primary", options = {}) {
  const host = document.getElementById("toastHost");
  if (!host) return;
  const el = document.createElement("div");
  el.className = `toast align-items-center text-bg-${variant} border-0`;
  el.setAttribute("role", "alert");
  const href = options.href;
  const linkLabel = options.linkLabel || "Open";
  const linkHtml = href
    ? `<a href="${href}" class="btn btn-sm btn-light ms-2">${linkLabel}</a>`
    : "";
  el.innerHTML = `
    <div class="d-flex align-items-center">
      <div class="toast-body">${message}${linkHtml}</div>
      <button type="button" class="btn-close btn-close-white me-2 m-auto" data-bs-dismiss="toast"></button>
    </div>`;
  host.appendChild(el);
  const toast = new bootstrap.Toast(el, { delay: options.delay ?? 7000 });
  toast.show();
  el.addEventListener("hidden.bs.toast", () => el.remove());
}

function setActiveJobsCount(count) {
  const badges = [
    document.getElementById("navJobsBadge"),
    ...document.querySelectorAll("[data-nav-jobs-badge]"),
  ].filter(Boolean);
  const n = Math.max(0, Number(count) || 0);
  badges.forEach((badge) => {
    badge.textContent = String(n);
    badge.classList.toggle("d-none", n < 1);
  });
}

async function refreshActiveJobsBadge() {
  if (!isAuthenticated()) {
    stopAuthPolling();
    return;
  }
  try {
    const res = await fetch("/api/jobs/active");
    if (handleAuthFailure(res)) return;
    const data = await res.json().catch(() => ({}));
    if (!res.ok) return;
    setActiveJobsCount(data.count || 0);
  } catch {
    /* ignore badge errors */
  }
}

function toastVariantForLevel(level) {
  if (level === "success") return "success";
  if (level === "danger" || level === "error") return "danger";
  if (level === "warning") return "warning";
  return "primary";
}

function maybeBrowserNotify(title, body) {
  if (!("Notification" in window)) return;
  if (Notification.permission === "granted") {
    try {
      new Notification(title, { body, silent: false });
    } catch {
      /* ignore */
    }
    return;
  }
  if (Notification.permission === "default") {
    Notification.requestPermission().catch(() => {});
  }
}

async function pollUserNotifications() {
  if (!isAuthenticated()) {
    stopAuthPolling();
    return;
  }
  try {
    const res = await fetch("/api/notifications?unread=1");
    if (handleAuthFailure(res)) return;
    const data = await res.json().catch(() => ({}));
    if (!res.ok) return;
    const items = data.notifications || [];
    if (!items.length) return;

    const ids = items.map((n) => n.id).filter(Boolean);
    for (const note of items) {
      const variant = toastVariantForLevel(note.level);
      const msg = `<strong>${escapeToast(note.title || "Job update")}</strong><br>${escapeToast(
        note.body || ""
      )}`;
      showToast(msg, variant, {
        href: note.href || (note.job_id ? `/history/${note.job_id}` : "/jobs"),
        linkLabel: "View",
        delay: 9000,
      });
      maybeBrowserNotify(note.title || "Helix", note.body || "");
    }
    const ackRes = await fetch("/api/notifications/ack", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ids }),
    });
    if (handleAuthFailure(ackRes)) return;
    refreshActiveJobsBadge();
  } catch {
    /* ignore */
  }
}

function escapeToast(value) {
  const div = document.createElement("div");
  div.textContent = value == null ? "" : String(value);
  return div.innerHTML;
}

async function checkHealth() {
  const navStatus = document.getElementById("navStatus");
  if (!navStatus) return;

  const dot = navStatus.querySelector(".status-dot");
  const label = navStatus.querySelector(".status-label");

  try {
    const res = await fetch("/api/health");
    const data = await res.json();
    if (!res.ok) throw new Error(data.message || "unhealthy");

    if (data.status === "ok") {
      dot?.classList.remove("status-dot-error", "status-dot-warn");
      if (label) label.textContent = "Operational";
      return;
    }

    if (data.status === "degraded") {
      dot?.classList.remove("status-dot-error");
      dot?.classList.add("status-dot-warn");
      if (label) label.textContent = "Needs attention";
      const detail =
        Array.isArray(data.config_errors) && data.config_errors.length
          ? data.config_errors[0]
          : "Configuration needs attention.";
      window.AppUI?.showToast(detail, "warning");
      return;
    }

    throw new Error(data.message || "unhealthy");
  } catch {
    dot?.classList.remove("status-dot-warn");
    dot?.classList.add("status-dot-error");
    if (label) label.textContent = "Unavailable";
  }
}

function initMobileNav() {
  const toggle = document.getElementById("navMenuToggle");
  const panel = document.getElementById("mobileNav");
  if (!toggle || !panel) return;

  const setOpen = (open) => {
    panel.classList.toggle("d-none", !open);
    toggle.setAttribute("aria-expanded", open ? "true" : "false");
    toggle.setAttribute("aria-label", open ? "Close menu" : "Open menu");
    const icon = toggle.querySelector("i");
    if (icon) icon.className = open ? "bi bi-x-lg" : "bi bi-list";
  };

  toggle.addEventListener("click", () => {
    setOpen(panel.classList.contains("d-none"));
  });

  panel.querySelectorAll("a").forEach((link) => {
    link.addEventListener("click", () => setOpen(false));
  });
}

const PAGE_TRANSIT_KEY = "helixPageTransitAt";
const PAGE_TRANSIT_MS = 250;
let pageTransitHideTimer = null;

function showPageTransit() {
  try {
    sessionStorage.setItem(PAGE_TRANSIT_KEY, String(Date.now()));
  } catch {
    /* ignore */
  }
  document.documentElement.classList.add("helix-transit");
  const el = document.getElementById("pageTransit");
  if (!el) return;
  el.hidden = false;
  el.classList.add("is-on");
  el.setAttribute("aria-hidden", "false");
}

function hidePageTransit() {
  document.documentElement.classList.remove("helix-transit");
  try {
    sessionStorage.removeItem(PAGE_TRANSIT_KEY);
  } catch {
    /* ignore */
  }
  const el = document.getElementById("pageTransit");
  if (!el) return;
  el.classList.remove("is-on");
  el.setAttribute("aria-hidden", "true");
  window.setTimeout(() => {
    if (!document.documentElement.classList.contains("helix-transit")) {
      el.hidden = true;
    }
  }, 220);
}

function finishPageTransit() {
  let started = 0;
  try {
    started = Number(sessionStorage.getItem(PAGE_TRANSIT_KEY) || 0);
  } catch {
    started = 0;
  }
  if (!started && !document.documentElement.classList.contains("helix-transit")) {
    hidePageTransit();
    return;
  }
  const el = document.getElementById("pageTransit");
  if (el) {
    el.hidden = false;
    el.classList.add("is-on");
    el.setAttribute("aria-hidden", "false");
  }
  document.documentElement.classList.add("helix-transit");
  const elapsed = started ? Date.now() - started : 0;
  const wait = Math.max(120, PAGE_TRANSIT_MS - elapsed);
  if (pageTransitHideTimer) clearTimeout(pageTransitHideTimer);
  pageTransitHideTimer = setTimeout(hidePageTransit, wait);
}

function shouldTransitToUrl(url) {
  if (!url) return false;
  if (url.origin !== window.location.origin) return false;
  // Same page (including hash-only jumps) — no full navigation loader.
  if (url.pathname === window.location.pathname && url.search === window.location.search) {
    return false;
  }
  return true;
}

function initPageTransit() {
  const el = document.getElementById("pageTransit");
  if (!el) return;

  if (document.documentElement.classList.contains("helix-transit")) {
    finishPageTransit();
  } else {
    el.hidden = true;
  }

  document.addEventListener("click", (e) => {
    if (e.defaultPrevented) return;
    if (e.button !== 0) return;
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    const anchor = e.target.closest("a[href]");
    if (!anchor) return;
    if (anchor.hasAttribute("data-no-transit")) return;
    if (anchor.target && anchor.target !== "_self") return;
    if (anchor.hasAttribute("download")) return;
    const href = anchor.getAttribute("href");
    if (!href || href.startsWith("javascript:") || href.startsWith("mailto:") || href.startsWith("tel:")) {
      return;
    }
    let url;
    try {
      url = new URL(anchor.href, window.location.href);
    } catch {
      return;
    }
    if (!shouldTransitToUrl(url)) return;
    showPageTransit();
  });

  document.addEventListener("submit", (e) => {
    const form = e.target;
    if (!(form instanceof HTMLFormElement)) return;
    if (form.hasAttribute("data-no-transit")) return;
    if (form.target && form.target !== "_self") return;
    // Wait until other handlers can preventDefault (AJAX forms).
    queueMicrotask(() => {
      if (e.defaultPrevented) return;
      showPageTransit();
    });
  });

  window.addEventListener("pageshow", (ev) => {
    if (ev.persisted) hidePageTransit();
  });
}

function startAuthPolling() {
  if (!isAuthenticated()) return;
  refreshActiveJobsBadge();
  pollUserNotifications();
  activeJobsTimer = setInterval(refreshActiveJobsBadge, 5000);
  notificationsTimer = setInterval(pollUserNotifications, 4000);
}

document.addEventListener("DOMContentLoaded", () => {
  initPageTransit();
  initMobileNav();
  checkHealth();
  startAuthPolling();
});

// Expose for non-module scripts
window.AppUI = {
  showToast,
  setActiveJobsCount,
  refreshActiveJobsBadge,
  checkHealth,
  pollUserNotifications,
  stopAuthPolling,
  isAuthenticated,
  showPageTransit,
  hidePageTransit,
};
