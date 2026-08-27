/* Not-So-Mid-Point front end: form -> POST /api/recommend -> cards + map. */

const CATEGORIES = [
  ["coffee", "Coffee"], ["restaurant", "Food"], ["bar", "Bar"],
  ["brewery", "Brewery"], ["dessert", "Dessert"], ["park", "Park"],
  ["museum", "Museum"], ["art", "Art"], ["books", "Books"],
  ["movies", "Movies"], ["music", "Music"], ["shopping", "Shopping"],
  ["activity", "Activity"],
];
const DEFAULT_SELECTED = new Set(["coffee"]);

const MODES = [
  ["driving", "🚗 Drive"], ["transit", "🚌 Bus/train"],
  ["bicycling", "🚲 Bike"], ["walking", "🚶 Walk"],
];
// First in MODES is also what a new person starts on.
const DEFAULT_MODE = MODES[0][0];
const MODE_SHORT = { transit: "transit", driving: "drive", bicycling: "bike", walking: "walk" };
const MIN_PARTIES = 2;
const MAX_PARTIES = 5;
const LEG_COLORS = ["#2f6f5e", "#a8563c", "#3d5a80", "#7a5a8c", "#8a6d1f"];
const PARTY_LETTERS = ["A", "B", "C", "D", "E"];
const defaultPartyName = (i) => `Person ${PARTY_LETTERS[i]}`;

/* One entry per participant. `field` is the AddressField bound to its input. */
const parties = [];

const WEIGHT_NAMES = ["fairness", "preference", "transfers"];
// Source of truth for the weights. Always sums to 100 across the *active*
// sliders, so each slider's position is literally its percentage.
const weights = { fairness: 40, preference: 40, transfers: 20 };
let lastTransfersWeight = 20;

const $ = (sel) => document.querySelector(sel);

/* Hosting edges occasionally fail to route a request to the instance, returning
   a 404 or 5xx that never reached the app at all. Those are safe to retry: no
   API call was made, no rate-limit budget was spent. A 429 or a real error from
   our own code is NOT retried, since that response is the answer. */
const TRANSIENT = new Set([404, 502, 503, 504]);

async function fetchWithRetry(url, options, attempts = 3) {
  let lastError;
  for (let i = 0; i < attempts; i++) {
    try {
      const response = await fetch(url, options);
      if (!TRANSIENT.has(response.status) || i === attempts - 1) return response;
    } catch (error) {
      lastError = error;
      if (i === attempts - 1) throw error;
    }
    // Brief, growing pause: these gaps are short-lived.
    await new Promise((r) => setTimeout(r, 250 * (i + 1)));
  }
  throw lastError || new Error("request failed");
}
const form = $("#form");
const statusBox = $("#status");
const output = $("#output");
const results = $("#results");

let map, layer;

/* ------------------------------------------------------- address typeahead */
/* One Google billing session covers every keystroke lookup plus the final
   Place Details call, as long as they share a token. A fresh token starts when
   the user begins typing again after a selection. */
const AUTOCOMPLETE_DEBOUNCE_MS = 300;
const AUTOCOMPLETE_MIN_CHARS = 3;

function newSessionToken() {
  return crypto.randomUUID ? crypto.randomUUID() : String(Math.random()).slice(2);
}

class AddressField {
  constructor(input, list) {
    this.input = input;
    this.list = list;
    this.session = newSessionToken();
    this.placeId = "";
    this.items = [];
    this.cursor = -1;
    this.timer = null;
    this.seq = 0;

    input.addEventListener("input", () => this.onType());
    input.addEventListener("keydown", (event) => this.onKey(event));
    input.addEventListener("blur", () => setTimeout(() => this.close(), 150));
    list.addEventListener("mousedown", (event) => {
      // mousedown, not click: blur would close the list first.
      const li = event.target.closest("li[data-index]");
      if (li) {
        event.preventDefault();
        this.choose(this.items[Number(li.dataset.index)]);
      }
    });
  }

  onType() {
    // Typing after a pick invalidates it -- fall back to geocoding the text.
    this.placeId = "";
    clearTimeout(this.timer);
    const query = this.input.value.trim();
    if (query.length < AUTOCOMPLETE_MIN_CHARS) {
      this.close();
      return;
    }
    this.timer = setTimeout(() => this.fetch(query), AUTOCOMPLETE_DEBOUNCE_MS);
  }

