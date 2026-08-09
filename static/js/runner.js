document.addEventListener("DOMContentLoaded", async () => {
  const root = document.getElementById("runnerApp");
  if (!root) return;

  const scriptId = root.dataset.scriptId;
  const pipelineId = root.dataset.pipelineId || "";
  const isPipeline = root.dataset.mode === "pipeline" && !!pipelineId;
  const currentUser = root.dataset.currentUser || "";
  const projectRoot = window.RUNNER_PROJECT_ROOT || "";
  const presetKey = `ah-last-params:${scriptId}`;
  const activeJobKey = `helix:activeJob:${scriptId}`;
  let scriptMeta = null;
  let currentJobId = null;
  let pollTimer = null;
  let pollFailures = 0;
  let stdoutSince = 0;
  let stderrSince = 0;
  let browseTargetInputId = null;
  let browseCurrentPath = projectRoot;
  let activeStageIndex = -1;
  let stageFrozen = false;
  let logVisible = false;
  let progressValue = 8;
  let softProgressTimer = null;
  let progressAnimRaf = null;
  let progressAnimResolve = null;
  const COMPLETE_PROGRESS_MS = 2800;
  const stagedFiles = {};
  const STAGE_FAIL_MARKERS = [
    "login failed",
    "stopped —",
    "stopped -",
    "test date failed",
    "❌ stopped",
  ];

  const panelConfigure = document.getElementById("panelConfigure");
  const panelRunning = document.getElementById("panelRunning");
  const panelDone = document.getElementById("panelDone");
  const dynamicInputs = document.getElementById("dynamicInputs");
  const btnRun = document.getElementById("btnRun");
  const liveLog = document.getElementById("liveLog");
  const progressLabel = document.getElementById("progressLabel");
  const progressPct = document.getElementById("progressPct");
  const progressCaption = document.getElementById("progressCaption");
  const runningTitle = document.getElementById("runningTitle");
  const runProgress = document.getElementById("runProgress");
  const pathHintWrap = document.getElementById("pathHintWrap");
  const runPipeline = document.getElementById("runPipeline");
  const runPipelineEmpty = document.getElementById("runPipelineEmpty");
  const btnRestoreLast = document.getElementById("btnRestoreLast");
  const browseModalEl = document.getElementById("folderBrowseModal");
  const browseModal = browseModalEl ? new bootstrap.Modal(browseModalEl) : null;
  const confirmModalEl = document.getElementById("runnerConfirmModal");
  const confirmModal = confirmModalEl ? new bootstrap.Modal(confirmModalEl) : null;

  const FIELD_ICONS = {
    folder: "bi-folder2",
    file: "bi-file-earmark-arrow-up",
    text: "bi-input-cursor-text",
    select: "bi-list-ul",
    boolean: "bi-toggle-on",
  };

  function escapeHtml(str) {
    const div = document.createElement("div");
    div.textContent = str == null ? "" : String(str);
    return div.innerHTML;
  }

  function setStep(n) {
    const steps = document.querySelectorAll(".runner-stepper .runner-step, .stepper .step");
    steps.forEach((el) => {
      const s = Number(el.dataset.step);
      const active = s === n;
      const done = s < n;
      el.classList.toggle("active", active);
      el.classList.toggle("done", done);
      if (active) el.setAttribute("aria-current", "step");
      else el.removeAttribute("aria-current");
    });

    const stepper = document.getElementById("stepper");
    if (stepper) {
      stepper.dataset.phase = String(n);
      stepper.querySelectorAll(".runner-step-connector").forEach((conn, i) => {
        conn.classList.toggle("is-lit", i + 1 < n);
      });
    }
  }

  function renderFieldLabel(inp) {
    const icon = FIELD_ICONS[inp.type] || "bi-sliders";
    const req = inp.required ? '<span class="required-mark">*</span>' : "";
    return `
      <label class="form-field-label" for="inp_${inp.id}">
        <span class="field-icon"><i class="bi ${icon}"></i></span>
        <span>${escapeHtml(inp.label)}${req}</span>
      </label>`;
  }

  function renderHelp(inp) {
    const text = String(inp.help || "").trim();
    // Always reserve one hint line so half-width inputs stay aligned.
    return `<div class="form-field-help${text ? "" : " is-empty"}">${text ? escapeHtml(text) : "&nbsp;"}</div>`;
  }

  function renderFeedback(id) {
    return `<div class="field-feedback" id="hint_${id}"></div>`;
  }

  function fieldWidthClass(inp) {
    const w = (inp.width || (inp.type === "boolean" || inp.type === "folder" || inp.type === "file" ? "full" : "half")).toLowerCase();
    return w === "half" ? "form-field--half" : "form-field--full";
  }

  function renderSingleInput(inp) {
    const wrap = document.createElement("div");
    wrap.className = `form-field ${fieldWidthClass(inp)}`;
    wrap.dataset.inputType = inp.type;
    wrap.dataset.inputId = inp.id;
    wrap.dataset.width = inp.width || "half";

    if (inp.type === "folder") {
      wrap.innerHTML = `
        ${renderFieldLabel(inp)}
        ${renderHelp(inp)}
        <div class="input-group-app">
          <input type="text" class="form-control form-control-app" id="inp_${inp.id}"
            placeholder="Select a folder…" autocomplete="off"
            value="${escapeHtml(inp.default || "")}">
          <button class="btn btn-outline-secondary" type="button" data-browse="${inp.id}">
            <i class="bi bi-folder2-open me-1"></i>Browse
          </button>
          <button class="btn btn-outline-secondary" type="button" data-validate="${inp.id}">Check</button>
        </div>
        ${renderFeedback(inp.id)}`;
    } else if (inp.type === "boolean") {
      wrap.innerHTML = `
        <div class="form-field-boolean">
          <div>
            <div class="boolean-label">${escapeHtml(inp.label)}</div>
            <div class="boolean-help">${inp.help ? escapeHtml(inp.help) : "&nbsp;"}</div>
          </div>
          <div class="form-check form-switch mb-0">
            <input class="form-check-input" type="checkbox" id="inp_${inp.id}" ${inp.default ? "checked" : ""}>
          </div>
        </div>`;
    } else if (inp.type === "select") {
      const options = (inp.options || [])
        .map((opt) => {
          const val = typeof opt === "string" ? opt : opt.value;
          const label = typeof opt === "string" ? opt : opt.label || opt.value;
          const selected = val === (inp.default ?? "") ? " selected" : "";
          return `<option value="${escapeHtml(val)}"${selected}>${escapeHtml(label)}</option>`;
        })
        .join("");
      wrap.innerHTML = `
        ${renderFieldLabel(inp)}
        ${renderHelp(inp)}
        <select class="form-select form-select-app" id="inp_${inp.id}">
          ${inp.required ? "" : '<option value="">Choose…</option>'}
          ${options}
        </select>
        ${renderFeedback(inp.id)}`;
    } else if (inp.type === "file") {
      const accept = inp.accept ? ` accept="${escapeHtml(inp.accept)}"` : "";
      wrap.innerHTML = `
        ${renderFieldLabel(inp)}
        ${renderHelp(inp)}
        <div class="file-dropzone" id="dropzone_${inp.id}" data-file-input="${inp.id}">
          <input type="file" id="inp_${inp.id}"${accept}${inp.multiple ? " multiple" : ""}>
          <div class="file-dropzone-icon"><i class="bi bi-cloud-arrow-up"></i></div>
          <div class="file-dropzone-text">Drop a file here, or click to choose one</div>
          <div class="file-dropzone-hint">${inp.accept ? `Looks for: ${escapeHtml(inp.accept)}` : "Any file is fine"}</div>
          <div class="file-selected-name mt-2 d-none" id="filename_${inp.id}"></div>
        </div>
        <input type="hidden" id="staged_${inp.id}" value="">
        ${renderFeedback(inp.id)}`;
    } else {
      const inputType = inp.input_type || "text";
      const isPassword = inputType === "password";
      const displayValue = fieldDisplayValue(inp);
      wrap.innerHTML = `
        ${renderFieldLabel(inp)}
        ${renderHelp(inp)}
        <div class="${isPassword ? "input-group-app" : ""}">
          <input type="${escapeHtml(inputType)}" class="form-control form-control-app" id="inp_${inp.id}"
            placeholder="${escapeHtml(inp.placeholder || "")}"
            value="${escapeHtml(displayValue)}"
            ${inp.pattern ? `pattern="${escapeHtml(inp.pattern)}"` : ""}
            autocomplete="${isPassword ? "current-password" : "off"}">
          ${
            isPassword
              ? `<button class="btn btn-outline-secondary" type="button" data-toggle-password="${inp.id}" aria-label="Show password">
                   <i class="bi bi-eye"></i>
                 </button>`
              : ""
          }
        </div>
        ${renderFeedback(inp.id)}`;
    }
    return wrap;
  }

  const MONTHS_SHORT = [
    "JAN", "FEB", "MAR", "APR", "MAY", "JUN",
    "JUL", "AUG", "SEP", "OCT", "NOV", "DEC",
  ];

  function toIsoDate(value) {
    const raw = String(value || "").trim();
    if (!raw) return "";
    if (/^\d{4}-\d{2}-\d{2}$/.test(raw)) return raw;
    let m = raw.match(/^(\d{1,2})-([A-Za-z]{3})-(\d{4})$/);
    if (m) {
      const month = MONTHS_SHORT.indexOf(m[2].toUpperCase());
      if (month >= 0) {
        return `${m[3]}-${String(month + 1).padStart(2, "0")}-${m[1].padStart(2, "0")}`;
      }
    }
    m = raw.match(/^(\d{1,2})-(\d{1,2})-(\d{4})$/);
    if (m) {
      return `${m[3]}-${m[2].padStart(2, "0")}-${m[1].padStart(2, "0")}`;
    }
    return "";
  }

  function fieldDisplayValue(inp) {
    const raw = inp.default == null ? "" : String(inp.default);
    if ((inp.input_type || "") === "date") return toIsoDate(raw) || "";
    return raw;
  }

  function matchesShowWhen(showWhen) {
    if (!showWhen || typeof showWhen !== "object") return true;
    return Object.entries(showWhen).every(([id, expected]) => {
      const el = document.getElementById(`inp_${id}`);
      const current = el
        ? el.type === "checkbox"
          ? el.checked
            ? "true"
            : "false"
          : String(el.value || "")
        : String(
            (scriptMeta.inputs || []).find((i) => i.id === id)?.default ?? ""
          );
      const allowed = Array.isArray(expected) ? expected.map(String) : [String(expected)];
      return allowed.includes(current);
    });
  }

  function groupStartsOpen(groupName, fields, index) {
    if (fields.some((f) => f.collapsed)) return false;
    if (fields.every((f) => f.advanced)) return false;
    if (/more options|optional|advanced|extra|passenger/i.test(groupName)) return false;
    if (index === 0) return true;
    if (/dataset|source|basics|what do you need|what to collect/i.test(groupName)) return true;
    // Mode-specific sections open when they match the current choice.
    const sample = fields.find((f) => f.show_when);
    if (sample?.show_when) return matchesShowWhen(sample.show_when);
    return true;
  }

  function buildInputs(inputs) {
    dynamicInputs.innerHTML = "";
    const groupOrder = [];
    const grouped = new Map();

    inputs.forEach((inp) => {
      if (inp.hidden) return;
      const g = inp.group || (inp.advanced ? "More options" : "Basics");
      if (!grouped.has(g)) {
        grouped.set(g, []);
        groupOrder.push(g);
      }
      grouped.get(g).push(inp);
    });

    groupOrder.forEach((groupName, index) => {
      const fields = grouped.get(groupName);
      const section = document.createElement("details");
      section.className = "form-group-section";
      section.dataset.group = groupName;
      section.open = groupStartsOpen(groupName, fields, index);
      section.innerHTML = `
        <summary class="form-group-summary" title="Expand or collapse this section">
          <span class="form-group-title">${escapeHtml(groupName)}</span>
          <span class="form-group-hint">${section.open ? "Hide" : "Show"}</span>
          <i class="bi bi-chevron-down form-group-chevron" aria-hidden="true"></i>
        </summary>`;
      const body = document.createElement("div");
      body.className = "form-group-body form-group-grid";
      fields.forEach((inp) => body.appendChild(renderSingleInput(inp)));
      section.appendChild(body);
      section.addEventListener("toggle", () => {
        const hint = section.querySelector(".form-group-hint");
        if (hint) hint.textContent = section.open ? "Hide" : "Show";
      });
      dynamicInputs.appendChild(section);
    });

    renderFormNote();
    bindInputEvents(inputs);
    syncInputModeVisibility();
    ensureRunNameDefault();
    maybeShowRestore();
  }

  function suggestedRunName() {
    const base = (scriptMeta?.name || "Run").trim() || "Run";
    const now = new Date();
    const label = now.toLocaleDateString(undefined, { day: "numeric", month: "short" });
    return `${base} — ${label}`;
  }

  function currentRunName() {
    const el = document.getElementById("inp_run_name");
    const typed = el?.value?.trim();
    return typed || suggestedRunName();
  }

  function ensureRunNameDefault() {
    const el = document.getElementById("inp_run_name");
    if (!el || el.value.trim()) return;
    el.value = suggestedRunName();
  }

  function renderFormNote() {
    if (!pathHintWrap) return;
    const note = (scriptMeta?.form_note || "").trim();
    if (!note) {
      pathHintWrap.classList.add("d-none");
      pathHintWrap.innerHTML = "";
      return;
    }
    pathHintWrap.classList.add("path-hint", "app-info");
    pathHintWrap.innerHTML = `<i class="bi bi-info-circle" aria-hidden="true"></i><span>${escapeHtml(note)}</span>`;
    pathHintWrap.classList.remove("d-none");
  }

  function sourceMode() {
    return (
      document.getElementById("inp_csv_mode")?.value ||
      document.getElementById("inp_input_mode")?.value ||
      "folder"
    );
  }

  function syncInputModeVisibility() {
    const mode = sourceMode();
    const pairs = [
      ["image_folder", ["folder"]],
      ["image_file", ["file"]],
      ["csv_folder", ["folder", "all"]],
      ["csv", ["file"]],
      ["csv_list", ["list"]],
    ];
    pairs.forEach(([id, whenModes]) => {
      const field = dynamicInputs.querySelector(`[data-input-id="${id}"]`);
      if (field) field.classList.toggle("d-none", !whenModes.includes(mode));
    });

    (scriptMeta.inputs || []).forEach((inp) => {
      if (!inp.show_when || inp.hidden) return;
      const field = dynamicInputs.querySelector(`[data-input-id="${inp.id}"]`);
      if (field) field.classList.toggle("d-none", !matchesShowWhen(inp.show_when));
    });

    dynamicInputs.querySelectorAll(".form-group-section").forEach((section) => {
      const fields = [...section.querySelectorAll(".form-field")];
      const visible = fields.some((f) => !f.classList.contains("d-none"));
      section.classList.toggle("d-none", !visible);
      if (visible && section.tagName === "DETAILS") {
        const isPrimary = /dataset|source|basics|what do you need|what to collect/i.test(
          section.dataset.group || ""
        );
        const isOptional = /more options|optional|advanced|extra|passenger/i.test(
          section.dataset.group || ""
        );
        if (!isOptional) section.open = true;
        if (isPrimary) section.open = true;
      }
    });

    // Return date only matters for round trips.
    const trip = document.getElementById("inp_trip_type")?.value;
    const returnField = dynamicInputs.querySelector('[data-input-id="return_date"]');
    if (returnField && trip) {
      const parentHidden = returnField.closest(".form-group-section")?.classList.contains("d-none");
      if (!parentHidden) {
        returnField.classList.toggle("d-none", String(trip).toUpperCase() !== "R");
      }
    }

    renderFormNote();
  }

  function bindInputEvents(inputs) {
    dynamicInputs.querySelectorAll("[data-validate]").forEach((btn) => {
      btn.addEventListener("click", () => validateFolder(btn.dataset.validate));
    });
    dynamicInputs.querySelectorAll("[data-browse]").forEach((btn) => {
      btn.addEventListener("click", () => openBrowseModal(btn.dataset.browse));
    });
    dynamicInputs.querySelectorAll("[data-toggle-password]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const id = btn.dataset.togglePassword;
        const input = document.getElementById(`inp_${id}`);
        if (!input) return;
        const show = input.type === "password";
        input.type = show ? "text" : "password";
        btn.innerHTML = show ? '<i class="bi bi-eye-slash"></i>' : '<i class="bi bi-eye"></i>';
      });
    });
    inputs.filter((i) => i.type === "file").forEach((inp) => setupFileDropzone(inp));
    document.getElementById("inp_input_mode")?.addEventListener("change", syncInputModeVisibility);
    document.getElementById("inp_csv_mode")?.addEventListener("change", syncInputModeVisibility);
    document.getElementById("inp_mode")?.addEventListener("change", syncInputModeVisibility);
    document.getElementById("inp_trip_type")?.addEventListener("change", syncInputModeVisibility);
  }

  function setupFileDropzone(inp) {
    const dropzone = document.getElementById(`dropzone_${inp.id}`);
    const fileInput = document.getElementById(`inp_${inp.id}`);
    if (!dropzone || !fileInput) return;

    dropzone.addEventListener("click", (e) => {
      if (e.target === fileInput) return;
      fileInput.click();
    });
    dropzone.addEventListener("dragover", (e) => {
      e.preventDefault();
      dropzone.classList.add("dragover");
    });
    dropzone.addEventListener("dragleave", () => dropzone.classList.remove("dragover"));
    dropzone.addEventListener("drop", (e) => {
      e.preventDefault();
      dropzone.classList.remove("dragover");
      if (e.dataTransfer.files.length) {
        fileInput.files = e.dataTransfer.files;
        handleFileSelected(inp, fileInput.files[0]);
      }
    });
    fileInput.addEventListener("change", () => {
      if (fileInput.files.length) handleFileSelected(inp, fileInput.files[0]);
    });
  }

  async function handleFileSelected(inp, file) {
    const dropzone = document.getElementById(`dropzone_${inp.id}`);
    const filenameEl = document.getElementById(`filename_${inp.id}`);
    const hint = document.getElementById(`hint_${inp.id}`);
    const stagedField = document.getElementById(`staged_${inp.id}`);

    dropzone?.classList.add("has-file");
    filenameEl?.classList.remove("d-none");
    if (filenameEl) filenameEl.textContent = file.name;
    setFieldFeedback(hint, "Uploading…", "");

    const formData = new FormData();
    formData.append("file", file);
    formData.append("input_id", inp.id);
    if (inp.accept) formData.append("accept", inp.accept);

    try {
      const res = await fetch("/api/upload-file", { method: "POST", body: formData });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Upload failed");
      stagedFiles[inp.id] = data.path;
      if (stagedField) stagedField.value = data.path;
      setFieldFeedback(hint, `Ready — ${data.filename}`, "success");
      window.AppUI?.showToast("File uploaded.", "success");
    } catch (err) {
      stagedFiles[inp.id] = null;
      if (stagedField) stagedField.value = "";
      dropzone?.classList.remove("has-file");
      filenameEl?.classList.add("d-none");
      setFieldFeedback(hint, err.message, "error");
      window.AppUI?.showToast(err.message, "danger");
    }
  }

  function setFieldFeedback(el, text, type) {
    if (!el) return;
    el.textContent = text;
    el.className = "field-feedback" + (type ? ` ${type}` : "");
  }

  async function openBrowseModal(inputId) {
    browseTargetInputId = inputId;
    const field = document.getElementById(`inp_${inputId}`);
    browseCurrentPath = field?.value.trim() || projectRoot;
    await loadBrowseListing(browseCurrentPath);
    browseModal?.show();
  }

  async function loadBrowseListing(path) {
    const list = document.getElementById("browseFolderList");
    const pathLabel = document.getElementById("browseCurrentPath");
    list.innerHTML = `<div class="list-group-item text-secondary">Loading…</div>`;
    const res = await fetch(`/api/browse-folders?path=${encodeURIComponent(path || "")}`);
    const data = await res.json();
    if (!res.ok) {
      list.innerHTML = `<div class="list-group-item text-danger">${escapeHtml(data.error || "Could not list folder.")}</div>`;
      return;
    }
    browseCurrentPath = data.current;
    pathLabel.textContent = data.current;
    document.getElementById("browseUpBtn").disabled = !data.parent;

    if (!data.entries.length) {
      list.innerHTML = `<div class="list-group-item text-secondary">No subfolders.</div>`;
      return;
    }
    list.innerHTML = data.entries
      .map(
        (e) => `
      <button type="button" class="list-group-item list-group-item-action d-flex align-items-center gap-2" data-path="${escapeHtml(e.path)}">
        <i class="bi bi-folder-fill text-warning"></i>
        <span>${escapeHtml(e.name)}</span>
        ${e.has_children ? '<i class="bi bi-chevron-right ms-auto text-secondary"></i>' : ""}
      </button>`
      )
      .join("");
    list.querySelectorAll("[data-path]").forEach((btn) => {
      btn.addEventListener("click", () => loadBrowseListing(btn.dataset.path));
    });
    document.getElementById("browseUpBtn").onclick = () => data.parent && loadBrowseListing(data.parent);
  }

  document.getElementById("browseSelectBtn")?.addEventListener("click", () => {
    if (!browseTargetInputId) return;
    const field = document.getElementById(`inp_${browseTargetInputId}`);
    if (field) field.value = browseCurrentPath;
    browseModal?.hide();
    validateFolder(browseTargetInputId);
  });

  async function validateFolder(inputId) {
    const field = document.getElementById(`inp_${inputId}`);
    const hint = document.getElementById(`hint_${inputId}`);
    const inpDef = scriptMeta?.inputs?.find((i) => i.id === inputId);

    const res = await fetch("/api/validate-folder", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        path: field.value,
        require_images: !!inpDef?.require_images,
      }),
    });
    const data = await res.json();
    if (data.ok) {
      field.value = data.path;
      field.classList.add("is-valid");
      field.classList.remove("is-invalid");
      const msg = data.image_count
        ? `Valid — ${data.image_count} image(s) found.`
        : "Valid folder.";
      setFieldFeedback(hint, msg, "success");
      window.AppUI?.showToast("Folder validated.", "success");
    } else {
      field.classList.add("is-invalid");
      field.classList.remove("is-valid");
      setFieldFeedback(hint, data.error, "error");
      window.AppUI?.showToast(data.error, "danger");
    }
  }

  function validateClientSide() {
    const mode = sourceMode();
    for (const inp of scriptMeta.inputs || []) {
      if (inp.hidden) continue;
      if ((inp.id === "image_folder") && mode !== "folder") continue;
      if ((inp.id === "csv_folder") && mode !== "folder" && mode !== "all") continue;
      if ((inp.id === "image_file" || inp.id === "csv") && mode !== "file") continue;
      if (inp.id === "csv_list" && mode !== "list") continue;
      if (inp.show_when && !matchesShowWhen(inp.show_when)) continue;
      if (!isInputVisible(inp)) continue;

      const el = document.getElementById(`inp_${inp.id}`);
      if (!el) continue;

      const required =
        !!inp.required ||
        (inp.id === "image_folder" && mode === "folder") ||
        (inp.id === "csv_folder" && (mode === "folder" || mode === "all")) ||
        ((inp.id === "image_file" || inp.id === "csv") && mode === "file") ||
        (inp.id === "csv_list" && mode === "list");

      if (inp.type === "file") {
        const staged = document.getElementById(`staged_${inp.id}`)?.value;
        if (required && !staged) {
          window.AppUI?.showToast(`${inp.label} is required.`, "danger");
          return false;
        }
        continue;
      }
      if (inp.type === "boolean") continue;

      const value = inp.type === "select" ? el.value : el.value.trim();
      if (required && !value) {
        window.AppUI?.showToast(`${inp.label} is required.`, "danger");
        el.classList.add("is-invalid");
        el.focus();
        return false;
      }
    }

    const origin = document.getElementById("inp_origin")?.value;
    const dest = document.getElementById("inp_destination")?.value;
    if (origin && dest && origin === dest) {
      window.AppUI?.showToast("Origin and destination must be different.", "danger");
      return false;
    }
    const start = document.getElementById("inp_start_date")?.value;
    const end = document.getElementById("inp_end_date")?.value;
    if (start && end && end < start) {
      window.AppUI?.showToast("End date must be on or after start date.", "danger");
      return false;
    }
    return true;
  }

  function collectParameters() {
    const params = {};
    scriptMeta.inputs.forEach((inp) => {
      const el = document.getElementById(`inp_${inp.id}`);
      if (!el) {
        // Hidden / not rendered fields still run from scripts.json defaults.
        if (inp.hidden && inp.default != null && inp.default !== "") {
          params[inp.id] = inp.type === "boolean" ? !!inp.default : String(inp.default);
        }
        return;
      }
      if (inp.type === "boolean") params[inp.id] = el.checked;
      else if (inp.type === "file")
        params[inp.id] = document.getElementById(`staged_${inp.id}`)?.value || stagedFiles[inp.id] || "";
      else if (inp.type === "select") params[inp.id] = el.value;
      else if ((inp.input_type || "") === "date") {
        // Always send ISO (YYYY-MM-DD) from the calendar control.
        // Scripts that need another wire format (e.g. Booking API) normalize themselves.
        params[inp.id] = toIsoDate(el.value.trim()) || el.value.trim();
      } else params[inp.id] = el.value.trim();
    });
    return params;
  }

  function applyParameters(params) {
    if (!params) return;
    (scriptMeta.inputs || []).forEach((inp) => {
      const el = document.getElementById(`inp_${inp.id}`);
      const raw = params[inp.id];
      if (!el || raw == null || raw === "" || raw === "***") return;
      if (inp.input_type === "password") return; // never restore secrets
      if (inp.type === "boolean") el.checked = !!raw;
      else if (inp.type === "file") return;
      else if ((inp.input_type || "") === "date") el.value = toIsoDate(String(raw)) || "";
      else el.value = String(raw);
    });
    syncInputModeVisibility();
  }

  function maybeShowRestore() {
    try {
      const raw = localStorage.getItem(presetKey);
      if (!raw || !btnRestoreLast) return;
      btnRestoreLast.classList.remove("d-none");
      btnRestoreLast.onclick = () => {
        applyParameters(JSON.parse(raw));
        window.AppUI?.showToast("Restored last settings.", "success");
      };
    } catch (_) {
      /* ignore */
    }
  }

  function savePreset(params) {
    try {
      const safe = { ...params };
      delete safe.password;
      (scriptMeta.inputs || []).forEach((inp) => {
        if (inp.hidden) delete safe[inp.id];
      });
      localStorage.setItem(presetKey, JSON.stringify(safe));
    } catch (_) {
      /* ignore */
    }
  }

  function stages() {
    // Pipelines: each configured chain step is a stage.
    if (isPipeline && scriptMeta.steps?.length) {
      return scriptMeta.steps.map((s) => ({
        id: s.id,
        label: s.label || s.script_id,
        match: null,
      }));
    }
    // Optional: scripts may omit stages. Prefer explicit stages, else progress_hints, else none.
    if (scriptMeta.stages?.length) return scriptMeta.stages;
    const hints = scriptMeta.progress_hints || [];
    if (!hints.length) return [];
    return hints.map((h, i) => ({
      id: `s${i}`,
      label: (h.label || h.match || `Step ${i + 1}`).replace(/…$/, ""),
      match: h.match,
    }));
  }

  function setProgressPct(value, caption) {
    const clamped = Math.max(0, Math.min(100, Math.round(value)));
    progressValue = clamped;
    if (runProgress) {
      runProgress.style.width = `${clamped}%`;
      runProgress.setAttribute("aria-valuenow", String(clamped));
    }
    if (progressPct) progressPct.textContent = `${clamped}%`;
    if (caption && progressCaption) progressCaption.textContent = caption;
  }

  function stopProgressAnimation() {
    if (progressAnimRaf) {
      cancelAnimationFrame(progressAnimRaf);
      progressAnimRaf = null;
    }
    if (progressAnimResolve) {
      const resolve = progressAnimResolve;
      progressAnimResolve = null;
      resolve(false);
    }
  }

  function animateProgressTo(target, caption, durationMs = COMPLETE_PROGRESS_MS) {
    stopProgressAnimation();
    const from = progressValue;
    const to = Math.max(0, Math.min(100, Math.round(target)));
    if (caption && progressCaption) progressCaption.textContent = caption;
    // Keep a short beat even when already near 100 so the finish is visible.
    const duration = Math.max(durationMs, 1600);

    return new Promise((resolve) => {
      progressAnimResolve = resolve;
      const start = performance.now();
      const tick = (now) => {
        const t = Math.min(1, (now - start) / duration);
        const eased = 1 - (1 - t) ** 3;
        setProgressPct(from + (to - from) * eased, caption);
        if (t < 1) {
          progressAnimRaf = requestAnimationFrame(tick);
          return;
        }
        progressAnimRaf = null;
        progressAnimResolve = null;
        setProgressPct(to, caption);
        resolve(true);
      };
      progressAnimRaf = requestAnimationFrame(tick);
    });
  }

  function stopSoftProgress() {
    if (softProgressTimer) {
      clearInterval(softProgressTimer);
      softProgressTimer = null;
    }
  }

  function startSoftProgress() {
    stopSoftProgress();
    // Gentle climb with light jitter when a workflow has no stage map.
    softProgressTimer = setInterval(() => {
      if (progressValue >= 90) return;
      const bump = 1 + Math.floor(Math.random() * 3);
      setProgressPct(Math.min(90, progressValue + bump), "Working…");
    }, 1200);
  }

  function progressForStage(index, total) {
    if (!total) return progressValue;
    // Base on stage position, plus a small random offset so the bar feels alive.
    const base = ((index + 1) / total) * 88;
    const jitter = Math.floor(Math.random() * 5);
    return Math.min(92, Math.max(10, base + jitter));
  }

  function renderPipeline(stateIndex, options = {}) {
    const target = options.target || runPipeline;
    if (!target) return;
    const list = stages();
    const failedIndex = options.failedIndex;
    const emptyEl = options.target ? null : runPipelineEmpty;

    if (!list.length) {
      target.innerHTML = "";
      target.classList.add("d-none");
      emptyEl?.classList.remove("d-none");
      return;
    }

    target.classList.remove("d-none");
    emptyEl?.classList.add("d-none");
    const parts = [];
    list.forEach((stage, i) => {
      let cls = "run-stage";
      let dot = String(i + 1);
      if (failedIndex != null && i === failedIndex) {
        cls += " is-failed";
        dot = '<i class="bi bi-x"></i>';
      } else if (i < stateIndex) {
        cls += " is-done";
        dot = '<i class="bi bi-check"></i>';
      } else if (i === stateIndex) {
        cls += " is-active";
      }
      parts.push(`
        <div class="${cls}" data-stage="${escapeHtml(stage.id)}">
          <div class="run-stage-dot">${dot}</div>
          <div class="run-stage-label">${escapeHtml(stage.label)}</div>
        </div>`);
      if (i < list.length - 1) {
        let conn = "run-stage-connector";
        if (failedIndex != null) {
          if (i < failedIndex) conn += " is-done";
        } else if (i < stateIndex) {
          conn += " is-done";
        } else if (i === stateIndex) {
          conn += " is-active";
        }
        parts.push(`<div class="${conn}" aria-hidden="true"></div>`);
      }
    });
    target.innerHTML = parts.join("");
  }

  function collectedLogText() {
    const text = (liveLog?.textContent || "").trim();
    if (!text || text === "Waiting for log output…") return "";
    return text;
  }

  async function fetchFullLog(jobId) {
    if (!jobId) return "";
    try {
      const res = await fetch(`/output/${jobId}?since=0`);
      const data = await res.json();
      if (!res.ok) return "";
      const chunks = [];
      (data.stdout || []).forEach((l) => chunks.push(l));
      (data.stderr || []).forEach((l) => chunks.push(`[stderr] ${l}`));
      return chunks.join("\n").trim();
    } catch (_) {
      return "";
    }
  }

  function updateLogToggleLabel() {
    const toggle = document.getElementById("btnToggleLog");
    if (!toggle) return;
    const lines = collectedLogText() ? collectedLogText().split("\n").length : 0;
    if (logVisible) {
      toggle.textContent = "Hide details";
    } else if (lines) {
      toggle.textContent = `Show details (${lines})`;
    } else {
      toggle.textContent = "Show details";
    }
  }

  function looksLikeHardFailure(text) {
    const lower = String(text || "").toLowerCase();
    return STAGE_FAIL_MARKERS.some((m) => lower.includes(m));
  }

  function advanceStageFromText(text) {
    const list = stages();
    if (!text) return;
    if (!list.length) {
      // No stages configured — nudge progress on log activity.
      if (progressValue < 88) {
        setProgressPct(Math.min(88, progressValue + 1 + Math.floor(Math.random() * 4)), "Working…");
      }
      return;
    }
    // Freeze at the stage where the workflow actually stopped (e.g. login failed),
    // so cleanup lines like "Writing CSV" don't move the red X to Export.
    if (stageFrozen || looksLikeHardFailure(text)) {
      stageFrozen = true;
      return;
    }
    let best = activeStageIndex;
    list.forEach((stage, i) => {
      if (stage.match && text.includes(stage.match) && i > best) best = i;
    });
    if (best !== activeStageIndex) {
      activeStageIndex = best;
      renderPipeline(activeStageIndex);
      const label = list[best]?.label || "In progress";
      setProgressPct(progressForStage(best, list.length), label);
      stopSoftProgress();
    }
  }

  function lockUI(locked) {
    btnRun.disabled = locked;
    dynamicInputs.querySelectorAll("input, button, select").forEach((el) => {
      el.disabled = locked;
    });
  }

  function showRunningPanel() {
    setStep(2);
    panelConfigure.classList.add("d-none");
    panelRunning.classList.remove("d-none");
    panelDone.classList.add("d-none");
    const btnCancel = document.getElementById("btnCancel");
    if (btnCancel) btnCancel.disabled = false;
    activeStageIndex = 0;
    stageFrozen = false;
    const list = stages();
    renderPipeline(list.length ? 0 : -1);
    setProgressPct(list.length ? progressForStage(0, list.length) : 8, list[0]?.label || "Starting…");
    if (!list.length) startSoftProgress();
    else stopSoftProgress();
    logVisible = false;
    liveLog.classList.add("d-none");
    const toggle = document.getElementById("btnToggleLog");
    if (toggle) toggle.textContent = "Show details";
  }

  function showConfigurePanel() {
    panelConfigure.classList.remove("d-none");
    panelRunning.classList.add("d-none");
  }

  function formatParamDisplay(inp, value) {
    if (inp.type === "boolean") return value ? "Yes" : "No";
    if (inp.type === "file") {
      if (!value) return "—";
      const name = String(value).split("/").pop() || String(value);
      return name;
    }
    if (inp.input_type === "password" || /password|secret|token|key/i.test(inp.id || "")) {
      return value ? "••••••••" : "—";
    }
    if (value == null || value === "") return "—";
    return String(value);
  }

  function isInputVisible(inp) {
    const field = dynamicInputs?.querySelector(`[data-input-id="${inp.id}"]`);
    return !field || !field.classList.contains("d-none");
  }

  function buildConfirmRows(parameters) {
    return (scriptMeta.inputs || [])
      .filter((inp) => !inp.hidden && isInputVisible(inp))
      .map((inp) => {
        const value = parameters[inp.id];
        const shown = formatParamDisplay(inp, value);
        return `
          <div class="runner-confirm-row">
            <dt>${escapeHtml(inp.label || inp.id)}</dt>
            <dd title="${escapeHtml(shown)}">${escapeHtml(shown)}</dd>
          </div>`;
      })
      .join("");
  }

  function openConfirmModal() {
    if (!validateClientSide()) return;
    const parameters = collectParameters();
    const list = document.getElementById("runnerConfirmList");
    const title = document.getElementById("runnerConfirmTitle");
    if (list) list.innerHTML = buildConfirmRows(parameters) || "<p class=\"runner-confirm-empty\">No settings to review.</p>";
    if (title) {
      const runName = currentRunName();
      title.textContent = `Ready to launch “${runName}”?`;
    }
    confirmModal?.show();
  }

  async function startRun() {
    confirmModal?.hide();
    if (!validateClientSide()) return;

    const parameters = collectParameters();
    if (!String(parameters.run_name || "").trim()) {
      parameters.run_name = suggestedRunName();
    }
    savePreset(parameters);
    lockUI(true);
    showRunningPanel();
    liveLog.textContent = "";
    stdoutSince = 0;
    stderrSince = 0;
    const runLabel = parameters.run_name || scriptMeta.name;
    runningTitle.textContent = `Working on ${runLabel}…`;
    progressLabel.textContent = scriptMeta.name || "Starting…";
    runProgress.classList.add("progress-bar-striped", "progress-bar-animated");
    setProgressPct(8, "Starting…");

    const endpoint = isPipeline ? "/run-pipeline" : "/run-script";
    const payload = isPipeline
      ? { pipeline_id: pipelineId, parameters }
      : { script_id: scriptId, parameters };
    const res = await fetch(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (!res.ok) {
      stopSoftProgress();
      runProgress.classList.remove("progress-bar-animated", "progress-bar-striped");
      panelRunning.classList.add("d-none");
      panelDone.classList.remove("d-none");
      setStep(3);
      lockUI(false);
      showFailure(data.error || (isPipeline ? "Could not start pipeline." : "Could not start script."));
      return;
    }
    currentJobId = data.job_id;
    try {
      sessionStorage.setItem(activeJobKey, currentJobId);
    } catch {
      /* ignore */
    }
    const url = new URL(location.href);
    url.searchParams.set("job", currentJobId);
    history.replaceState(null, "", url);
    window.AppUI?.refreshActiveJobsBadge?.();
    if (data.queue_position) progressLabel.textContent = `Queued (#${data.queue_position})…`;
    pollTimer = setInterval(pollStatus, 700);
    pollStatus();
  }

  function rememberActiveJob(jobId) {
    currentJobId = jobId;
    try {
      sessionStorage.setItem(activeJobKey, jobId);
    } catch {
      /* ignore */
    }
  }

  function clearActiveJob() {
    try {
      sessionStorage.removeItem(activeJobKey);
    } catch {
      /* ignore */
    }
    const url = new URL(location.href);
    if (url.searchParams.has("job")) {
      url.searchParams.delete("job");
      history.replaceState(null, "", url);
    }
    window.AppUI?.refreshActiveJobsBadge?.();
  }

  function failureTips(message, status, stage) {
    const text = `${message || ""} ${stage || ""}`.toLowerCase();
    const tips = [];
    const add = (tip) => {
      if (!tips.includes(tip)) tips.push(tip);
    };
    if (status === "cancelled") add("This run was stopped early. Start it again when you are ready.");
    if (status === "timeout") add("The job hit its time limit. Try a smaller batch, or ask an admin to raise the timeout.");
    if (/login|password|credential|auth|unauthorized|401|403/.test(text)) {
      add("Check the username and password, then try again.");
    }
    if (/timeout|timed out|took too long/.test(text)) {
      add("Network or site may be slow — retry, or reduce the date range / file count.");
    }
    if (/no image|empty folder|folder|not found|does not exist|no files/.test(text)) {
      add("Confirm the input folder exists and contains the expected files.");
    }
    if (/connection|endpoint|refused|network|dns|unreachable|ssl/.test(text)) {
      add("Check network access and that the service URL is reachable.");
    }
    if (!tips.length) {
      add("Review the log, fix the cause, then Edit & run again.");
      add("If it keeps failing, copy the job id and share it with your admin.");
    }
    return tips.slice(0, 4);
  }

  async function resumeJob(jobId) {
    rememberActiveJob(jobId);
    lockUI(true);
    showRunningPanel();
    liveLog.textContent = "";
    stdoutSince = 0;
    stderrSince = 0;
    pollFailures = 0;
    runningTitle.textContent = `Reconnecting to ${scriptMeta?.name || "job"}…`;
    progressLabel.textContent = "Reconnecting…";
    runProgress.classList.add("progress-bar-striped", "progress-bar-animated");
    setProgressPct(12, "Reconnecting…");
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = setInterval(pollStatus, 700);
    pollStatus();
  }

  async function cancelRun() {
    if (!currentJobId) return;
    const btnCancel = document.getElementById("btnCancel");
    if (btnCancel) btnCancel.disabled = true;
    const res = await fetch(`/api/jobs/${currentJobId}/cancel`, { method: "POST" });
    const data = await res.json();
    if (!res.ok) {
      window.AppUI?.showToast(data.error || "Could not cancel.", "danger");
      if (btnCancel) btnCancel.disabled = false;
      return;
    }
    window.AppUI?.showToast("Cancel requested.", "warning");
    progressLabel.textContent = "Cancelling…";
  }

  async function runInBackground() {
    if (!currentJobId) return;
    const btn = document.getElementById("btnBackground");
    if (btn) btn.disabled = true;
    try {
      const res = await fetch(`/api/jobs/${encodeURIComponent(currentJobId)}/background`, {
        method: "POST",
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Could not move job to background");
      if (pollTimer) clearInterval(pollTimer);
      stopSoftProgress();
      try {
        sessionStorage.setItem(activeJobKey, currentJobId);
      } catch {
        /* ignore */
      }
      window.AppUI?.refreshActiveJobsBadge?.();
      window.AppUI?.showToast(
        "Running in background. We’ll notify you when it finishes.",
        "success"
      );
      location.href = "/jobs";
    } catch (err) {
      window.AppUI?.showToast(err.message || "Could not background this job.", "danger");
      if (btn) btn.disabled = false;
    }
  }

  function updateCancelVisibility(job) {
    const btnCancel = document.getElementById("btnCancel");
    const btnBackground = document.getElementById("btnBackground");
    const bgHint = document.getElementById("backgroundHint");
    const canCancel = Boolean(job?.cancellable);
    const isMine =
      Boolean(job?.started_by) &&
      String(job.started_by).toLowerCase() === String(currentUser).toLowerCase();
    const active = job?.status === "queued" || job?.status === "running";

    if (btnCancel) {
      btnCancel.classList.toggle("d-none", !canCancel);
      btnCancel.disabled = !canCancel;
    }
    if (btnBackground) {
      btnBackground.classList.toggle("d-none", !(active && isMine));
      btnBackground.disabled = !(active && isMine);
    }
    if (bgHint) {
      bgHint.classList.toggle("d-none", !(active && isMine));
    }

    let note = document.getElementById("cancelOwnerNote");
    if (!canCancel && active) {
      if (!note) {
        note = document.createElement("p");
        note.id = "cancelOwnerNote";
        note.className = "text-secondary small mb-0 mt-2";
        (btnCancel || btnBackground)?.parentElement?.appendChild(note);
      }
      const starter = job.started_by || "the starter";
      note.textContent =
        starter === currentUser
          ? "Cancel unavailable."
          : `Watching only — only ${starter} can stop this job.`;
      note.classList.remove("d-none");
    } else if (note) {
      note.classList.add("d-none");
    }
  }

  async function pollStatus() {
    if (!currentJobId) return;
    let res;
    let job;
    try {
      res = await fetch(
        `/status/${currentJobId}?since=${stdoutSince}&since_stderr=${stderrSince}`
      );
      job = await res.json();
    } catch {
      pollFailures += 1;
      progressLabel.textContent =
        pollFailures > 2
          ? "Connection issue — retrying… (open Active jobs if this persists)"
          : "Connection issue — retrying…";
      if (pollFailures >= 12) {
        clearInterval(pollTimer);
        stopSoftProgress();
        panelRunning.classList.add("d-none");
        panelDone.classList.remove("d-none");
        setStep(3);
        lockUI(false);
        showFailure(
          "Lost connection to the server while the job may still be running. Open Active jobs to reconnect.",
          { job_id: currentJobId, status: "failed" }
        );
      }
      return;
    }
    if (!res.ok) {
      if (res.status === 401) {
        clearInterval(pollTimer);
        stopSoftProgress();
        window.AppUI?.stopAuthPolling?.();
        if (!window.location.pathname.startsWith("/login")) {
          const next = encodeURIComponent(window.location.pathname + window.location.search);
          window.location.href = `/login?next=${next}`;
        }
        return;
      }
      pollFailures += 1;
      if (pollFailures >= 8) {
        clearInterval(pollTimer);
        stopSoftProgress();
        panelRunning.classList.add("d-none");
        panelDone.classList.remove("d-none");
        setStep(3);
        lockUI(false);
        showFailure(job?.error || "Could not load job status.", {
          job_id: currentJobId,
          status: "failed",
        });
      }
      return;
    }
    pollFailures = 0;

    appendLog(job.stdout, job.stderr);
    stdoutSince = job.stdout_line_count ?? stdoutSince;
    stderrSince = job.stderr_line_count ?? stderrSince;
    progressLabel.textContent = job.progress_label || "Working…";
    updateCancelVisibility(job);
    if (isPipeline && Array.isArray(job.steps)) {
      const idx =
        typeof job.active_step_index === "number"
          ? job.active_step_index
          : job.steps.findIndex((s) => s.status === "running");
      const stepIndex = idx >= 0 ? idx : Math.max(0, activeStageIndex);
      if (stepIndex !== activeStageIndex || job.status === "running") {
        activeStageIndex = stepIndex;
        const failedIdx = job.steps.findIndex((s) => s.status === "failed");
        renderPipeline(activeStageIndex, failedIdx >= 0 ? { failedIndex: failedIdx } : {});
        setProgressPct(
          progressForStage(activeStageIndex, stages().length),
          job.steps[activeStageIndex]?.label || job.progress_label || "Working…"
        );
      }
    } else {
      (job.stdout || []).forEach((line) => advanceStageFromText(line));
      advanceStageFromText(job.progress_label || "");
    }

    if (job.status === "running" || job.status === "queued") {
      if (job.status === "queued" && job.queue_position) {
        progressLabel.textContent = `Queued (#${job.queue_position})…`;
      }
      return;
    }

    clearInterval(pollTimer);
    stopSoftProgress();
    clearActiveJob();
    runProgress.classList.remove("progress-bar-animated", "progress-bar-striped");

    if (job.status === "success") {
      // Stay on Working and ease the bar to 100% so completion is easy to see.
      const finishedJobId = job.job_id;
      renderPipeline(stages().length);
      if (runningTitle) runningTitle.textContent = `${scriptMeta.name} is finishing…`;
      progressLabel.textContent = "Almost done…";
      const finished = await animateProgressTo(100, "Complete", COMPLETE_PROGRESS_MS);
      if (!finished || currentJobId !== finishedJobId) return;
      setStep(3);
      panelRunning.classList.add("d-none");
      panelDone.classList.remove("d-none");
      lockUI(false);
      showSuccess(job);
    } else {
      stopProgressAnimation();
      setStep(3);
      panelRunning.classList.add("d-none");
      panelDone.classList.remove("d-none");
      lockUI(false);
      const list = stages();
      const failAt = list.length ? Math.max(0, activeStageIndex) : -1;
      if (failAt >= 0) {
        setProgressPct(progressForStage(failAt, list.length), "Failed");
        renderPipeline(failAt, { failedIndex: failAt });
      } else {
        setProgressPct(Math.max(progressValue, 35), "Failed");
      }
      showFailure(job.error_message || `Script ${job.status}.`, job);
    }
  }

  function appendLog(stdoutLines = [], stderrLines = []) {
    const chunks = [];
    stdoutLines.forEach((l) => chunks.push(l));
    stderrLines.forEach((l) => chunks.push(`[stderr] ${l}`));
    if (!chunks.length) return;
    const existing = collectedLogText();
    liveLog.textContent = existing ? `${existing}\n${chunks.join("\n")}` : chunks.join("\n");
    liveLog.scrollTop = liveLog.scrollHeight;
    updateLogToggleLabel();
  }

  function resolveResultUi(job) {
    return (
      job?.result_ui ||
      scriptMeta?.result_ui || {
        mode: "run_summary",
        title: "Results",
        lead: "Preview or download the generated file.",
        stats: "ocr",
      }
    );
  }

  function normalizeOutputs(job) {
    const ui = resolveResultUi(job);
    const fromApi = Array.isArray(job.outputs) ? job.outputs : [];
    let list = fromApi;
    if (!list.length && (job.report_path || job.report_url)) {
      list = [
        {
          id: "0",
          index: 0,
          label: job.report_path ? job.report_path.split("/").pop() : "Output",
          kind: job.report_is_csv ? "csv" : "file",
          filename: job.report_path ? job.report_path.split("/").pop() : "output",
          rows: job.summary?.rows_total ?? null,
          source_image: "",
          view_url: job.report_url || `/report/${job.job_id}/view`,
          download_url: `/report/${job.job_id}/download`,
        },
      ];
    }
    // API / booking: one results CSV — never surface OCR-style multi-image tabs.
    if (ui.mode === "run_summary" && list.length > 1) return list.slice(0, 1);
    return list;
  }

  function renderOutputFiles(job, outputs) {
    const list = document.getElementById("outputFileList");
    const reportLead = document.querySelector("#reportPanel .report-panel-lead");
    if (!list) return;
    if (reportLead) {
      reportLead.textContent = outputs.length
        ? "Open a file, or download it"
        : "No files were saved";
    }
    list.innerHTML = outputs
      .map((item) => {
        const label = item.label || item.filename || "File";
        const filename = item.filename || "";
        const metaParts = [];
        if (filename && filename !== label) metaParts.push(filename);
        if (item.rows != null && item.rows !== "") {
          metaParts.push(`${item.rows} row${Number(item.rows) === 1 ? "" : "s"}`);
        }
        if (!metaParts.length) metaParts.push("Open in a new tab");
        return `
          <li class="rd-file-item">
            <a class="rd-file-open" href="${escapeHtml(item.view_url || item.download_url)}"
               target="_blank" rel="noopener" title="Open ${escapeHtml(label)}">
              <span class="rd-file-icon" aria-hidden="true"><i class="bi bi-file-earmark-text"></i></span>
              <span class="rd-file-copy">
                <span class="rd-file-name">${escapeHtml(label)}</span>
                <span class="rd-file-meta">${escapeHtml(metaParts.join(" · "))}</span>
              </span>
            </a>
            <a class="rd-file-download" href="${escapeHtml(item.download_url)}"
               download="${escapeHtml(item.filename || "output")}"
               title="Download ${escapeHtml(label)}">
              Download <i class="bi bi-download" aria-hidden="true"></i>
            </a>
          </li>`;
      })
      .join("");
  }

  function showSuccess(job) {
    document.getElementById("resultSuccess").classList.remove("d-none");
    document.getElementById("resultError").classList.add("d-none");
    const dl = document.getElementById("summaryStats");
    const ui = resolveResultUi(job);
    const outputs = normalizeOutputs(job);
    const booked = job.summary?.booked;
    const posted = job.summary?.posted;
    const failed = job.summary?.failed;
    const rowsTotal = job.summary?.rows_total;
    const sub = document.getElementById("successSub");
    if (sub) {
      if (ui.stats === "collect") {
        sub.textContent = `${rowsTotal ?? "—"} row${String(rowsTotal) === "1" ? "" : "s"} collected · ${job.duration_seconds ?? "—"}s`;
      } else if (ui.stats === "api" && (posted != null || failed != null)) {
        sub.textContent = `${posted ?? "—"} posted · ${failed ?? "—"} didn’t · ${job.duration_seconds ?? "—"}s`;
      } else if (ui.stats === "booking" && (booked != null || failed != null)) {
        sub.textContent = `${booked ?? "—"} booked · ${failed ?? "—"} didn’t · ${job.duration_seconds ?? "—"}s`;
      } else if (ui.mode === "per_source") {
        const fileCount = outputs.length || job.images_processed || 0;
        sub.textContent = `${fileCount} file${fileCount === 1 ? "" : "s"} · ${job.duration_seconds ?? "—"}s`;
      } else {
        sub.textContent = `Finished in ${job.duration_seconds ?? "—"}s`;
      }
    }

    const facts = [`<div class="runner-summary-item"><span>Took</span><strong>${job.duration_seconds ?? "—"}s</strong></div>`];
    if (ui.stats === "collect") {
      facts.push(`<div class="runner-summary-item"><span>Rows</span><strong>${rowsTotal ?? 0}</strong></div>`);
      facts.push(`<div class="runner-summary-item"><span>Files</span><strong>${outputs.length || 0}</strong></div>`);
    } else if (ui.stats === "api" && (posted != null || failed != null)) {
      facts.push(`<div class="runner-summary-item"><span>Posted</span><strong>${posted ?? 0}</strong></div>`);
      facts.push(`<div class="runner-summary-item"><span>Didn’t post</span><strong>${failed ?? "—"}</strong></div>`);
    } else if (ui.stats === "booking" && (booked != null || failed != null)) {
      facts.push(`<div class="runner-summary-item"><span>Booked</span><strong>${booked ?? 0}</strong></div>`);
      facts.push(`<div class="runner-summary-item"><span>Didn’t book</span><strong>${failed ?? "—"}</strong></div>`);
    } else if (ui.mode === "per_source") {
      facts.push(`<div class="runner-summary-item"><span>Files</span><strong>${outputs.length}</strong></div>`);
      if (rowsTotal != null) {
        facts.push(`<div class="runner-summary-item"><span>Rows</span><strong>${rowsTotal}</strong></div>`);
      }
    } else if (rowsTotal != null) {
      facts.push(`<div class="runner-summary-item"><span>Rows</span><strong>${rowsTotal}</strong></div>`);
    }
    facts.push(
      `<div class="runner-summary-item"><span>History</span><strong><a href="/history/${job.job_id}">Open details</a></strong></div>`
    );
    dl.innerHTML = facts.join("");

    const reportPanel = document.getElementById("reportPanel");
    const reportTitle = reportPanel?.querySelector(".runner-panel-title, h3");
    if (reportTitle) reportTitle.textContent = ui.title || "Your files";
    if (outputs.length) {
      reportPanel.classList.remove("d-none");
      renderOutputFiles(job, outputs);
    } else {
      reportPanel.classList.add("d-none");
    }

    document.getElementById("btnOpenFolder").onclick = async () => {
      const res = await fetch(`/api/open-output/${job.job_id}`, { method: "POST" });
      const data = await res.json();
      if (!data.ok) window.AppUI?.showToast(data.error || "Could not open folder.", "danger");
      else window.AppUI?.showToast("Opened output folder.", "success");
    };
    document.getElementById("btnRunAgain").onclick = resetRunner;
    window.AppUI?.showToast(
      ui.mode === "per_source" && outputs.length > 1
        ? `Workflow completed — ${outputs.length} files ready.`
        : "Workflow completed.",
      "success"
    );
  }

  async function showFailure(message, job) {
    document.getElementById("resultSuccess").classList.add("d-none");
    document.getElementById("resultError").classList.remove("d-none");
    document.getElementById("errorMessage").textContent = message;
    document.getElementById("reportPanel").classList.add("d-none");

    const list = stages();
    const failAt = list.length ? Math.max(0, activeStageIndex) : -1;
    const stageName = failAt >= 0 ? list[failAt]?.label || "" : "";
    const stageLabel = document.getElementById("errorStageLabel");
    if (stageLabel) {
      stageLabel.textContent = stageName ? `Stopped at: ${stageName}` : "Workflow stopped before completion.";
    }

    const tipsEl = document.getElementById("errorTips");
    if (tipsEl) {
      const tips = failureTips(message, job?.status, stageName);
      tipsEl.innerHTML = tips.map((t) => `<li>${escapeHtml(t)}</li>`).join("");
      tipsEl.hidden = !tips.length;
    }

    const jobId = job?.job_id || currentJobId;
    const refEl = document.getElementById("errorJobRef");
    if (refEl) {
      if (jobId) {
        refEl.textContent = `Job reference: ${jobId}`;
        refEl.classList.remove("d-none");
      } else {
        refEl.classList.add("d-none");
      }
    }
    const historyBtn = document.getElementById("btnErrorHistory");
    if (historyBtn) {
      if (jobId) {
        historyBtn.href = `/history/${jobId}`;
        historyBtn.classList.remove("d-none");
      } else {
        historyBtn.classList.add("d-none");
      }
    }
    const copyBtn = document.getElementById("btnCopyJobId");
    if (copyBtn) {
      if (jobId) {
        copyBtn.classList.remove("d-none");
        copyBtn.onclick = async () => {
          try {
            await navigator.clipboard.writeText(jobId);
            window.AppUI?.showToast("Job id copied.", "success");
          } catch {
            window.AppUI?.showToast("Could not copy job id.", "danger");
          }
        };
      } else {
        copyBtn.classList.add("d-none");
      }
    }

    const errorPipeline = document.getElementById("errorPipeline");
    if (errorPipeline) {
      if (failAt >= 0) {
        errorPipeline.classList.remove("d-none");
        renderPipeline(failAt, {
          target: errorPipeline,
          failedIndex: failAt,
        });
      } else {
        errorPipeline.innerHTML = "";
        errorPipeline.classList.add("d-none");
      }
    }

    // Prefer the accumulated live log — final poll only returns new lines since last read.
    let logText = collectedLogText();
    if (!logText && job) {
      const delta = [...(job.stdout || []), ...(job.stderr || []).map((l) => `[stderr] ${l}`)]
        .join("\n")
        .trim();
      logText = delta;
    }
    if (!logText && job?.job_id) {
      logText = await fetchFullLog(job.job_id);
    }
    const errorLog = document.getElementById("errorLog");
    errorLog.textContent = logText || "No log output was captured for this run.";
    errorLog.classList.remove("d-none");
    const toggleErr = document.getElementById("btnToggleErrorLog");
    if (toggleErr) {
      toggleErr.textContent = "Hide details";
      toggleErr.onclick = () => {
        const hidden = errorLog.classList.toggle("d-none");
        toggleErr.textContent = hidden ? "Show details" : "Hide details";
      };
    }

    document.getElementById("btnRetryAfterError").onclick = resetRunner;
    window.AppUI?.showToast(message, "danger");
  }

  function resetRunner() {
    stopSoftProgress();
    stopProgressAnimation();
    clearActiveJob();
    setStep(1);
    panelDone.classList.add("d-none");
    showConfigurePanel();
    currentJobId = null;
    pollFailures = 0;
    runProgress.classList.add("progress-bar-striped", "progress-bar-animated");
    setProgressPct(8, "Starting…");
  }

  btnRun.addEventListener("click", openConfirmModal);
  document.getElementById("btnConfirmLaunch")?.addEventListener("click", startRun);
  document.getElementById("btnCancel")?.addEventListener("click", cancelRun);
  document.getElementById("btnBackground")?.addEventListener("click", runInBackground);
  document.getElementById("btnToggleLog")?.addEventListener("click", () => {
    logVisible = !logVisible;
    liveLog.classList.toggle("d-none", !logVisible);
    if (logVisible && !collectedLogText()) {
      liveLog.textContent = "Waiting for log output…";
    }
    updateLogToggleLabel();
  });

  try {
    const metaUrl = isPipeline ? `/api/pipelines/${pipelineId}` : `/api/scripts/${scriptId}`;
    const res = await fetch(metaUrl);
    scriptMeta = await res.json();
    if (!res.ok) {
      window.AppUI?.showToast(scriptMeta.error || "Could not load workflow.", "danger");
      return;
    }
    if (!res.ok) throw new Error(scriptMeta.error);
    buildInputs(scriptMeta.inputs || []);
    setStep(1);

    if (scriptMeta.enabled === false) {
      if (btnRun) btnRun.disabled = true;
    }

    const params = new URLSearchParams(location.search);
    const reuseId = params.get("reuse");
    if (reuseId) {
      const runRes = await fetch(`/api/runs/${reuseId}`);
      const runData = await runRes.json();
      if (runRes.ok && runData.parameters) {
        applyParameters(runData.parameters);
        ensureRunNameDefault();
        window.AppUI?.showToast("Loaded settings from previous run.", "success");
      }
    }

    let resumeId = params.get("job");
    if (!resumeId) {
      try {
        resumeId = sessionStorage.getItem(activeJobKey);
      } catch {
        resumeId = null;
      }
    }
    if (resumeId) {
      const statusRes = await fetch(`/status/${resumeId}?since=0&since_stderr=0`);
      const statusJob = await statusRes.json().catch(() => ({}));
      if (statusRes.ok && ["queued", "running"].includes(statusJob.status)) {
        window.AppUI?.showToast("Resumed live monitoring.", "success");
        await resumeJob(resumeId);
      } else {
        clearActiveJob();
      }
    }
  } catch (err) {
    window.AppUI?.showToast("Could not load workflow configuration.", "danger");
  }
});
