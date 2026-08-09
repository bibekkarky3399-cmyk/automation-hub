(() => {
  const $ = (id) => document.getElementById(id);

  const els = {
    form: $("flightSearchForm"),
    sectorFrom: $("sectorFrom"),
    sectorTo: $("sectorTo"),
    flightDate: $("flightDate"),
    returnDate: $("returnDate"),
    returnField: $("returnField"),
    adults: $("adults"),
    children: $("children"),
    nationality: $("nationality"),
    swapBtn: $("btnSwapSectors"),
    searchBtn: $("btnSearch"),
    formError: $("searchError"),
    loading: $("loadingState"),
    results: $("resultsSection"),
    resultsTitle: $("resultsTitle"),
    resultsSub: $("resultsSub"),
    resultsMetaPill: $("resultsMetaPill"),
    resultsCount: $("resultsCount"),
    resultsList: $("flightList"),
    emptyState: $("resultsEmpty"),
    airlineFilters: $("airlineFilters"),
    timeFilters: $("timeFilters"),
    resultsToolbar: $("resultsToolbar"),
    directionTabs: $("directionTabs"),
    tabOutbound: $("tabOutbound"),
    tabInbound: $("tabInbound"),
    sortBy: $("sortBy"),
    quickRoutes: $("quickRoutes"),
  };

  const POPULAR = [
    ["KTM", "PKR"],
    ["KTM", "BIR"],
    ["KTM", "BDP"],
    ["KTM", "BWA"],
    ["PKR", "KTM"],
    ["KTM", "DHI"],
  ];

  const state = {
    sectors: [],
    flights: [],
    airlines: [],
    meta: {},
    query: null,
    airlineFilter: "all",
    timeBand: "all",
    direction: "Outbound",
    selectedFare: {},
  };

  function showError(msg) {
    if (!msg) {
      els.formError.classList.add("d-none");
      els.formError.textContent = "";
      return;
    }
    els.formError.textContent = msg;
    els.formError.classList.remove("d-none");
  }

  function setLoading(on) {
    els.loading.classList.toggle("d-none", !on);
    els.searchBtn.disabled = on;
  }

  function todayISO() {
    const d = new Date();
    const pad = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  }

  function formatMoney(n, currency = "NPR") {
    const num = Number(n);
    if (!Number.isFinite(num)) return "—";
    return `${currency} ${Math.round(num).toLocaleString("en-NP")}`;
  }

  function formatDuration(min) {
    const n = Number(min);
    if (!Number.isFinite(n) || n <= 0) return "";
    if (n < 60) return `${n} min`;
    const h = Math.floor(n / 60);
    const m = n % 60;
    return m ? `${h}h ${m}m` : `${h}h`;
  }

  function tripType() {
    const checked = document.querySelector('input[name="trip_type"]:checked');
    return checked ? checked.value : "O";
  }

  function syncReturn() {
    const rt = tripType() === "R";
    els.returnField.classList.toggle("is-disabled", !rt);
    els.returnDate.disabled = !rt;
    if (!rt) els.returnDate.value = "";
    else if (!els.returnDate.value && els.flightDate.value) {
      els.returnDate.min = els.flightDate.value;
    }
  }

  function fillSectors(list) {
    state.sectors = list || [];
    const opts = state.sectors
      .map((s) => {
        const code = s.code || "";
        const name = s.name || code;
        if (!code) return "";
        return `<option value="${escapeAttr(code)}">${escapeHtml(name)} (${escapeHtml(code)})</option>`;
      })
      .filter(Boolean)
      .join("");

    const blank = `<option value="">Select city</option>`;
    els.sectorFrom.innerHTML = blank + opts;
    els.sectorTo.innerHTML = blank + opts;

    const codes = new Set(state.sectors.map((s) => String(s.code || "").toUpperCase()));
    if (codes.has("KTM")) els.sectorFrom.value = "KTM";
    if (codes.has("PKR")) els.sectorTo.value = "PKR";
    renderQuickRoutes(codes);
  }

  function renderQuickRoutes(codes) {
    const available = POPULAR.filter(([a, b]) => codes.has(a) && codes.has(b));
    if (!available.length) {
      els.quickRoutes.innerHTML = "";
      return;
    }
    els.quickRoutes.innerHTML = available
      .map(
        ([a, b]) =>
          `<button type="button" class="fx-quick-chip" data-from="${a}" data-to="${b}">${a} → ${b}</button>`
      )
      .join("");
    els.quickRoutes.querySelectorAll(".fx-quick-chip").forEach((btn) => {
      btn.addEventListener("click", () => {
        els.sectorFrom.value = btn.dataset.from;
        els.sectorTo.value = btn.dataset.to;
      });
    });
  }

  async function loadSectors() {
    try {
      const res = await fetch("/api/flights/sectors", { credentials: "same-origin" });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || data.ok === false) {
        throw new Error(data.error || `Could not load cities (${res.status})`);
      }
      fillSectors(data.sectors || []);
    } catch (err) {
      showError(err.message || "Could not load sector list.");
      fillSectors([]);
    }
  }

  function nationalityCode() {
    const raw = (els.nationality.value || "NP").toUpperCase();
    // API wants ISO-2; "OTHER" falls back to NP for domestic fare rules.
    if (!raw || raw === "OTHER" || raw.length !== 2) return "NP";
    return raw;
  }

  function nationalityLabel() {
    const opt = els.nationality.selectedOptions[0];
    return opt ? opt.textContent.trim() : nationalityCode();
  }

  function currentFilters() {
    return {
      from: els.sectorFrom.value,
      to: els.sectorTo.value,
      date: els.flightDate.value,
      return_date: tripType() === "R" ? els.returnDate.value : "",
      trip_type: tripType(),
      nationality: nationalityCode(),
      adults: Number(els.adults.value || 1),
      children: Number(els.children.value || 0),
    };
  }

  function validate(payload) {
    if (!payload.from || !payload.to) return "Choose departure and arrival cities.";
    if (payload.from === payload.to) return "Departure and arrival must be different.";
    if (!payload.date) return "Pick a departure date.";
    if (payload.trip_type === "R" && !payload.return_date) return "Pick a return date.";
    if (payload.adults < 1) return "At least one adult is required.";
    return "";
  }

  async function search(ev) {
    if (ev) ev.preventDefault();
    showError("");
    const payload = currentFilters();
    const err = validate(payload);
    if (err) {
      showError(err);
      return;
    }

    setLoading(true);
    els.results.hidden = true;
    els.emptyState.classList.add("d-none");

    try {
      const res = await fetch("/api/flights/search", {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        credentials: "same-origin",
        body: JSON.stringify(payload),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || data.ok === false) {
        throw new Error(data.error || `Search failed (${res.status})`);
      }
      state.flights = data.flights || [];
      state.airlines = data.airlines || [];
      state.meta = data.meta || {};
      state.query = data.query || payload;
      state.airlineFilter = "all";
      state.timeBand = "all";
      state.direction = "Outbound";
      state.selectedFare = {};
      renderResults(payload);
    } catch (e) {
      showError(e.message || "Search failed.");
      state.flights = [];
      els.results.hidden = true;
    } finally {
      setLoading(false);
    }
  }

  function filteredSorted() {
    let list = state.flights.slice();
    const hasInbound = state.flights.some((f) => f.direction === "Inbound");
    if (hasInbound || state.query?.trip_type === "R") {
      list = list.filter((f) => (f.direction || "Outbound") === state.direction);
    }
    if (state.airlineFilter !== "all") {
      list = list.filter((f) => f.airline_code === state.airlineFilter);
    }
    if (state.timeBand !== "all") {
      list = list.filter((f) => f.time_band === state.timeBand);
    }
    const sort = els.sortBy.value;
    list.sort((a, b) => {
      if (sort === "price") return (a.from_price ?? 1e12) - (b.from_price ?? 1e12);
      if (sort === "airline") {
        return String(a.airline_name || "").localeCompare(String(b.airline_name || "")) ||
          String(a.flight_no || "").localeCompare(String(b.flight_no || ""));
      }
      return String(a.dep_time || "").localeCompare(String(b.dep_time || ""));
    });
    return list;
  }

  function setAirlineFilter(code) {
    state.airlineFilter = code || "all";
    const pool = directionPool(state.flights).filter((f) =>
      state.airlineFilter === "all" ? true : f.airline_code === state.airlineFilter
    );
    if (
      state.timeBand !== "all" &&
      !pool.some((f) => f.time_band === state.timeBand)
    ) {
      state.timeBand = "all";
    }
    renderFilters();
    renderList();
  }

  function setTimeBand(band) {
    state.timeBand = band || "all";
    renderFilters();
    renderList();
  }

  function directionPool(list) {
    const hasInbound = list.some((f) => f.direction === "Inbound");
    if (!hasInbound && !(state.query && state.query.trip_type === "R")) return list;
    return list.filter((f) => (f.direction || "Outbound") === state.direction);
  }

  function renderFilters() {
    const currency = state.meta.currency || "NPR";
    const inDirection = directionPool(state.flights);
    const byAirline = new Map();
    for (const f of inDirection) {
      const code = f.airline_code;
      if (!code) continue;
      const prev = byAirline.get(code) || {
        code,
        name: f.airline_name,
        color: f.airline_color,
        count: 0,
        from_price: null,
      };
      prev.count += 1;
      if (f.from_price != null && (prev.from_price == null || f.from_price < prev.from_price)) {
        prev.from_price = f.from_price;
      }
      byAirline.set(code, prev);
    }
    const airlines = [...byAirline.values()].sort((a, b) =>
      String(a.name || "").localeCompare(String(b.name || ""))
    );
    const airlineCount = airlines.length;
    const flightWord = (n) => `${n} flight${n === 1 ? "" : "s"}`;

    els.airlineFilters.innerHTML =
      `<button type="button" class="fx-chip${state.airlineFilter === "all" ? " is-on" : ""}" data-filter="airline" data-airline="all" aria-pressed="${state.airlineFilter === "all"}">
        All airlines · ${airlineCount}
      </button>` +
      airlines
        .map((a) => {
          const on = state.airlineFilter === a.code;
          const price =
            a.from_price != null ? ` · from ${formatMoney(a.from_price, currency)}` : "";
          return `<button type="button" class="fx-chip${on ? " is-on" : ""}" data-filter="airline" data-airline="${escapeAttr(a.code)}" aria-pressed="${on}">
            <span class="fx-air-dot" style="background:${escapeAttr(a.color || "#0b3d5c")}"></span>
            ${escapeHtml(a.name || a.code)} · ${flightWord(a.count)}${price}
          </button>`;
        })
        .join("");

    const bands = [
      { id: "all", label: "Any time" },
      { id: "morning", label: "Morning" },
      { id: "afternoon", label: "Afternoon" },
      { id: "evening", label: "Evening" },
    ];
    const pool = inDirection.filter((f) => {
      if (state.airlineFilter === "all") return true;
      return f.airline_code === state.airlineFilter;
    });
    els.timeFilters.innerHTML = bands
      .map((b) => {
        const on = state.timeBand === b.id;
        const count =
          b.id === "all"
            ? pool.length
            : pool.filter((f) => f.time_band === b.id).length;
        return `<button type="button" class="fx-chip${on ? " is-on" : ""}" data-filter="time" data-band="${b.id}" aria-pressed="${on}"${count === 0 && b.id !== "all" ? " disabled" : ""}>
          ${b.label} · ${flightWord(count)}
        </button>`;
      })
      .join("");
  }

  function renderDirectionTabs() {
    const inboundCount = state.meta.inbound_count || state.flights.filter((f) => f.direction === "Inbound").length;
    const outboundCount = state.meta.outbound_count || state.flights.filter((f) => f.direction !== "Inbound").length;
    const show = (state.query && state.query.trip_type === "R") || inboundCount > 0;
    els.directionTabs.classList.toggle("d-none", !show);
    if (!show) {
      state.direction = "Outbound";
      return;
    }
    els.tabOutbound.textContent = `Outbound (${outboundCount})`;
    els.tabInbound.textContent = `Return (${inboundCount})`;
    els.tabOutbound.classList.toggle("is-on", state.direction === "Outbound");
    els.tabInbound.classList.toggle("is-on", state.direction === "Inbound");
  }

  function renderResults(payload) {
    const q = state.query || payload;
    const from = q.from || payload.from;
    const to = q.to || payload.to;
    els.resultsTitle.textContent = `${from} → ${to}`;
    const adults = Number(q.adults || payload.adults || 1);
    const children = Number(q.children || payload.children || 0);
    const pax =
      `${adults} adult${adults === 1 ? "" : "s"}` +
      (children ? `, ${children} child${children === 1 ? "" : "ren"}` : "");
    const tripLabel = (q.trip_type || payload.trip_type) === "R" ? "Return" : "One way";
    const dateLabel = q.flight_date || payload.date;
    els.resultsSub.textContent = `${dateLabel} · ${tripLabel} · ${pax} · ${nationalityLabel()}`;

    const meta = state.meta || {};
    const currency = meta.currency || "NPR";
    const bits = [];
    if (meta.airline_count != null) bits.push(`${meta.airline_count} airline${meta.airline_count === 1 ? "" : "s"}`);
    if (meta.cheapest != null) bits.push(`from ${formatMoney(meta.cheapest, currency)}`);
    if (meta.cached) bits.push("cached");
    bits.push("Live fares");
    els.resultsMetaPill.textContent = bits.join(" · ");

    els.results.hidden = false;
    renderDirectionTabs();
    renderFilters();
    renderList();
  }

  function partyTotal(fare) {
    if (!fare || fare.total == null) return null;
    const adults = Number((state.query && state.query.adults) || els.adults.value || 1);
    const children = Number((state.query && state.query.children) || els.children.value || 0);
    const childUnit = fare.child_total != null ? fare.child_total : fare.total;
    return adults * fare.total + children * (childUnit || 0);
  }

  function renderList() {
    const list = filteredSorted();
    els.resultsCount.textContent = `${list.length} flight${list.length === 1 ? "" : "s"}`;
    if (!list.length) {
      els.resultsList.innerHTML = "";
      els.emptyState.classList.remove("d-none");
      return;
    }
    els.emptyState.classList.add("d-none");
    els.resultsList.innerHTML = list.map((f) => cardHtml(f)).join("");
    els.resultsList.querySelectorAll(".fx-fare").forEach((btn) => {
      btn.addEventListener("click", () => {
        state.selectedFare[btn.dataset.flight] = btn.dataset.class;
        renderList();
      });
    });
  }

  function cardHtml(f) {
    const id = f.group_id || `${f.airline_code}-${f.flight_no}-${f.dep_time}`;
    const color = f.airline_color || "#0b3d5c";
    const fares = f.fares || [];
    const selected =
      state.selectedFare[id] || (fares[0] && fares[0].class_code) || "";
    const selectedFare = fares.find((x) => x.class_code === selected) || fares[0];
    const currency = (selectedFare && selectedFare.currency) || f.currency || "NPR";
    const party = partyTotal(selectedFare);

    const fareBtns = fares
      .map((fare) => {
        const on = selectedFare && fare.class_code === selectedFare.class_code ? " is-on" : "";
        return `<button type="button" class="fx-fare${on}" data-flight="${escapeAttr(id)}" data-class="${escapeAttr(fare.class_code)}" title="Flight class ${escapeAttr(fare.class_code)}">
          <span class="fx-fare-label">Class ${escapeHtml(fare.class_code)}</span>
          <span class="fx-fare-price">${formatMoney(fare.total, fare.currency || currency)}</span>
        </button>`;
      })
      .join("");

    const facts = [];
    if (selectedFare && selectedFare.baggage) {
      facts.push(
        `<span><i class="bi bi-briefcase" aria-hidden="true"></i> ${escapeHtml(selectedFare.baggage)}</span>`
      );
    }
    if (selectedFare && selectedFare.refundable === true) {
      facts.push(
        `<span><i class="bi bi-shield-check" aria-hidden="true"></i> Refundable</span>`
      );
    } else if (selectedFare && selectedFare.refundable === false) {
      facts.push(
        `<span><i class="bi bi-shield-check" aria-hidden="true"></i> Non-refundable</span>`
      );
    }
    if (selectedFare && selectedFare.class_code) {
      facts.push(
        `<span><i class="bi bi-ticket-perforated" aria-hidden="true"></i> Class <span class="fx-mono">${escapeHtml(selectedFare.class_code)}</span></span>`
      );
    }

    const detail = selectedFare
      ? `<div class="fx-fare-detail">
          ${facts.length ? `<div class="fx-fare-facts">${facts.join("")}</div>` : ""}
          <div class="fx-fare-math">
            <div class="fx-fare-rows">
              ${selectedFare.adult_fare != null ? `<span>Base <strong>${formatMoney(selectedFare.adult_fare, currency)}</strong></span>` : ""}
              ${selectedFare.fuel_surcharge != null ? `<span>Fuel <strong>${formatMoney(selectedFare.fuel_surcharge, currency)}</strong></span>` : ""}
              ${selectedFare.tax != null ? `<span>Tax <strong>${formatMoney(selectedFare.tax, currency)}</strong></span>` : ""}
              ${selectedFare.vat != null ? `<span>VAT <strong>${formatMoney(selectedFare.vat, currency)}</strong></span>` : ""}
            </div>
            <div class="fx-fare-total">
              <span>Per adult</span>
              <strong>${formatMoney(selectedFare.total, currency)}</strong>
              ${party != null && (Number(els.adults.value) > 1 || Number(els.children.value) > 0)
                ? `<em>Party total ${formatMoney(party, currency)}</em>`
                : ""}
            </div>
          </div>
        </div>`
      : "";

    return `<article class="fx-card" data-id="${escapeAttr(id)}">
      <div class="fx-card-top">
        <div class="fx-air">
          <div class="fx-air-logo" style="background:${escapeAttr(color)}"><i class="bi bi-airplane-engines"></i></div>
          <div>
            <strong>${escapeHtml(f.airline_name || f.airline_code || "Airline")}</strong>
            <small class="fx-mono">${escapeHtml(f.flight_no || "—")}${f.aircraft ? ` · ${escapeHtml(f.aircraft)}` : ""}</small>
          </div>
        </div>
        <div class="fx-times">
          <div class="fx-time">
            <strong class="fx-mono">${escapeHtml(f.dep_time || "--:--")}</strong>
            <small>${escapeHtml(f.from_name || f.from || "")}</small>
          </div>
          <div class="fx-path">
            <div class="fx-path-line"></div>
            ${formatDuration(f.duration_min) ? `<small>${escapeHtml(formatDuration(f.duration_min))}</small>` : ""}
          </div>
          <div class="fx-time">
            <strong class="fx-mono">${escapeHtml(f.arr_time || "--:--")}</strong>
            <small>${escapeHtml(f.to_name || f.to || "")}</small>
          </div>
        </div>
        <div class="fx-from-price">
          <span>From</span>
          <strong class="fx-mono">${f.from_price != null ? formatMoney(f.from_price, currency) : "—"}</strong>
          <small>${f.fare_count || fares.length} fare${(f.fare_count || fares.length) === 1 ? "" : "s"}</small>
        </div>
      </div>
      <div class="fx-fares">${fareBtns || '<span class="fx-empty">No fares</span>'}</div>
      ${detail}
    </article>`;
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function escapeAttr(s) {
    return escapeHtml(s).replace(/'/g, "&#39;");
  }

  function bind() {
    els.flightDate.min = todayISO();
    if (!els.flightDate.value) els.flightDate.value = todayISO();
    els.returnDate.min = todayISO();
    syncReturn();

    document.querySelectorAll('input[name="trip_type"]').forEach((r) => {
      r.addEventListener("change", syncReturn);
    });
    els.flightDate.addEventListener("change", () => {
      els.returnDate.min = els.flightDate.value || todayISO();
      if (els.returnDate.value && els.returnDate.value < els.returnDate.min) {
        els.returnDate.value = els.returnDate.min;
      }
    });
    els.swapBtn.addEventListener("click", () => {
      const a = els.sectorFrom.value;
      els.sectorFrom.value = els.sectorTo.value;
      els.sectorTo.value = a;
    });
    els.sortBy.addEventListener("change", renderList);
    els.form.addEventListener("submit", search);

    els.resultsToolbar.addEventListener("click", (ev) => {
      const btn = ev.target.closest("button[data-filter]");
      if (!btn || !els.resultsToolbar.contains(btn)) return;
      if (btn.dataset.filter === "airline") {
        setAirlineFilter(btn.dataset.airline || "all");
      } else if (btn.dataset.filter === "time") {
        setTimeBand(btn.dataset.band || "all");
      }
    });

    els.tabOutbound.addEventListener("click", () => {
      state.direction = "Outbound";
      renderDirectionTabs();
      renderFilters();
      renderList();
    });
    els.tabInbound.addEventListener("click", () => {
      state.direction = "Inbound";
      renderDirectionTabs();
      renderFilters();
      renderList();
    });
  }

  bind();
  loadSectors();
})();