  async fetch(query) {
    const ticket = ++this.seq;
    try {
      const response = await fetchWithRetry("/api/places/autocomplete", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ q: query, session: this.session }),
      });
      if (!response.ok) return;
      const data = await response.json();
      // Drop responses that arrived out of order.
      if (ticket !== this.seq) return;
      this.render(data.suggestions || []);
    } catch {
      this.close(); // suggestions are a convenience; never block the form
    }
  }

  render(items) {
    this.items = items;
    this.cursor = -1;
    if (!items.length) {
      this.close();
      return;
    }
    this.list.innerHTML = items
      .map(
        (item, i) =>
          `<li role="option" data-index="${i}" aria-selected="false">
            <span class="main">${escapeHtml(item.main || item.text)}</span>
            ${item.secondary ? `<span class="secondary">${escapeHtml(item.secondary)}</span>` : ""}
          </li>`
      )
      .join("");
    this.list.hidden = false;
    this.input.setAttribute("aria-expanded", "true");
  }

  move(step) {
    if (!this.items.length) return;
    this.cursor = (this.cursor + step + this.items.length) % this.items.length;
    [...this.list.children].forEach((li, i) =>
      li.setAttribute("aria-selected", i === this.cursor ? "true" : "false")
    );
    this.list.children[this.cursor].scrollIntoView({ block: "nearest" });
  }

  onKey(event) {
    if (this.list.hidden) return;
    if (event.key === "ArrowDown") { event.preventDefault(); this.move(1); }
    else if (event.key === "ArrowUp") { event.preventDefault(); this.move(-1); }
    else if (event.key === "Escape") { this.close(); }
    else if (event.key === "Enter" && this.cursor >= 0) {
      event.preventDefault(); // don't submit the form on the first Enter
      this.choose(this.items[this.cursor]);
    }
  }

  choose(item) {
    if (!item) return;
    this.input.value = item.text;
    this.placeId = item.place_id;
    this.close();
  }

  close() {
    this.list.hidden = true;
    this.list.innerHTML = "";
    this.items = [];
    this.cursor = -1;
    this.input.setAttribute("aria-expanded", "false");
  }

  /* Called once the address has been used, so the next edit opens a new
     billing session rather than extending the closed one. */
  commit() {
    const used = { placeId: this.placeId, session: this.session };
    this.session = newSessionToken();
    return used;
  }
}



/* ---------------------------------------------------------------- form UI */
function buildChips() {
  const host = $("#categories");
  for (const [value, label] of CATEGORIES) {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "chip";
    chip.textContent = label;
    chip.dataset.value = value;
    chip.setAttribute("aria-pressed", DEFAULT_SELECTED.has(value) ? "true" : "false");
    chip.addEventListener("click", () => {
      chip.setAttribute("aria-pressed", chip.getAttribute("aria-pressed") === "true" ? "false" : "true");
    });
    host.appendChild(chip);
  }
}

function addParty(defaults = {}) {
  if (parties.length >= MAX_PARTIES) return;
  const index = parties.length;
  const party = { mode: defaults.mode || DEFAULT_MODE, field: null, nameInput: null, renamed: false };
  parties.push(party);

  const row = document.createElement("div");
  row.className = "party";
  row.innerHTML = `
    <div class="who">
      <span class="dot" style="color:${LEG_COLORS[index]}"></span>
      <input class="party-name" value="${escapeHtml(defaultPartyName(index))}"
             aria-label="Name for this person" maxlength="24" spellcheck="false">
      <button type="button" class="remove-party" aria-label="Remove this person" title="Remove">&times;</button>
    </div>
    <div class="field">
      <span class="typeahead">
        <input class="address" placeholder="e.g. Ballard, Seattle" autocomplete="off" role="combobox"
               aria-expanded="false" aria-autocomplete="list" required>
        <ul class="suggestions" role="listbox" hidden></ul>
      </span>
      <div class="modes"></div>
    </div>`;

  const nameInput = row.querySelector(".party-name");
  party.nameInput = nameInput;
  // Renaming is opt-in: an untouched field keeps tracking its letter as people
  // are added or removed, a renamed one is left alone.
  nameInput.addEventListener("input", () => {
    party.renamed = nameInput.value.trim().length > 0;
  });
  nameInput.addEventListener("blur", () => {
    if (!nameInput.value.trim()) {
      party.renamed = false;
      renumberParties();
    }
  });

  const input = row.querySelector("input.address");
  input.value = defaults.address || "";
  party.field = new AddressField(input, row.querySelector(".suggestions"));

  const modeHost = row.querySelector(".modes");
  for (const [value, label] of MODES) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "mode";
    button.textContent = label;
    button.dataset.value = value;
    button.setAttribute("aria-pressed", party.mode === value ? "true" : "false");
    button.addEventListener("click", () => {
      party.mode = value;
      for (const sibling of modeHost.children) {
        sibling.setAttribute("aria-pressed", sibling === button ? "true" : "false");
      }
      syncTransferControls();
    });
    modeHost.appendChild(button);
  }

  row.querySelector(".remove-party").addEventListener("click", () => removeParty(party));
  $("#parties").appendChild(row);
  renumberParties();
}

