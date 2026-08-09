document.addEventListener("DOMContentLoaded", () => {
  const root = document.getElementById("settingsApp");
  if (!root) return;

  let scripts = [];
  let pipelines = [];
  let deleted = [];
  let selectedScriptId = null;
  let selectedPipelineId = null;
  let selectedBackupId = null;
  let dragStepIndex = null;

  const workflowList = document.getElementById("workflowList");
  const pipelineList = document.getElementById("pipelineList");
  const deletedList = document.getElementById("deletedList");
  const workflowEditor = document.getElementById("workflowEditor");
  const pipelineEditor = document.getElementById("pipelineEditor");
  const deletedEditor = document.getElementById("deletedEditor");

  const ICON_OPTIONS = [
    "bi-lightning-charge",
    "bi-image",
    "bi-ticket-perforated",
    "bi-cloud-download",
    "bi-diagram-3",
    "bi-robot",
    "bi-file-earmark-spreadsheet",
    "bi-gear",
  ];

  const BADGE_OPTIONS = [
    "Active",
    "Draft",
    "Sample",
    "Pipeline",
    "Beta",
    "New",
  ];

  function toast(message, type = "success") {
    window.AppUI?.showToast(message, type);
  }

  function escapeHtml(value) {
    const div = document.createElement("div");
    div.textContent = value == null ? "" : String(value);
    return div.innerHTML;
  }

  async function api(url, options = {}) {
    const res = await fetch(url, {
      headers: { "Content-Type": "application/json", ...(options.headers || {}) },
      ...options,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || "Request failed");
    return data;
  }

  function formatDeletedWhen(iso) {
    if (!iso) return "—";
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return String(iso);
    return d.toLocaleString(undefined, {
      year: "numeric",
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  }

  async function loadConfig() {
    const [data, deletedData] = await Promise.all([
      api("/api/settings/config"),
      api("/api/settings/deleted"),
    ]);
    scripts = data.scripts || [];
    pipelines = data.pipelines || [];
    deleted = deletedData.deleted || [];
    renderLists();
    if (selectedScriptId) {
      const still = scripts.find((s) => s.id === selectedScriptId);
      if (still) renderWorkflowEditor(still);
      else {
        selectedScriptId = null;
        workflowEditor.innerHTML = emptyState("bi-sliders", "Select a workflow to edit, or create a new one.");
      }
    }
    if (selectedPipelineId) {
      const still = pipelines.find((p) => p.id === selectedPipelineId);
      if (still) renderPipelineEditor(still);
      else {
        selectedPipelineId = null;
        pipelineEditor.innerHTML = emptyState(
          "bi-diagram-3",
          "Select a pipeline to open the flow canvas, or create a new chain."
        );
      }
    }
    if (selectedBackupId) {
      const still = deleted.find((d) => d.backup_id === selectedBackupId);
      if (still) renderDeletedDetail(still.backup_id);
      else {
        selectedBackupId = null;
        deletedEditor.innerHTML = emptyState(
          "bi-trash3",
          "Select a deleted workflow to view details, restore, or purge."
        );
      }
    }
  }

  function emptyState(icon, text) {
    return `<div class="settings-empty"><i class="bi ${icon}" aria-hidden="true"></i><p>${escapeHtml(text)}</p></div>`;
  }

  function renderLists() {
    workflowList.innerHTML = scripts
      .map((s) => {
        const active = s.id === selectedScriptId ? " is-active" : "";
        const on = s.enabled !== false;
        return `
          <button type="button" class="settings-list-item${active}" data-select-script="${escapeHtml(s.id)}">
            <span class="settings-list-icon"><i class="bi ${escapeHtml(s.icon || "bi-lightning-charge")}" aria-hidden="true"></i></span>
            <span class="settings-list-copy">
              <strong>${escapeHtml(s.name || s.id)}</strong>
              <small>${escapeHtml(s.id)}${s.badge ? ` · ${escapeHtml(s.badge)}` : ""}</small>
            </span>
            <span class="settings-list-switch ${on ? "is-on" : ""}" data-toggle-script="${escapeHtml(s.id)}" title="${on ? "Enabled" : "Disabled"}" role="switch" aria-checked="${on}">
              <span></span>
            </span>
          </button>`;
      })
      .join("") || `<p class="settings-list-empty">No workflows yet.</p>`;

    pipelineList.innerHTML = pipelines
      .map((p) => {
        const active = p.id === selectedPipelineId ? " is-active" : "";
        const on = p.enabled !== false;
        const steps = (p.steps || []).length;
        return `
          <button type="button" class="settings-list-item${active}" data-select-pipeline="${escapeHtml(p.id)}">
            <span class="settings-list-icon"><i class="bi ${escapeHtml(p.icon || "bi-diagram-3")}" aria-hidden="true"></i></span>
            <span class="settings-list-copy">
              <strong>${escapeHtml(p.name || p.id)}</strong>
              <small>${escapeHtml(p.id)} · ${steps} step${steps === 1 ? "" : "s"}</small>
            </span>
            <span class="settings-list-switch ${on ? "is-on" : ""}" data-toggle-pipeline="${escapeHtml(p.id)}" title="${on ? "Enabled" : "Disabled"}" role="switch" aria-checked="${on}">
              <span></span>
            </span>
          </button>`;
      })
      .join("") || `<p class="settings-list-empty">No pipelines yet. Create a chain from existing workflows.</p>`;

    if (deletedList) {
      deletedList.innerHTML = deleted
        .map((item) => {
          const active = item.backup_id === selectedBackupId ? " is-active" : "";
          return `
            <button type="button" class="settings-list-item${active}" data-select-deleted="${escapeHtml(item.backup_id)}">
              <span class="settings-list-icon"><i class="bi ${escapeHtml(item.icon || "bi-trash3")}" aria-hidden="true"></i></span>
              <span class="settings-list-copy">
                <strong>${escapeHtml(item.name || item.id)}</strong>
                <small>${escapeHtml(item.id)} · ${escapeHtml(formatDeletedWhen(item.deleted_at))}</small>
              </span>
            </button>`;
        })
        .join("") || `<p class="settings-list-empty">No deleted workflows yet.</p>`;
    }
  }

  async function renderDeletedDetail(backupId) {
    selectedBackupId = backupId;
    renderLists();
    deletedEditor.innerHTML = `<div class="settings-empty"><p>Loading…</p></div>`;
    try {
      const data = await api(`/api/settings/deleted/${encodeURIComponent(backupId)}`);
      const meta = data.meta || {};
      const workflow = data.workflow || {};
      const inputs = Array.isArray(workflow.inputs) ? workflow.inputs : [];
      deletedEditor.innerHTML = `
        <div class="settings-form">
          <div class="settings-form-head">
            <div>
              <p class="settings-form-kicker">Deleted workflow</p>
              <h2>${escapeHtml(meta.name || meta.id || backupId)}</h2>
              <p class="settings-form-path">Backup <code>${escapeHtml(meta.path || data.path || "")}</code></p>
            </div>
          </div>
          <div class="settings-grid">
            <div class="settings-field">
              <span>ID</span>
              <strong class="settings-readonly">${escapeHtml(meta.id || "—")}</strong>
            </div>
            <div class="settings-field">
              <span>Deleted</span>
              <strong class="settings-readonly">${escapeHtml(formatDeletedWhen(meta.deleted_at))}</strong>
            </div>
            <div class="settings-field">
              <span>Badge</span>
              <strong class="settings-readonly">${escapeHtml(meta.badge || "—")}</strong>
            </div>
            <div class="settings-field">
              <span>Parameters</span>
              <strong class="settings-readonly">${escapeHtml(String(meta.input_count ?? inputs.length))}</strong>
            </div>
            <div class="settings-field settings-field-full">
              <span>Script path</span>
              <strong class="settings-readonly"><code>${escapeHtml(meta.script || "—")}</code></strong>
            </div>
            <div class="settings-field settings-field-full">
              <span>Description</span>
              <strong class="settings-readonly">${escapeHtml(meta.description || "—")}</strong>
            </div>
            <div class="settings-field settings-field-full">
              <span>Files backed up</span>
              <strong class="settings-readonly">${
                meta.has_files
                  ? escapeHtml(meta.copied_from || "Yes")
                  : "No Python files were found to back up"
              }</strong>
            </div>
          </div>
          ${
            inputs.length
              ? `<div class="settings-inputs-block">
                  <div class="settings-steps-head"><h3>Saved parameters</h3></div>
                  <ul class="settings-deleted-params">
                    ${inputs
                      .map(
                        (inp) =>
                          `<li><strong>${escapeHtml(inp.label || inp.id)}</strong><small>${escapeHtml(
                            inp.id
                          )}${inp.cli?.flag ? ` · ${escapeHtml(inp.cli.flag)}` : ""}</small></li>`
                      )
                      .join("")}
                  </ul>
                </div>`
              : ""
          }
          <div class="settings-form-actions">
            <button type="button" class="btn settings-save-btn" id="btnRestoreDeleted">Restore workflow</button>
            <button type="button" class="btn btn-outline-danger ms-auto" id="btnPurgeDeleted">Delete forever</button>
          </div>
        </div>`;

      document.getElementById("btnRestoreDeleted")?.addEventListener("click", async () => {
        if (
          !confirm(
            `Restore “${meta.name || meta.id}”?\n\nThis puts it back in scripts.json and restores files under scripts/.`
          )
        ) {
          return;
        }
        try {
          await api(`/api/settings/deleted/${encodeURIComponent(backupId)}/restore`, {
            method: "POST",
          });
          toast("Workflow restored.");
          selectedBackupId = null;
          deletedEditor.innerHTML = emptyState(
            "bi-trash3",
            "Select a deleted workflow to view details, restore, or purge."
          );
          await loadConfig();
          const restored = scripts.find((s) => s.id === meta.id);
          if (restored) {
            root.querySelector('.settings-tab[data-tab="workflows"]')?.click();
            renderWorkflowEditor(restored);
          }
        } catch (err) {
          toast(err.message, "danger");
        }
      });

      document.getElementById("btnPurgeDeleted")?.addEventListener("click", async () => {
        if (
          !confirm(
            `Permanently delete backup “${meta.name || meta.id}”?\n\nThis cannot be undone.`
          )
        ) {
          return;
        }
        try {
          await api(`/api/settings/deleted/${encodeURIComponent(backupId)}`, {
            method: "DELETE",
          });
          toast("Backup removed forever.");
          selectedBackupId = null;
          deletedEditor.innerHTML = emptyState(
            "bi-trash3",
            "Select a deleted workflow to view details, restore, or purge."
          );
          await loadConfig();
        } catch (err) {
          toast(err.message, "danger");
        }
      });
    } catch (err) {
      deletedEditor.innerHTML = emptyState("bi-exclamation-triangle", err.message);
    }
  }

  function iconSelect(name, selected) {
    return `
      <select class="form-select form-select-app" name="${name}" id="${name}">
        ${ICON_OPTIONS.map(
          (icon) =>
            `<option value="${icon}"${icon === selected ? " selected" : ""}>${icon.replace("bi-", "")}</option>`
        ).join("")}
      </select>`;
  }

  function badgeSelect(name, selected) {
    const current = String(selected || "").trim();
    const options = [...BADGE_OPTIONS];
    if (current && !options.includes(current)) options.unshift(current);
    return `
      <select class="form-select form-select-app" name="${name}" id="${name}">
        <option value=""${!current ? " selected" : ""}>None</option>
        ${options
          .map(
            (badge) =>
              `<option value="${escapeHtml(badge)}"${badge === current ? " selected" : ""}>${escapeHtml(badge)}</option>`
          )
          .join("")}
      </select>`;
  }

  function slugifyId(value) {
    return String(value || "")
      .trim()
      .toLowerCase()
      .replace(/[^a-z0-9_]+/g, "_")
      .replace(/^_+|_+$/g, "")
      .replace(/^([^a-z])/, "f_$1");
  }

  function optionsToText(options) {
    if (!Array.isArray(options) || !options.length) return "";
    return options
      .map((opt) => {
        if (typeof opt === "string") return opt;
        const value = opt?.value ?? "";
        const label = opt?.label ?? value;
        return label && label !== value ? `${value} | ${label}` : String(value);
      })
      .join("\n");
  }

  function textToOptions(text) {
    return String(text || "")
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean)
      .map((line) => {
        const parts = line.split("|").map((p) => p.trim());
        if (parts.length >= 2) return { value: parts[0], label: parts.slice(1).join(" | ") };
        return { value: parts[0], label: parts[0] };
      });
  }

  function defaultInput(partial = {}) {
    const id = partial.id || "new_field";
    return {
      id,
      label: partial.label || "New field",
      type: partial.type || "text",
      required: !!partial.required,
      hidden: !!partial.hidden,
      default: partial.default ?? "",
      group: partial.group || "Basics",
      width: partial.width || "half",
      help: partial.help || "",
      placeholder: partial.placeholder || "",
      input_type: partial.input_type || "text",
      accept: partial.accept || "",
      options: Array.isArray(partial.options) ? partial.options : [],
      cli: {
        flag: partial.cli?.flag || `--${String(id).replace(/_/g, "-")}`,
        position: partial.cli?.position ?? null,
        is_switch: !!partial.cli?.is_switch,
      },
    };
  }

  function inputCardHtml(inp, index) {
    const type = inp.type || "text";
    const showOptions = type === "select";
    const showInputType = type === "text";
    const showAccept = type === "file";
    const showSwitch = type === "boolean";
    const defaultInputType =
      type === "text" && (inp.input_type || "text") === "password" ? "password" : "text";
    const badges = [
      inp.hidden ? `<span class="settings-input-badge">Hidden on run form</span>` : "",
      (inp.input_type || "") === "password" ? `<span class="settings-input-badge is-secret">Secret</span>` : "",
    ]
      .filter(Boolean)
      .join("");
    return `
      <details class="settings-input-card${inp.hidden ? " is-hidden-field" : ""}" data-input-index="${index}" ${index === 0 ? "open" : ""}>
        <summary class="settings-input-summary">
          <span class="settings-input-handle" title="Drag to reorder" draggable="true" data-drag-input="${index}">
            <i class="bi bi-grip-vertical" aria-hidden="true"></i>
          </span>
          <span class="settings-input-title">
            <strong>${escapeHtml(inp.label || inp.id || "Field")}</strong>
            <small>${escapeHtml(inp.id || "—")} · ${escapeHtml(type)}${inp.cli?.flag ? ` · ${escapeHtml(inp.cli.flag)}` : ""}${badges ? ` · ${badges}` : ""}</small>
          </span>
          <button type="button" class="settings-input-remove" data-remove-input="${index}" title="Remove field" aria-label="Remove field">
            <i class="bi bi-trash3" aria-hidden="true"></i>
          </button>
        </summary>
        <div class="settings-input-body">
          <div class="settings-grid">
            <label class="settings-field">
              <span>Field ID</span>
              <input class="form-control form-control-app" data-inp="id" value="${escapeHtml(inp.id || "")}" pattern="[a-z][a-z0-9_]*" required>
            </label>
            <label class="settings-field">
              <span>Label</span>
              <input class="form-control form-control-app" data-inp="label" value="${escapeHtml(inp.label || "")}" required>
            </label>
            <label class="settings-field">
              <span>Type</span>
              <select class="form-select form-select-app" data-inp="type">
                ${["text", "select", "boolean", "folder", "file"]
                  .map((t) => `<option value="${t}"${t === type ? " selected" : ""}>${t}</option>`)
                  .join("")}
              </select>
            </label>
            <label class="settings-field">
              <span>Group</span>
              <input class="form-control form-control-app" data-inp="group" value="${escapeHtml(inp.group || "Basics")}" placeholder="Basics">
            </label>
            <label class="settings-field">
              <span>Width</span>
              <select class="form-select form-select-app" data-inp="width">
                <option value="half"${(inp.width || "half") === "half" ? " selected" : ""}>Half</option>
                <option value="full"${inp.width === "full" ? " selected" : ""}>Full</option>
              </select>
            </label>
            <label class="settings-field ${showInputType ? "" : "d-none"}" data-show-for="text">
              <span>Input type</span>
              <select class="form-select form-select-app" data-inp="input_type">
                ${["text", "password", "number", "date"]
                  .map(
                    (t) =>
                      `<option value="${t}"${(inp.input_type || "text") === t ? " selected" : ""}>${t}</option>`
                  )
                  .join("")}
              </select>
            </label>
            <label class="settings-field">
              <span>${inp.hidden ? "Stored value (used on every run)" : "Default"}</span>
              <input class="form-control form-control-app" data-inp="default" type="${defaultInputType}" value="${escapeHtml(
                inp.default == null || typeof inp.default === "object" ? "" : String(inp.default)
              )}" placeholder="${type === "boolean" ? "true / false" : inp.hidden ? "Set in Settings only" : ""}" autocomplete="off">
            </label>
            <label class="settings-field">
              <span>CLI flag</span>
              <input class="form-control form-control-app" data-inp="cli_flag" value="${escapeHtml(
                inp.cli?.flag || ""
              )}" placeholder="--my-flag">
            </label>
            <label class="settings-field ${showAccept ? "" : "d-none"}" data-show-for="file">
              <span>Accept</span>
              <input class="form-control form-control-app" data-inp="accept" value="${escapeHtml(inp.accept || "")}" placeholder=".csv,.png">
            </label>
            <label class="settings-field settings-field-full">
              <span>Help text</span>
              <input class="form-control form-control-app" data-inp="help" value="${escapeHtml(inp.help || "")}">
            </label>
            <label class="settings-field settings-field-full">
              <span>Placeholder</span>
              <input class="form-control form-control-app" data-inp="placeholder" value="${escapeHtml(inp.placeholder || "")}">
            </label>
            <label class="settings-field settings-field-full ${showOptions ? "" : "d-none"}" data-show-for="select">
              <span>Options <small>(one per line: value | label)</small></span>
              <textarea class="form-control form-control-app" data-inp="options" rows="4" placeholder="KTM | Kathmandu">${escapeHtml(
                optionsToText(inp.options)
              )}</textarea>
            </label>
          </div>
          <div class="settings-input-flags">
            <label><input type="checkbox" data-inp="required" ${inp.required ? "checked" : ""}> Required</label>
            <label><input type="checkbox" data-inp="hidden" ${inp.hidden ? "checked" : ""}> Hidden on run form (Settings only)</label>
            <label class="${showSwitch ? "" : "d-none"}" data-show-for="boolean">
              <input type="checkbox" data-inp="cli_switch" ${inp.cli?.is_switch ? "checked" : ""}> CLI switch (--flag / --no-flag)
            </label>
          </div>
        </div>
      </details>`;
  }

  function collectInputsFromDom(rootEl) {
    const cards = [...rootEl.querySelectorAll(".settings-input-card")];
    const inputs = [];
    const seen = new Set();
    for (const card of cards) {
      const get = (key) => card.querySelector(`[data-inp="${key}"]`);
      const id = slugifyId(get("id")?.value || "");
      const label = String(get("label")?.value || "").trim();
      const type = String(get("type")?.value || "text");
      if (!id || !label) {
        throw new Error("Each parameter needs an ID and label.");
      }
      if (seen.has(id)) throw new Error(`Duplicate field ID: ${id}`);
      seen.add(id);

      const flag = String(get("cli_flag")?.value || "").trim();
      let defaultValue = String(get("default")?.value ?? "");
      if (type === "boolean") {
        const low = defaultValue.toLowerCase();
        defaultValue = low === "true" || low === "1" || low === "yes";
      }

      const inp = {
        id,
        label,
        type,
        required: !!get("required")?.checked,
        hidden: !!get("hidden")?.checked,
        default: defaultValue,
        group: String(get("group")?.value || "Basics").trim() || "Basics",
        width: String(get("width")?.value || "half"),
        help: String(get("help")?.value || "").trim(),
        placeholder: String(get("placeholder")?.value || "").trim(),
        cli: {
          flag: flag || null,
          position: null,
          is_switch: type === "boolean" ? !!get("cli_switch")?.checked : false,
        },
      };

      if (type === "text") {
        inp.input_type = String(get("input_type")?.value || "text");
      }
      if (type === "file") {
        const accept = String(get("accept")?.value || "").trim();
        if (accept) inp.accept = accept;
      }
      if (type === "select") {
        const options = textToOptions(get("options")?.value || "");
        if (!options.length) throw new Error(`Select field “${label}” needs at least one option.`);
        inp.options = options;
      }

      // Drop empty defaults for cleaner JSON (except boolean false).
      if (inp.default === "" && type !== "boolean") delete inp.default;
      if (!inp.help) delete inp.help;
      if (!inp.placeholder) delete inp.placeholder;
      if (!inp.cli.flag) {
        delete inp.cli;
      } else if (!inp.cli.is_switch) {
        delete inp.cli.is_switch;
        if (inp.cli.position == null) delete inp.cli.position;
      }

      inputs.push(inp);
    }
    return inputs;
  }

  function parseFixedArgs(text) {
    const tokens = String(text || "")
      .split(/\s+/)
      .map((t) => t.trim())
      .filter(Boolean);
    return tokens;
  }

  function renderWorkflowEditor(script) {
    selectedScriptId = script.id;
    renderLists();
    let workingInputs = (Array.isArray(script.inputs) ? script.inputs : []).map((inp) =>
      defaultInput(inp)
    );
    let dragInputIndex = null;

    workflowEditor.innerHTML = `
      <form class="settings-form" id="workflowForm">
        <div class="settings-form-head">
          <div>
            <p class="settings-form-kicker">Workflow</p>
            <h2>${escapeHtml(script.name || script.id)}</h2>
            <p class="settings-form-path"><code>${escapeHtml(script.script || "")}</code></p>
          </div>
        </div>

        <div class="settings-grid">
          <label class="settings-field settings-field-full">
            <span>Name</span>
            <input class="form-control form-control-app" name="name" value="${escapeHtml(script.name || "")}" required>
          </label>
          <label class="settings-field settings-field-full">
            <span>Description</span>
            <textarea class="form-control form-control-app" name="description" rows="3">${escapeHtml(script.description || "")}</textarea>
          </label>
          <label class="settings-field">
            <span>Icon</span>
            ${iconSelect("icon", script.icon || "bi-lightning-charge")}
          </label>
          <label class="settings-field">
            <span>Badge</span>
            ${badgeSelect("badge", script.badge || "Active")}
          </label>
          <label class="settings-field">
            <span>Timeout (seconds)</span>
            <input class="form-control form-control-app" type="number" min="30" name="timeout_seconds" value="${escapeHtml(script.timeout_seconds ?? 1800)}">
          </label>
          <label class="settings-field">
            <span>Script path</span>
            <input class="form-control form-control-app" name="script" value="${escapeHtml(script.script || "")}" required>
          </label>
          <label class="settings-field settings-field-full">
            <span>Form note</span>
            <input class="form-control form-control-app" name="form_note" value="${escapeHtml(script.form_note || "")}" placeholder="Short tip shown above the form">
          </label>
          <label class="settings-field settings-field-full">
            <span>Fixed CLI args <small>(always appended, space-separated)</small></span>
            <input class="form-control form-control-app" name="fixed_args" value="${escapeHtml(
              (script.fixed_args || []).join(" ")
            )}" placeholder="--output my_output">
          </label>
        </div>

        <div class="settings-inputs-block">
          <div class="settings-steps-head">
            <h3>Form parameters</h3>
            <span class="settings-steps-hint">${workingInputs.length} field${workingInputs.length === 1 ? "" : "s"}</span>
          </div>
          <p class="settings-inputs-lead">These become runner form fields and map to CLI flags for your Python script.</p>
          <div id="workflowInputsList" class="settings-inputs-list">
            ${
              workingInputs.length
                ? workingInputs.map((inp, i) => inputCardHtml(inp, i)).join("")
                : `<p class="settings-list-empty" id="inputsEmpty">No parameters yet. Add a field to collect user input.</p>`
            }
          </div>
          <div class="settings-add-input">
            <button type="button" class="btn btn-outline-secondary btn-sm" id="btnAddInput">
              <i class="bi bi-plus-lg me-1" aria-hidden="true"></i>Add parameter
            </button>
          </div>
        </div>

        <div class="settings-form-actions">
          <button type="submit" class="btn settings-save-btn">Save changes</button>
          <a class="btn btn-outline-secondary" href="/runner/${encodeURIComponent(script.id)}">Open runner</a>
          <button type="button" class="btn btn-outline-danger ms-auto" id="btnDeleteWorkflow">Delete</button>
        </div>
      </form>`;

    const listEl = document.getElementById("workflowInputsList");

    function refreshInputCards(openIndex = null) {
      if (!workingInputs.length) {
        listEl.innerHTML = `<p class="settings-list-empty" id="inputsEmpty">No parameters yet. Add a field to collect user input.</p>`;
      } else {
        listEl.innerHTML = workingInputs.map((inp, i) => inputCardHtml(inp, i)).join("");
        if (openIndex != null) {
          listEl.querySelector(`[data-input-index="${openIndex}"]`)?.setAttribute("open", "");
        }
      }
      const hint = workflowEditor.querySelector(".settings-inputs-block .settings-steps-hint");
      if (hint) {
        hint.textContent = `${workingInputs.length} field${workingInputs.length === 1 ? "" : "s"}`;
      }
      bindInputEditor();
    }

    function readCard(card) {
      const get = (key) => card.querySelector(`[data-inp="${key}"]`);
      const type = String(get("type")?.value || "text");
      return defaultInput({
        id: get("id")?.value,
        label: get("label")?.value,
        type,
        required: !!get("required")?.checked,
        hidden: !!get("hidden")?.checked,
        default: get("default")?.value,
        group: get("group")?.value,
        width: get("width")?.value,
        help: get("help")?.value,
        placeholder: get("placeholder")?.value,
        input_type: get("input_type")?.value,
        accept: get("accept")?.value,
        options: textToOptions(get("options")?.value || ""),
        cli: {
          flag: get("cli_flag")?.value,
          is_switch: !!get("cli_switch")?.checked,
        },
      });
    }

    function bindInputEditor() {
      listEl.querySelectorAll(".settings-input-card").forEach((card) => {
        const typeSel = card.querySelector('[data-inp="type"]');
        const syncTypeUi = () => {
          const type = typeSel?.value || "text";
          card.querySelectorAll("[data-show-for]").forEach((el) => {
            el.classList.toggle("d-none", el.dataset.showFor !== type);
          });
        };
        typeSel?.addEventListener("change", syncTypeUi);
        syncTypeUi();

        const inputTypeSel = card.querySelector('[data-inp="input_type"]');
        const defaultInput = card.querySelector('[data-inp="default"]');
        const syncDefaultType = () => {
          if (!defaultInput) return;
          const t = inputTypeSel?.value || "text";
          defaultInput.type = t === "password" ? "password" : "text";
        };
        inputTypeSel?.addEventListener("change", syncDefaultType);
        syncDefaultType();

        card.querySelector("[data-remove-input]")?.addEventListener("click", (e) => {
          e.preventDefault();
          e.stopPropagation();
          const idx = Number(card.dataset.inputIndex);
          // Capture current edits before remove
          workingInputs = [...listEl.querySelectorAll(".settings-input-card")].map(readCard);
          workingInputs.splice(idx, 1);
          refreshInputCards();
        });

        const handle = card.querySelector("[data-drag-input]");
        handle?.addEventListener("dragstart", (e) => {
          dragInputIndex = Number(card.dataset.inputIndex);
          card.classList.add("is-dragging");
          e.dataTransfer.effectAllowed = "move";
          // Persist current values before reorder
          workingInputs = [...listEl.querySelectorAll(".settings-input-card")].map(readCard);
        });
        handle?.addEventListener("dragend", () => {
          card.classList.remove("is-dragging");
          dragInputIndex = null;
          listEl.querySelectorAll(".settings-input-card").forEach((n) => n.classList.remove("is-over"));
        });
        card.addEventListener("dragover", (e) => {
          e.preventDefault();
          card.classList.add("is-over");
        });
        card.addEventListener("dragleave", () => card.classList.remove("is-over"));
        card.addEventListener("drop", (e) => {
          e.preventDefault();
          card.classList.remove("is-over");
          const to = Number(card.dataset.inputIndex);
          if (dragInputIndex == null || Number.isNaN(to) || dragInputIndex === to) return;
          const [moved] = workingInputs.splice(dragInputIndex, 1);
          workingInputs.splice(to, 0, moved);
          refreshInputCards(to);
        });
      });
    }

    bindInputEditor();

    document.getElementById("btnAddInput")?.addEventListener("click", () => {
      workingInputs = listEl.querySelectorAll(".settings-input-card").length
        ? [...listEl.querySelectorAll(".settings-input-card")].map(readCard)
        : workingInputs;
      let n = workingInputs.length + 1;
      let id = `field_${n}`;
      while (workingInputs.some((i) => i.id === id)) {
        n += 1;
        id = `field_${n}`;
      }
      workingInputs.push(
        defaultInput({
          id,
          label: `Field ${n}`,
          cli: { flag: `--${id.replace(/_/g, "-")}` },
        })
      );
      refreshInputCards(workingInputs.length - 1);
    });

    document.getElementById("workflowForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(e.target);
      let inputs;
      try {
        inputs = collectInputsFromDom(listEl);
      } catch (err) {
        toast(err.message, "danger");
        return;
      }
      const patch = {
        name: String(fd.get("name") || "").trim(),
        description: String(fd.get("description") || "").trim(),
        icon: String(fd.get("icon") || "bi-lightning-charge"),
        badge: String(fd.get("badge") || "").trim(),
        timeout_seconds: Number(fd.get("timeout_seconds") || 1800),
        script: String(fd.get("script") || "").trim(),
        form_note: String(fd.get("form_note") || "").trim(),
        fixed_args: parseFixedArgs(fd.get("fixed_args")),
        inputs,
      };
      try {
        await api(`/api/settings/scripts/${encodeURIComponent(script.id)}`, {
          method: "PATCH",
          body: JSON.stringify(patch),
        });
        toast("Workflow saved.");
        await loadConfig();
        const updated = scripts.find((s) => s.id === script.id);
        if (updated) renderWorkflowEditor(updated);
      } catch (err) {
        toast(err.message, "danger");
      }
    });

    document.getElementById("btnDeleteWorkflow")?.addEventListener("click", async () => {
      const scriptPath = script.script || `scripts/${script.id}/`;
      if (
        !confirm(
          `Delete workflow “${script.name || script.id}”?\n\nA backup is kept under backups/workflows/. Live files under ${scriptPath} are removed. You can restore later from Settings → Deleted.`
        )
      ) {
        return;
      }
      try {
        const result = await api(`/api/settings/scripts/${encodeURIComponent(script.id)}`, {
          method: "DELETE",
        });
        selectedScriptId = null;
        workflowEditor.innerHTML = emptyState("bi-sliders", "Select a workflow to edit, or create a new one.");
        const backupPath = result.backup?.path;
        toast(backupPath ? `Workflow deleted. Backup: ${backupPath}` : "Workflow deleted.");
        await loadConfig();
      } catch (err) {
        toast(err.message, "danger");
      }
    });
  }

  function renderCreateWorkflow() {
    selectedScriptId = null;
    renderLists();
    workflowEditor.innerHTML = `
      <form class="settings-form" id="createWorkflowForm">
        <div class="settings-form-head">
          <div>
            <p class="settings-form-kicker">New workflow</p>
            <h2>Create workflow</h2>
            <p class="settings-form-path">Creates a draft Python stub and adds it to scripts.json.</p>
          </div>
        </div>
        <div class="settings-grid">
          <label class="settings-field">
            <span>ID</span>
            <input class="form-control form-control-app" name="id" placeholder="my_workflow" pattern="[a-z][a-z0-9_]*" required>
          </label>
          <label class="settings-field">
            <span>Name</span>
            <input class="form-control form-control-app" name="name" placeholder="My workflow" required>
          </label>
          <label class="settings-field settings-field-full">
            <span>Description</span>
            <textarea class="form-control form-control-app" name="description" rows="3" placeholder="What this automation does"></textarea>
          </label>
        </div>
        <div class="settings-form-actions">
          <button type="submit" class="btn settings-save-btn">Create workflow</button>
        </div>
      </form>`;

    document.getElementById("createWorkflowForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(e.target);
      try {
        const data = await api("/api/settings/scripts", {
          method: "POST",
          body: JSON.stringify({
            id: String(fd.get("id") || "").trim(),
            name: String(fd.get("name") || "").trim(),
            description: String(fd.get("description") || "").trim(),
          }),
        });
        toast("Workflow created.");
        scripts = data.scripts || scripts;
        selectedScriptId = String(fd.get("id") || "").trim();
        await loadConfig();
        const created = scripts.find((s) => s.id === selectedScriptId);
        if (created) renderWorkflowEditor(created);
      } catch (err) {
        toast(err.message, "danger");
      }
    });
  }

  function makePipelineStepId(scriptId, existing) {
    const base = String(scriptId || "step").replace(/[^a-z0-9_]+/g, "_") || "step";
    let stepId = base;
    let n = 2;
    while (existing.some((s) => s.id === stepId)) {
      stepId = `${base}_${n++}`;
    }
    return stepId;
  }

  function paletteNodesHtml() {
    if (!scripts.length) {
      return `<p class="n8n-palette-empty">No workflows available. Create one in the Workflows tab first.</p>`;
    }
    return scripts
      .map((s) => {
        const disabled = s.enabled === false;
        return `
          <button type="button" class="n8n-palette-node${disabled ? " is-disabled" : ""}" data-add-script="${escapeHtml(s.id)}" draggable="true" title="Click or drag onto the canvas">
            <span class="n8n-palette-icon"><i class="bi ${escapeHtml(s.icon || "bi-lightning-charge")}" aria-hidden="true"></i></span>
            <span class="n8n-palette-copy">
              <strong>${escapeHtml(s.name || s.id)}</strong>
              <small>${escapeHtml(s.id)}${disabled ? " · off" : ""}</small>
            </span>
            <span class="n8n-palette-plus" aria-hidden="true"><i class="bi bi-plus-lg"></i></span>
          </button>`;
      })
      .join("");
  }

  function flowJoinHtml(insertIndex) {
    return `
      <div class="n8n-join" data-insert-at="${insertIndex}">
        <div class="n8n-wire" aria-hidden="true"></div>
        <button type="button" class="n8n-insert" data-insert-at="${insertIndex}" title="Insert step here" aria-label="Insert step here">
          <i class="bi bi-plus-lg" aria-hidden="true"></i>
        </button>
        <div class="n8n-wire" aria-hidden="true"></div>
      </div>`;
  }

  function flowNodesHtml(steps, selectedIndex) {
    const start = `
      <div class="n8n-node n8n-node-start" aria-hidden="false">
        <span class="n8n-port n8n-port-out" aria-hidden="true"></span>
        <div class="n8n-node-body">
          <span class="n8n-node-icon"><i class="bi bi-play-fill" aria-hidden="true"></i></span>
          <div class="n8n-node-copy">
            <strong>Start</strong>
            <small>Trigger</small>
          </div>
        </div>
      </div>`;

    const end = `
      <div class="n8n-node n8n-node-end">
        <span class="n8n-port n8n-port-in" aria-hidden="true"></span>
        <div class="n8n-node-body">
          <span class="n8n-node-icon"><i class="bi bi-flag-fill" aria-hidden="true"></i></span>
          <div class="n8n-node-copy">
            <strong>Done</strong>
            <small>Output</small>
          </div>
        </div>
      </div>`;

    if (!steps.length) {
      return `
        ${start}
        ${flowJoinHtml(0)}
        <div class="n8n-drop-hint" data-insert-at="0">
          <i class="bi bi-plus-circle" aria-hidden="true"></i>
          <p>Drop a workflow here, or click one in the node list</p>
        </div>
        ${flowJoinHtml(0)}
        ${end}`;
    }

    const middles = steps
      .map((step, index) => {
        const script = scripts.find((s) => s.id === step.script_id);
        const selected = selectedIndex === index ? " is-selected" : "";
        const blocked = script?.enabled === false ? " is-blocked" : "";
        return `
          ${flowJoinHtml(index)}
          <div class="n8n-node n8n-node-step${selected}${blocked}" draggable="true" data-step-index="${index}" tabindex="0" role="button" aria-pressed="${selected ? "true" : "false"}">
            <span class="n8n-port n8n-port-in" aria-hidden="true"></span>
            <span class="n8n-port n8n-port-out" aria-hidden="true"></span>
            <button type="button" class="n8n-node-remove" data-remove-step="${index}" title="Remove step" aria-label="Remove step">
              <i class="bi bi-x-lg" aria-hidden="true"></i>
            </button>
            <div class="n8n-node-body">
              <span class="n8n-node-index">${index + 1}</span>
              <span class="n8n-node-icon"><i class="bi ${escapeHtml(script?.icon || "bi-lightning-charge")}" aria-hidden="true"></i></span>
              <div class="n8n-node-copy">
                <strong>${escapeHtml(step.label || script?.name || step.script_id)}</strong>
                <small>${escapeHtml(step.script_id)}${script?.enabled === false ? " · disabled" : ""}</small>
              </div>
            </div>
          </div>`;
      })
      .join("");

    return `
      ${start}
      ${middles}
      ${flowJoinHtml(steps.length)}
      ${end}`;
  }

  function renderPipelineCanvas({ mode, pipeline }) {
    const isCreate = mode === "create";
    selectedPipelineId = isCreate ? null : pipeline.id;
    renderLists();

    const steps = isCreate ? [] : (Array.isArray(pipeline.steps) ? pipeline.steps.map((s) => ({ ...s })) : []);
    let workingSteps = steps;
    let selectedIndex = workingSteps.length ? 0 : null;
    let insertTarget = null;

    pipelineEditor.innerHTML = `
      <form class="n8n-pipe" id="pipelineForm">
        <header class="n8n-toolbar">
          <div class="n8n-toolbar-copy">
            <p class="n8n-toolbar-kicker">${isCreate ? "New pipeline" : "Pipeline canvas"}</p>
            <div class="n8n-toolbar-title-row">
              ${
                isCreate
                  ? `<input class="form-control form-control-app n8n-id-input" name="id" placeholder="pipeline_id" pattern="[a-z][a-z0-9_]*" required title="lowercase letters, numbers, underscores">`
                  : `<span class="n8n-toolbar-id">${escapeHtml(pipeline.id)}</span>`
              }
              <input class="form-control form-control-app n8n-name-input" name="name" value="${escapeHtml(isCreate ? "" : pipeline.name || "")}" placeholder="Pipeline name" required>
            </div>
          </div>
          <div class="n8n-toolbar-actions">
            <button type="submit" class="btn settings-save-btn">${isCreate ? "Create pipeline" : "Save pipeline"}</button>
            ${
              isCreate
                ? ""
                : `<a class="btn btn-outline-secondary btn-sm" href="/pipeline/${encodeURIComponent(pipeline.id)}">Open runner</a>
                   <button type="button" class="btn btn-outline-danger btn-sm" id="btnDeletePipeline">Delete</button>`
            }
          </div>
        </header>

        <div class="n8n-body">
          <aside class="n8n-palette">
            <div class="n8n-palette-head">
              <h3>Nodes</h3>
              <p>Click or drag onto the flow</p>
            </div>
            <div class="n8n-palette-list" id="pipelinePalette">${paletteNodesHtml()}</div>
          </aside>

          <div class="n8n-canvas-shell">
            <div class="n8n-canvas-hint">
              <span><i class="bi bi-grip-vertical" aria-hidden="true"></i> Drag nodes to reorder</span>
              <span><i class="bi bi-plus-lg" aria-hidden="true"></i> Plus on a wire to insert</span>
            </div>
            <div class="n8n-canvas" id="pipelineCanvas">
              <div class="n8n-flow" id="pipelineFlow">${flowNodesHtml(workingSteps, selectedIndex)}</div>
            </div>
          </div>
        </div>

        <div class="n8n-inspector" id="pipelineInspector"></div>

        <details class="n8n-meta">
          <summary>Pipeline details</summary>
          <div class="settings-grid n8n-meta-grid">
            <label class="settings-field settings-field-full">
              <span>Description</span>
              <textarea class="form-control form-control-app" name="description" rows="2" placeholder="What this chain does">${escapeHtml(isCreate ? "" : pipeline.description || "")}</textarea>
            </label>
            <label class="settings-field">
              <span>Icon</span>
              ${iconSelect("icon", isCreate ? "bi-diagram-3" : pipeline.icon || "bi-diagram-3")}
            </label>
            <label class="settings-field">
              <span>Badge</span>
              ${badgeSelect("badge", isCreate ? "Pipeline" : pipeline.badge || "Pipeline")}
            </label>
          </div>
        </details>
      </form>`;

    const flowEl = document.getElementById("pipelineFlow");
    const inspectorEl = document.getElementById("pipelineInspector");
    const canvasEl = document.getElementById("pipelineCanvas");

    function refreshFlow() {
      flowEl.innerHTML = flowNodesHtml(workingSteps, selectedIndex);
      bindFlowInteractions();
      renderInspector();
    }

    function addStep(scriptId, atIndex) {
      if (!scriptId) return;
      const script = scripts.find((s) => s.id === scriptId);
      const step = {
        id: makePipelineStepId(scriptId, workingSteps),
        script_id: scriptId,
        label: script?.name || scriptId,
      };
      const idx = typeof atIndex === "number" ? atIndex : workingSteps.length;
      workingSteps.splice(Math.max(0, Math.min(idx, workingSteps.length)), 0, step);
      selectedIndex = Math.max(0, Math.min(idx, workingSteps.length - 1));
      insertTarget = null;
      refreshFlow();
    }

    function renderInspector() {
      if (selectedIndex == null || !workingSteps[selectedIndex]) {
        inspectorEl.innerHTML = `
          <div class="n8n-inspector-empty">
            <i class="bi bi-cursor" aria-hidden="true"></i>
            <p>Select a node to edit its label, or add workflows from the left.</p>
          </div>`;
        return;
      }
      const step = workingSteps[selectedIndex];
      const script = scripts.find((s) => s.id === step.script_id);
      inspectorEl.innerHTML = `
        <div class="n8n-inspector-card">
          <div class="n8n-inspector-head">
            <span class="n8n-inspector-icon"><i class="bi ${escapeHtml(script?.icon || "bi-lightning-charge")}" aria-hidden="true"></i></span>
            <div>
              <p class="n8n-toolbar-kicker">Step ${selectedIndex + 1}</p>
              <strong>${escapeHtml(script?.name || step.script_id)}</strong>
            </div>
          </div>
          <label class="settings-field">
            <span>Display label</span>
            <input class="form-control form-control-app" id="stepLabelInput" value="${escapeHtml(step.label || "")}" placeholder="${escapeHtml(script?.name || step.script_id)}">
          </label>
          <p class="n8n-inspector-meta">Workflow ID <code>${escapeHtml(step.script_id)}</code>${script?.enabled === false ? " · currently disabled" : ""}</p>
        </div>`;
      document.getElementById("stepLabelInput")?.addEventListener("input", (e) => {
        workingSteps[selectedIndex].label = e.target.value;
        const title = flowEl.querySelector(`[data-step-index="${selectedIndex}"] .n8n-node-copy strong`);
        if (title) title.textContent = e.target.value || script?.name || step.script_id;
      });
    }

    function bindFlowInteractions() {
      flowEl.querySelectorAll(".n8n-node-step").forEach((el) => {
        el.addEventListener("click", (e) => {
          if (e.target.closest(".n8n-node-remove")) return;
          selectedIndex = Number(el.dataset.stepIndex);
          refreshFlow();
        });
        el.addEventListener("dragstart", (e) => {
          dragStepIndex = Number(el.dataset.stepIndex);
          el.classList.add("is-dragging");
          e.dataTransfer.effectAllowed = "move";
          e.dataTransfer.setData("application/x-helix-step", String(dragStepIndex));
        });
        el.addEventListener("dragend", () => {
          el.classList.remove("is-dragging");
          dragStepIndex = null;
          flowEl.querySelectorAll(".n8n-join, .n8n-drop-hint, .n8n-node-step").forEach((n) => n.classList.remove("is-over"));
        });
        el.addEventListener("dragover", (e) => {
          e.preventDefault();
          el.classList.add("is-over");
        });
        el.addEventListener("dragleave", () => el.classList.remove("is-over"));
        el.addEventListener("drop", (e) => {
          e.preventDefault();
          el.classList.remove("is-over");
          const to = Number(el.dataset.stepIndex);
          const scriptId = e.dataTransfer.getData("application/x-helix-script");
          if (scriptId) {
            addStep(scriptId, to);
            return;
          }
          if (dragStepIndex == null || Number.isNaN(to) || dragStepIndex === to) return;
          const [moved] = workingSteps.splice(dragStepIndex, 1);
          workingSteps.splice(to, 0, moved);
          selectedIndex = to;
          refreshFlow();
        });
      });

      flowEl.querySelectorAll("[data-remove-step]").forEach((btn) => {
        btn.addEventListener("click", (e) => {
          e.stopPropagation();
          const idx = Number(btn.dataset.removeStep);
          workingSteps.splice(idx, 1);
          if (!workingSteps.length) selectedIndex = null;
          else if (selectedIndex == null) selectedIndex = 0;
          else if (selectedIndex >= workingSteps.length) selectedIndex = workingSteps.length - 1;
          else if (selectedIndex > idx) selectedIndex -= 1;
          refreshFlow();
        });
      });

      flowEl.querySelectorAll(".n8n-join, .n8n-drop-hint").forEach((el) => {
        el.addEventListener("dragover", (e) => {
          e.preventDefault();
          el.classList.add("is-over");
        });
        el.addEventListener("dragleave", () => el.classList.remove("is-over"));
        el.addEventListener("drop", (e) => {
          e.preventDefault();
          el.classList.remove("is-over");
          const at = Number(el.dataset.insertAt);
          const scriptId = e.dataTransfer.getData("application/x-helix-script");
          if (scriptId) {
            addStep(scriptId, at);
            return;
          }
          if (dragStepIndex == null || Number.isNaN(at)) return;
          const [moved] = workingSteps.splice(dragStepIndex, 1);
          const target = dragStepIndex < at ? at - 1 : at;
          workingSteps.splice(target, 0, moved);
          selectedIndex = target;
          refreshFlow();
        });
      });

      flowEl.querySelectorAll(".n8n-insert").forEach((btn) => {
        btn.addEventListener("click", (e) => {
          e.stopPropagation();
          insertTarget = Number(btn.dataset.insertAt);
          const palette = document.getElementById("pipelinePalette");
          palette?.classList.add("is-picking");
          toast("Pick a workflow from the Nodes list to insert.");
        });
      });
    }

    document.getElementById("pipelinePalette")?.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-add-script]");
      if (!btn) return;
      const at = insertTarget == null ? workingSteps.length : insertTarget;
      addStep(btn.dataset.addScript, at);
      document.getElementById("pipelinePalette")?.classList.remove("is-picking");
    });

    document.getElementById("pipelinePalette")?.querySelectorAll("[data-add-script]").forEach((btn) => {
      btn.addEventListener("dragstart", (e) => {
        e.dataTransfer.effectAllowed = "copy";
        e.dataTransfer.setData("application/x-helix-script", btn.dataset.addScript);
        canvasEl?.classList.add("is-dropping");
      });
      btn.addEventListener("dragend", () => canvasEl?.classList.remove("is-dropping"));
    });

    canvasEl?.addEventListener("dragover", (e) => {
      if ([...e.dataTransfer.types].includes("application/x-helix-script")) {
        e.preventDefault();
      }
    });
    canvasEl?.addEventListener("drop", (e) => {
      const scriptId = e.dataTransfer.getData("application/x-helix-script");
      if (!scriptId) return;
      e.preventDefault();
      addStep(scriptId, workingSteps.length);
      canvasEl.classList.remove("is-dropping");
    });

    bindFlowInteractions();
    renderInspector();

    document.getElementById("pipelineForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      if (!workingSteps.length) {
        toast("Add at least one workflow node.", "danger");
        return;
      }
      const fd = new FormData(e.target);
      const payload = {
        name: String(fd.get("name") || "").trim(),
        description: String(fd.get("description") || "").trim(),
        icon: String(fd.get("icon") || "bi-diagram-3"),
        badge: String(fd.get("badge") || "Pipeline").trim(),
        steps: workingSteps.map((s) => ({
          id: s.id,
          script_id: s.script_id,
          label: String(s.label || "").trim() || scripts.find((x) => x.id === s.script_id)?.name || s.script_id,
        })),
      };
      try {
        if (isCreate) {
          const id = String(fd.get("id") || "").trim();
          await api("/api/settings/pipelines", {
            method: "POST",
            body: JSON.stringify({ id, ...payload }),
          });
          toast("Pipeline created.");
          selectedPipelineId = id;
          await loadConfig();
          const created = pipelines.find((p) => p.id === id);
          if (created) renderPipelineEditor(created);
        } else {
          await api(`/api/settings/pipelines/${encodeURIComponent(pipeline.id)}`, {
            method: "PATCH",
            body: JSON.stringify(payload),
          });
          toast("Pipeline saved.");
          await loadConfig();
        }
      } catch (err) {
        toast(err.message, "danger");
      }
    });

    document.getElementById("btnDeletePipeline")?.addEventListener("click", async () => {
      if (!confirm(`Delete pipeline “${pipeline.name || pipeline.id}”?`)) return;
      try {
        await api(`/api/settings/pipelines/${encodeURIComponent(pipeline.id)}`, { method: "DELETE" });
        selectedPipelineId = null;
        pipelineEditor.innerHTML = emptyState(
          "bi-diagram-3",
          "Select a pipeline to open the flow canvas, or create a new chain."
        );
        toast("Pipeline deleted.");
        await loadConfig();
      } catch (err) {
        toast(err.message, "danger");
      }
    });
  }

  function renderPipelineEditor(pipeline) {
    renderPipelineCanvas({ mode: "edit", pipeline });
  }

  function renderCreatePipeline() {
    renderPipelineCanvas({ mode: "create", pipeline: { id: "", name: "", description: "", icon: "bi-diagram-3", badge: "Pipeline", steps: [] } });
  }

  root.querySelectorAll(".settings-tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      root.querySelectorAll(".settings-tab").forEach((t) => {
        t.classList.toggle("is-active", t === tab);
        t.setAttribute("aria-selected", t === tab ? "true" : "false");
      });
      root.querySelectorAll(".settings-layout").forEach((panel) => {
        panel.classList.toggle("d-none", panel.dataset.panel !== tab.dataset.tab);
      });
    });
  });

  workflowList.addEventListener("click", async (e) => {
    const toggle = e.target.closest("[data-toggle-script]");
    if (toggle) {
      e.preventDefault();
      e.stopPropagation();
      const id = toggle.dataset.toggleScript;
      const script = scripts.find((s) => s.id === id);
      if (!script) return;
      try {
        await api(`/api/settings/scripts/${encodeURIComponent(id)}`, {
          method: "PATCH",
          body: JSON.stringify({ enabled: script.enabled === false }),
        });
        toast(script.enabled === false ? "Workflow enabled." : "Workflow disabled.");
        await loadConfig();
      } catch (err) {
        toast(err.message, "danger");
      }
      return;
    }
    const item = e.target.closest("[data-select-script]");
    if (!item) return;
    const script = scripts.find((s) => s.id === item.dataset.selectScript);
    if (script) renderWorkflowEditor(script);
  });

  pipelineList.addEventListener("click", async (e) => {
    const toggle = e.target.closest("[data-toggle-pipeline]");
    if (toggle) {
      e.preventDefault();
      e.stopPropagation();
      const id = toggle.dataset.togglePipeline;
      const pipeline = pipelines.find((p) => p.id === id);
      if (!pipeline) return;
      try {
        await api(`/api/settings/pipelines/${encodeURIComponent(id)}`, {
          method: "PATCH",
          body: JSON.stringify({ enabled: pipeline.enabled === false }),
        });
        toast(pipeline.enabled === false ? "Pipeline enabled." : "Pipeline disabled.");
        await loadConfig();
      } catch (err) {
        toast(err.message, "danger");
      }
      return;
    }
    const item = e.target.closest("[data-select-pipeline]");
    if (!item) return;
    const pipeline = pipelines.find((p) => p.id === item.dataset.selectPipeline);
    if (pipeline) renderPipelineEditor(pipeline);
  });

  deletedList?.addEventListener("click", (e) => {
    const item = e.target.closest("[data-select-deleted]");
    if (!item) return;
    renderDeletedDetail(item.dataset.selectDeleted);
  });

  document.getElementById("btnNewWorkflow")?.addEventListener("click", renderCreateWorkflow);
  document.getElementById("btnNewPipeline")?.addEventListener("click", renderCreatePipeline);
  document.getElementById("btnReloadConfig")?.addEventListener("click", async () => {
    try {
      await api("/api/settings/reload", { method: "POST" });
      await loadConfig();
      toast("Config reloaded.");
    } catch (err) {
      toast(err.message, "danger");
    }
  });

  document.getElementById("btnBackupAll")?.addEventListener("click", () => {
    toast("Building backup zip…");
  });

  loadConfig().catch((err) => toast(err.message, "danger"));
});