function removeParty(party) {
  if (parties.length <= MIN_PARTIES) return;
  const index = parties.indexOf(party);
  parties.splice(index, 1);
  $("#parties").children[index].remove();
  renumberParties();
  syncTransferControls();
}

/* Labels, colours and the add/remove affordances all depend on the count. */
function renumberParties() {
  [...$("#parties").children].forEach((row, i) => {
    const party = parties[i];
    row.querySelector(".dot").style.color = LEG_COLORS[i];
    if (!party.renamed) party.nameInput.value = defaultPartyName(i);
    row.querySelector("input.address").setAttribute(
      "aria-label",
      `${party.nameInput.value} starting address`
    );
    const remove = row.querySelector(".remove-party");
    remove.disabled = parties.length <= MIN_PARTIES;
    remove.style.visibility = parties.length <= MIN_PARTIES ? "hidden" : "visible";
  });
  $("#addParty").disabled = parties.length >= MAX_PARTIES;
  $("#partyCount").textContent =
    parties.length >= MAX_PARTIES
      ? `${MAX_PARTIES} is the maximum`
      : `${parties.length} of ${MAX_PARTIES}`;
  syncFairnessMode();
}

/* With two people, "fair" naturally means the gap between them. With three or
   more, holding the spread even tends to drag everyone toward a mediocre middle
   nobody chose, so least combined time is the better starting point. This only
   ever moves an untouched control: once someone picks a mode themselves, the
   party count stops overriding them. */
let fairnessModeChosen = false;

function syncFairnessMode() {
  if (fairnessModeChosen) return;
  const wanted = parties.length > 2 ? "absolute" : "gap";
  for (const radio of form.fairness_mode) radio.checked = radio.value === wanted;
}

function partyName(party, index) {
  return party.nameInput.value.trim() || defaultPartyName(index);
}

/* Transfers only exist on transit, so both the budget and the weight are
   disabled (and visibly explained) when neither person is riding. */
function transfersApply() {
  return parties.some((party) => party.mode === "transit");
}

function syncTransferControls() {
  const applies = transfersApply();
  const field = $("#transferField");
  const weight = $("#weightTransfers");
  field.classList.toggle("inapplicable", !applies);
  weight.classList.toggle("inapplicable", !applies);
  form.max_transfers.disabled = !applies;
  form.w_transfers.disabled = !applies;
  // Drive is the default, so this note now greets everyone on first load rather
  // than only appearing once someone switched transit off -- it has to read as
  // a sentence for one mode as well as for several.
  const modes = [...new Set(parties.map((p) => MODE_SHORT[p.mode] || p.mode))];
  const subject =
    modes.length === 1
      ? `a ${modes[0]} has`
      : `${modes.slice(0, -1).join(", ")} and ${modes[modes.length - 1]} have`;
  $("#transferNote").textContent = applies ? "" : `Not applicable: ${subject} no transfers.`;
  resplitForModeChange();
  syncOutputs();
}

function selectedCategories() {
  return [...document.querySelectorAll('.chip[aria-pressed="true"]')].map((c) => c.dataset.value);
}

function activeWeights() {
  // A transfer weight is meaningless when nobody is on transit.
  return transfersApply() ? WEIGHT_NAMES : ["fairness", "preference"];
}

function weightInputs() {
  return { ...weights };
}

/* Split `remaining` across `others`, in proportion to what they hold now.
   The last one absorbs the rounding error so the total is exactly 100. */
function distribute(others, remaining) {
  const currentTotal = others.reduce((sum, name) => sum + weights[name], 0);
  let handed = 0;
  others.forEach((name, i) => {
    let share;
    if (i === others.length - 1) {
      share = remaining - handed;
    } else if (currentTotal > 0) {
      share = Math.round((weights[name] / currentTotal) * remaining);
    } else {
      share = Math.floor(remaining / others.length);
    }
    weights[name] = Math.max(0, share);
    handed += weights[name];
  });
}

/* Dragging one slider takes from (or gives to) the others, so the three always
   total 100%. Without this the labels renormalised but the thumbs did not, and
   a slider sitting at 48 would be labelled 30%. */
function rebalanceWeights(changed, rawValue) {
  const active = activeWeights();
  if (!active.includes(changed)) return;
  weights[changed] = Math.max(0, Math.min(100, Math.round(rawValue)));
  distribute(active.filter((name) => name !== changed), 100 - weights[changed]);
  if (transfersApply()) lastTransfersWeight = weights.transfers;
  applyWeights();
}

/* Enabling or disabling the transfer weight has to re-split the other two,
   otherwise the visible percentages stop adding up. */
function resplitForModeChange() {
  if (transfersApply()) {
    weights.transfers = Math.max(1, lastTransfersWeight);
    distribute(["fairness", "preference"], 100 - weights.transfers);
  } else {
    weights.transfers = 0;
    distribute(["fairness", "preference"], 100);
  }
  applyWeights();
}

function applyWeights() {
  const active = activeWeights();
  for (const name of WEIGHT_NAMES) {
    const shown = active.includes(name) ? weights[name] : 0;
    form[`w_${name}`].value = shown;
    document.querySelector(`output[data-for="${name}"]`).textContent = `${shown}%`;
  }
}

function syncOutputs() {
  $("#timeOut").textContent = `${form.max_time_min.value} min`;
  const t = Number(form.max_transfers.value);
  $("#transferOut").textContent = transfersApply() ? (t === 0 ? "none" : String(t)) : "n/a";

}

/* ------------------------------------------------------------- rendering */
const minutes = (n) => `${Math.round(n)} min`;
const transferLabel = (leg) =>
  leg.mode !== "transit"
    ? MODE_SHORT[leg.mode] || leg.mode
    : leg.transfers === 0
    ? "direct"
    : `${leg.transfers} transfer${leg.transfers === 1 ? "" : "s"}`;

function bar(label, value, dead) {
  const pct = Math.round(value * 100);
  // A component that couldn't separate the candidates is shown greyed and
  // labelled, rather than as a misleading 100%.
  return `<div class="bar${dead ? " dead" : ""}"><span>${label}</span>
    <div><i style="width:${dead ? 0 : pct}%"></i></div>
    <span>${dead ? "n/a" : pct + "%"}</span></div>`;
}

function spotCard(item, index, people) {
  const { venue, shortlist: s, scores } = item;
  const dead = new Set(scores.inactive || []);
  const rating = venue.rating ? ` · ${venue.rating}★ (${venue.rating_count})` : "";
  return `
    <article class="spot">
      <header><span class="rank">${index + 1}</span><h3>${escapeHtml(venue.name)}</h3></header>
      <p class="meta">${escapeHtml(venue.category)}${rating} · ${escapeHtml(s.neighbourhood)}<br>${escapeHtml(venue.address)}</p>
      <p class="why">${escapeHtml(item.why)}</p>
      <div class="legs">
        ${s.legs
          .map(
            (leg, i) => `<div class="leg" style="border-color:${LEG_COLORS[i]}">
              <span>${escapeHtml(people[i])}<span class="mode-tag">${escapeHtml(MODE_SHORT[leg.mode] || leg.mode)}</span></span>
              <b>${minutes(leg.duration_min)}</b>
              <span>${escapeHtml(transferLabel(leg))}${leg.summary ? ` · ${escapeHtml(leg.summary)}` : ""}</span>
            </div>`
          )
          .join("")}
      </div>
      <p class="gap">${s.legs.length > 2 ? "Spread across you" : "Difference between you"}:
        <strong>${minutes(s.gap_min)}</strong> · combined ${minutes(s.total_min)}${
        s.transfers_meaningful ? ` · ${s.total_transfers} transfers total` : ""
      }</p>
      <div class="bars">
        ${bar("Fairness", scores.fairness, dead.has("fairness"))}
        ${bar("Preference", scores.preference, dead.has("preference"))}
        ${bar("Transfers", scores.transfers, dead.has("transfers"))}
        ${bar("Final score", scores.final, false)}
      </div>
    </article>`;
}

function shortName(label) {
  return label.split(",")[0];
}

/* Map pins for people carry their initials, not a number: the venue pins are
   already numbered by rank, and two numbered scales on one map read as one.
   The default "Person A" contributes only its letter -- "PA" would be noise. */
function personInitials(label) {
  const words = (label || "").trim().replace(/^person\s+/i, "").split(/\s+/).filter(Boolean);
  if (!words.length) return "?";
  // Spread rather than [0], so a name starting with an emoji or an astral
  // character yields that character instead of half a surrogate pair.
  return words.slice(0, 2).map((word) => [...word][0].toUpperCase()).join("");
}

function renderDetail(data) {
  const rows = data.shortlist
    .map(
      (e) =>
        `<tr><td>${escapeHtml(e.neighbourhood)}</td>` +
        e.legs.map((leg) => `<td>${minutes(leg.duration_min)}</td>`).join("") +
        `<td>${minutes(e.gap_min)}</td><td>${e.total_transfers}</td></tr>`
    )
    .join("");
  const timings = Object.entries(data.timings)
    .map(([k, v]) => `${k} ${v.toFixed(2)}s`)
    .join(" · ");
  const spec = data.preference_spec;
  const specLine = spec
    ? `<p><strong>Interpreted your interests as:</strong> ${escapeHtml(spec.place_types.join(", "))}
       ${spec.text_query ? `<br>Text search: “${escapeHtml(spec.text_query)}”` : ""}
       <br><span class="hint">source: ${escapeHtml(spec.source)} · ${escapeHtml(spec.rationale)}</span></p>`
    : "";
  const inactive = (data.results[0] && data.results[0].scores.inactive) || [];
  const inactiveLine = inactive.length
    ? `<p><strong>Scoring components not applied:</strong> ${escapeHtml(inactive.join(", "))}
       <br><span class="hint">Every candidate scored identically on these, so their weight was
       redistributed across the components that do vary.</span></p>`
    : "";
  $("#detailBody").innerHTML = `
    <p><strong>Search area:</strong> ${escapeHtml(data.search_area ? data.search_area.balance_note : "n/a")}</p>
    ${inactiveLine}
    ${specLine}
    <p><strong>Neighbourhoods that passed your limits (${data.shortlist.length}), best fairness first:</strong></p>
    <table><thead><tr><th>Neighbourhood</th>
    ${data.people.map((p) => `<th>${escapeHtml(p.label)}</th>`).join("")}
    <th>${data.people.length > 2 ? "Spread" : "Gap"}</th><th>Transfers</th></tr></thead>
    <tbody>${rows}</tbody></table>
    <p class="hint" style="margin-top:.75rem">${escapeHtml(timings)}</p>`;
}

function renderWarnings(warnings) {
  if (!warnings || !warnings.length) return "";
  return `<ul class="warnings">${warnings.map((w) => `<li>${escapeHtml(w)}</li>`).join("")}</ul>`;
}

function drawMap(data) {
  if (!map) {
    map = L.map("map", { scrollWheelZoom: false });
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      attribution: "&copy; OpenStreetMap contributors",
      maxZoom: 19,
    }).addTo(map);
  }
  if (layer) layer.remove();
  layer = L.layerGroup().addTo(map);

  // Two initials need a little more room inside the same circle than one
  // character does, so the type shrinks rather than the pin growing.
  const pin = (color, text) => {
    // Count code points, not UTF-16 units: an emoji initial is one glyph wide
    // however many units it takes to store.
    const wide = [...text].length > 1;
    return L.divIcon({
      className: "",
      html: `<div style="background:${color};color:#fff;width:26px;height:26px;border-radius:50%;
        display:grid;place-items:center;font:700 ${wide ? 10 : 12}px sans-serif;
        letter-spacing:${wide ? "-.02em" : "0"};box-shadow:0 1px 4px rgba(0,0,0,.4)">${escapeHtml(text)}</div>`,
      iconSize: [26, 26],
      iconAnchor: [13, 13],
    });
  };

  const points = [];
  data.people.forEach((person, i) => {
    const { lat, lng } = person.location.coords;
    L.marker([lat, lng], { icon: pin(LEG_COLORS[i], personInitials(person.label)) })
      .bindPopup(`<strong>${escapeHtml(person.label)}</strong><br>${escapeHtml(person.location.label)}`)
      .addTo(layer);
    points.push([lat, lng]);
  });

  if (data.search_area) {
    L.circle([data.search_area.center.lat, data.search_area.center.lng], {
      radius: data.search_area.radius_m,
      color: "#2f6f5e",
      weight: 1,
      fillOpacity: 0.06,
    }).addTo(layer);
  }

  data.results.forEach((item, i) => {
    const { lat, lng } = item.venue.coords;
    L.marker([lat, lng], { icon: pin("#1d1c1a", String(i + 1)) })
      .bindPopup(`<strong>${escapeHtml(item.venue.name)}</strong><br>${escapeHtml(item.venue.address)}`)
      .addTo(layer);
    points.push([lat, lng]);
  });

  // The map container is only sized once #output is un-hidden, so measure
  // before fitting -- otherwise Leaflet fits against a stale size and zooms out.
  const bounds = L.latLngBounds(points).pad(0.2);
  map.invalidateSize();
  map.fitBounds(bounds);
  setTimeout(() => {
    map.invalidateSize();
    map.fitBounds(bounds);
  }, 60);
}

function showNotice(title, body, warnings) {
  statusBox.className = "panel";
  statusBox.innerHTML = `<div class="notice"><h2>${escapeHtml(title)}</h2><p>${escapeHtml(body)}</p></div>${renderWarnings(warnings)}`;
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );
}

/* --------------------------------------------------------------- request */
async function submit(event) {
  event.preventDefault();
  const blankIndex = parties.findIndex((party) => party.field.input.value.trim().length < 2);
  if (blankIndex >= 0) {
    const party = parties[blankIndex];
    party.field.input.focus();
    showNotice(
      "Missing a starting point",
      `${partyName(party, blankIndex)} needs an address before we can search.`
    );
    return;
  }

  const button = $("#submit");
  button.disabled = true;
  button.textContent = "Checking transit routes…";
  output.classList.add("hidden");
  statusBox.className = "panel";
  statusBox.innerHTML = `<p class="spinner">Sampling the corridor between you, then timing transit from both sides…</p>`;

  const payload = {
    people: parties.map((party, i) => {
      const picked = party.field.commit();
      return {
        address: party.field.input.value,
        mode: party.mode,
        place_id: picked.placeId,
        session: picked.session,
        label: partyName(party, i),
      };
    }),
    categories: selectedCategories(),
    free_text: form.free_text.value,
    max_time_min: Number(form.max_time_min.value),
    max_transfers: Number(form.max_transfers.value),
    weights: weightInputs(),
    fairness_mode: form.fairness_mode.value,
    require_open: form.require_open.checked,
  };

  try {
    const response = await fetchWithRetry("/api/recommend", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await response.json();

    if (response.status === 429) {
      // The limiter is protecting the Maps bill, not rejecting the input, so
      // say so plainly rather than implying the search was malformed.
      const wait = Number(response.headers.get("Retry-After") || 0);
      const when = wait > 90 ? ` Try again in about ${Math.ceil(wait / 60)} minutes.` : "";
      showNotice("Hold on a moment", (data.detail || "Too many searches.") + when);
      return;
    }
    if (!response.ok) {
      showNotice("That didn't work", data.detail || `Request failed (${response.status}).`);
      return;
    }
    if (!data.ok) {
      const f = data.failure || {};
      showNotice(f.reason || "No spot found.", f.suggestion || "Try loosening your limits.", data.warnings);
      return;
    }

    statusBox.className = "panel hidden";
    const people = data.people.map((p) => p.label);
    results.innerHTML =
      data.results.map((item, i) => spotCard(item, i, people)).join("") + renderWarnings(data.warnings);
    renderDetail(data);
    output.classList.remove("hidden");
    drawMap(data);
  } catch (error) {
    showNotice("Couldn't reach the server", String(error));
  } finally {
    button.disabled = false;
    button.textContent = "Find our spot";
  }
}

buildChips();
addParty({ address: "Ballard, Seattle, WA" });
addParty({ address: "Columbia City, Seattle, WA" });
$("#addParty").addEventListener("click", () => {
  addParty();
  syncTransferControls();
});
for (const name of WEIGHT_NAMES) {
  form[`w_${name}`].addEventListener("input", (event) =>
    rebalanceWeights(name, Number(event.target.value))
  );
}
// The weight sliders manage themselves; everything else just refreshes labels.
form.addEventListener("input", (event) => {
  if (!String(event.target.name || "").startsWith("w_")) syncOutputs();
});
$("#fairnessMode").addEventListener("change", () => {
  fairnessModeChosen = true;
});
form.addEventListener("submit", submit);

const helpDialog = $("#helpDialog");
$("#helpButton").addEventListener("click", () => helpDialog.showModal());
helpDialog.querySelector(".close-dialog").addEventListener("click", () => helpDialog.close());
// Clicking the backdrop closes it; clicks inside the panel must not.
helpDialog.addEventListener("click", (event) => {
  if (event.target === helpDialog) helpDialog.close();
});

syncTransferControls();
applyWeights();
syncOutputs();
