"use strict";

/* Media Intelligence Editor -- frontend. Vanilla JS, no build step, no
 * framework. Loads the whole DB once (a few MB of JSON is nothing over
 * localhost), keeps it in memory, and re-fetches on demand (Reload
 * button, or automatically after a save). */

const state = {
  db: null,
  mtimeMs: null,
  // Story structure rows from the server ({act, sequence, start, finish}),
  // used to show act/sequence live while a scene is being typed. The
  // server is what actually writes act/sequence -- see deriveStructure().
  structure: [],
  structureError: null,
  // Selection is keyed on the asset's file path, not asset_id: a few
  // asset_ids are duplicated across folders, and path is what's unique.
  selectedPath: null,
  search: "",
  filters: {
    act: "",
    sequence: "",
    device: "",
    date: "",
    sceneStatus: "",
    usedInEditOnly: false,
    lowConfidenceOnly: false,
    includeMissing: false,
    hideCrFloor: true,
  },
  sort: "date-asc",
  dirty: false,
  // Whether the scene-candidates list is expanded for the currently
  // selected clip. Resets to false (i.e. collapsed-if-assigned) every
  // time a different clip is selected -- see renderSceneBrowser().
  sceneCandidatesExpanded: false,
};

// -----------------------------------------------------------------------
// CR2: constrained dropdown / multi-select option lists.
//
// shot_type and angle are select fields (same JSON keys as before -- this
// just constrains what the free-text inputs used to accept). take_status,
// quality_flags, and performance_indicators are NEW fields that supersede
// the old free-text "select", "quality", and "performance" fields; those
// old keys are no longer shown in this form, but if any asset already has
// a value in them it is left completely untouched in the JSON -- this
// app just doesn't display or edit it anymore. See a value that was
// hand-typed before this field became a dropdown and isn't in the
// standard list below? It's preserved automatically (see
// ensureSelectHasValue / fillMultiselect) rather than silently dropped.
// -----------------------------------------------------------------------

const SHOT_TYPE_OPTIONS = [
  "Extreme Long Shot (ELS)", "Long Shot (LS)", "Medium Long Shot / American (MLS)",
  "Medium Shot (MS)", "Medium Close-up (MCU)", "Close-up (CU)", "Extreme Close-up (ECU)",
];
const CAMERA_ANGLE_OPTIONS = [
  "Eye Level", "High Angle", "Low Angle", "Dutch", "Top Shot",
  "Worm's-Eye", "POV", "OTS", "Profile", "Reverse Angle",
];
const TAKE_STATUS_OPTIONS = [
  "Circle Take", "Good Take", "Hold for Review", "VFX Preferred", "Director Preferred",
  "Camera Preferred", "Sound Preferred", "NG (No Good)", "Incomplete", "Wild Track",
];
const QUALITY_FLAG_OPTIONS = [
  "Focus Issue", "Lighting Issue", "Boom in Frame", "Crew in Frame", "Continuity Issue",
  "Performance Issue", "Camera Bump", "Lens Flare (Unwanted)", "Rolling Shutter",
  "Exposure Issue", "Color Shift", "Audio Problem", "Slate Error",
];
const PERFORMANCE_INDICATOR_OPTIONS = [
  "Line Flub", "Missed Line", "Stepped on Line", "Unclear Delivery", "Unintelligible Delivery",
  "Pace Issue", "Emotional Continuity Issue", "Physical Continuity Issue", "Eyeline Issue",
  "Gesture Inconsistency", "Prop Interaction Issue", "Low Energy", "Flat", "Over-Read",
  "Actor Shadow Problem",
];

const SELECT_FIELDS = {
  shot_type: SHOT_TYPE_OPTIONS,
  angle: CAMERA_ANGLE_OPTIONS,
  take_status: TAKE_STATUS_OPTIONS,
};
const MULTISELECT_FIELDS = {
  quality_flags: QUALITY_FLAG_OPTIONS,
  performance_indicators: PERFORMANCE_INDICATOR_OPTIONS,
};

function slugify(s) {
  return String(s).toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/(^-|-$)/g, "");
}

const els = {};
function cacheEls() {
  [
    "project-name", "editor-version", "asset-count", "disk-status", "save-indicator", "reload-btn",
    "search-input", "filter-act", "filter-sequence", "filter-device", "filter-date", "filter-scene-status",
    "filter-used-in-edit", "filter-low-confidence", "filter-missing",
    "filter-hide-cr-floor",
    "sort-select", "asset-list",
    "detail-empty", "detail-content",
    "cr-floor-banner", "clip-filename", "clip-meta", "clip-tech",
    "copy-path-btn", "reveal-btn", "clip-path-status",
    "video-player", "audio-player", "video-fallback", "video-fallback-path",
    "scene-day-info", "scene-candidates-toggle-row", "scene-candidates-toggle",
    "scene-candidates", "scene-search-input", "scene-global-results",
    "editor-form", "f-act", "f-sequence", "f-scene", "f-transcript",
    "save-bar", "dirty-indicator", "save-btn",
  ].forEach((id) => { els[id] = document.getElementById(id); });
}

function escapeHtml(s) {
  if (s === null || s === undefined) return "";
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function debounce(fn, ms) {
  let t;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
}

// -----------------------------------------------------------------------
// Data loading
// -----------------------------------------------------------------------

async function loadDb() {
  const res = await fetch("/api/db");
  if (!res.ok) throw new Error(`GET /api/db failed: ${res.status}`);
  const payload = await res.json();
  state.db = payload.db;
  state.mtimeMs = payload.mtimeMs;
  state.structure = (payload.structure && payload.structure.rows) || [];
  state.structureError = (payload.structure && payload.structure.error) || null;
  state.callSheets = payload.callSheets || { files: [], overrides: {} };
  if (state.callSheets.error) console.warn(state.callSheets.error);

  // The server re-checks every path against the disk and re-derives
  // act/sequence on every load, writing back only what changed. Say so,
  // so a Reload that changed the file isn't a silent surprise in git.
  const sync = payload.sync || {};
  renderDiskStatus(sync);
  const notes = [];
  if (sync.presenceChanges) notes.push(`${sync.presenceChanges} on-disk status change${sync.presenceChanges === 1 ? "" : "s"}`);
  if (sync.structureUpdated) notes.push(`act/sequence updated on ${sync.structureUpdated} clip${sync.structureUpdated === 1 ? "" : "s"}`);
  if (notes.length) flashSaveIndicator(`Synced: ${notes.join(", ")}`, 6000);
  if (state.structureError) console.warn(state.structureError);
}

function flashSaveIndicator(msg, ms) {
  els["save-indicator"].textContent = msg;
  setTimeout(() => { if (els["save-indicator"].textContent === msg) els["save-indicator"].textContent = ""; }, ms || 1500);
}

// Topbar: how many clips are currently not on disk, and any drive that
// isn't connected at all (its clips keep whatever status they last had).
function renderDiskStatus(sync) {
  const el = els["disk-status"];
  const parts = [];
  if (sync.missingCount) parts.push(`${sync.missingCount} not on disk`);
  if (sync.offlineRoots && sync.offlineRoots.length) parts.push(`drive offline: ${sync.offlineRoots.join(", ")}`);
  el.textContent = parts.join(" · ");
  el.classList.toggle("hidden", !parts.length);
  el.title = "Checked against the disk on every load/Reload, every 20 seconds, and when this window regains focus. " +
    "Tick \"Include missing-on-disk\" to list those clips.";
}

// -----------------------------------------------------------------------
// Background disk-presence check
// -----------------------------------------------------------------------
// Asks the server to re-check every path against the disk. If a file or
// folder was deleted (or put back) since the last check, the server
// updates media_intel.missing_on_disk and returns what changed, which is
// applied to the in-memory DB here -- without touching whatever is being
// edited in the form. If the DB file was changed by something else in
// the meantime, the server writes nothing and this just says to Reload.

let presenceCheckInFlight = false;
async function checkPresence() {
  if (presenceCheckInFlight || !state.db) return;
  presenceCheckInFlight = true;
  try {
    const res = await fetch("/api/presence", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ expectedMtimeMs: state.mtimeMs }),
    });
    if (!res.ok) return;
    const body = await res.json();
    renderDiskStatus(body);
    if (body.stale) {
      els["disk-status"].textContent = "DB changed on disk — click Reload";
      els["disk-status"].classList.remove("hidden");
      return;
    }
    if (!body.changes.length) return;
    state.mtimeMs = body.mtimeMs;
    for (const ch of body.changes) {
      const a = findAsset(ch.path);
      if (!a) continue;
      a.media_intel = a.media_intel || {};
      if (ch.missing_on_disk) a.media_intel.missing_on_disk = true;
      else delete a.media_intel.missing_on_disk;
    }
    renderList();
    const selected = findAsset(state.selectedPath);
    if (selected) renderClipMeta(selected);
  } catch (err) {
    // Server unreachable -- next tick will try again.
  } finally {
    presenceCheckInFlight = false;
  }
}

// Shows which build of the editor is actually running, so a stale copy
// of these files (put back by a sync tool, a git checkout, a restored
// backup...) is obvious at a glance instead of looking identical to the
// current one. Independent of loadDb() -- a version-fetch failure should
// never block the app from opening.
async function loadVersion() {
  try {
    const res = await fetch("/api/version");
    if (!res.ok) throw new Error(`GET /api/version failed: ${res.status}`);
    const payload = await res.json();
    els["editor-version"].textContent = `v${payload.version}`;
    els["editor-version"].classList.remove("version-badge-unknown");
    const history = (payload.history || []).map((h) => `${h.version}: ${h.notes}`).join("\n");
    els["editor-version"].title = history || `Editor build ${payload.version}`;
  } catch (err) {
    els["editor-version"].textContent = "v?";
    els["editor-version"].classList.add("version-badge-unknown");
    els["editor-version"].title =
      "Could not read a version from the server -- it may be an older build (no /api/version endpoint yet) or unreachable.";
  }
}

async function init() {
  cacheEls();
  loadVersion();
  await loadDb();
  populateFilterOptions();
  populateSelectFields();
  populateMultiselectGroups();
  bindGlobalControls();
  bindEditorForm();
  renderTopbar();
  renderList();
  setInterval(checkPresence, 20000);
  window.addEventListener("focus", checkPresence);
}

function renderTopbar() {
  els["project-name"].textContent = state.db.project ? `— ${state.db.project}` : "";
  const visible = getVisibleAssets();
  els["asset-count"].textContent = `${visible.length} / ${(state.db.assets || []).length} clips`;
}

// -----------------------------------------------------------------------
// Filter option population
// -----------------------------------------------------------------------

function populateFilterOptions() {
  const assets = state.db.assets || [];
  const devices = [...new Set(assets.map((a) => a.device).filter(Boolean))].sort();
  const dates = [...new Set(assets.map((a) => (a.media_intel || {}).shoot_date).filter(Boolean))].sort();

  // Act/sequence options come from the story structure itself (in story
  // order), not from whatever values assets happen to carry.
  for (const act of [...new Set(state.structure.map((r) => r.act))]) {
    const opt = document.createElement("option");
    opt.value = act;
    opt.textContent = act;
    els["filter-act"].appendChild(opt);
  }
  populateSequenceFilter();

  for (const d of devices) {
    const opt = document.createElement("option");
    opt.value = d;
    opt.textContent = d;
    els["filter-device"].appendChild(opt);
  }
  for (const d of dates) {
    const opt = document.createElement("option");
    opt.value = d;
    opt.textContent = d;
    els["filter-date"].appendChild(opt);
  }
}

// Sequence dropdown is narrowed to the chosen act, if any.
function populateSequenceFilter() {
  const el = els["filter-sequence"];
  el.querySelectorAll("option:not([value=''])").forEach((o) => o.remove());
  for (const r of state.structure) {
    if (state.filters.act && r.act !== state.filters.act) continue;
    const opt = document.createElement("option");
    opt.value = r.sequence;
    opt.textContent = `${r.sequence} (sc. ${r.start}–${r.finish})`;
    el.appendChild(opt);
  }
  el.value = state.filters.sequence;
}

// -----------------------------------------------------------------------
// CR2: select / multi-select field setup
// -----------------------------------------------------------------------

function populateSelectFields() {
  for (const [name, options] of Object.entries(SELECT_FIELDS)) {
    const el = document.getElementById(`f-${name}`);
    if (!el) continue;
    const blank = document.createElement("option");
    blank.value = "";
    blank.textContent = "—";
    el.appendChild(blank);
    for (const opt of options) {
      const o = document.createElement("option");
      o.value = opt;
      o.textContent = opt;
      el.appendChild(o);
    }
  }
}

// Selects are constrained to the standard option list going forward, but
// an asset may already carry a value from before the field was a dropdown
// (or a value typed some other way). Rather than silently blanking that
// out when the form renders, inject it as a one-off extra option so nothing
// gets lost -- it stays selected/visible until someone deliberately picks
// something else and saves.
function ensureSelectHasValue(el, value) {
  el.querySelectorAll("option[data-temp]").forEach((o) => o.remove());
  if (!value) return;
  const exists = [...el.options].some((o) => o.value === value);
  if (exists) return;
  const opt = document.createElement("option");
  opt.value = value;
  opt.textContent = `${value} (existing, not in standard list)`;
  opt.dataset.temp = "1";
  el.appendChild(opt);
}

function populateMultiselectGroups() {
  for (const [name, options] of Object.entries(MULTISELECT_FIELDS)) {
    const container = document.getElementById(`f-${name}-group`);
    if (!container) continue;
    container.innerHTML = options.map((opt) => {
      const id = `f-${name}-${slugify(opt)}`;
      return `<label class="chip" for="${id}"><input type="checkbox" id="${id}" value="${escapeHtml(opt)}" /> ${escapeHtml(opt)}</label>`;
    }).join("");
  }
}

function fillMultiselect(name, values) {
  const container = document.getElementById(`f-${name}-group`);
  if (!container) return;
  container.querySelectorAll(".chip.extra").forEach((el) => el.remove());

  const remaining = new Set((values || []).map(String));
  container.querySelectorAll('input[type="checkbox"]').forEach((cb) => {
    cb.checked = remaining.has(cb.value);
    remaining.delete(cb.value);
  });

  // Anything left over was pre-existing data not in the standard option
  // list (e.g. hand-typed before this became a dropdown). Keep it as a
  // visible, checked, extra chip instead of dropping it on load.
  for (const val of remaining) {
    const id = `f-${name}-extra-${slugify(val)}`;
    const label = document.createElement("label");
    label.className = "chip extra";
    label.title = "Existing value not in the standard list -- kept as-is. Uncheck to remove it.";
    label.setAttribute("for", id);
    label.innerHTML = `<input type="checkbox" id="${id}" checked value="${escapeHtml(val)}" /> ${escapeHtml(val)} *`;
    container.appendChild(label);
  }
}

function collectMultiselect(name) {
  const container = document.getElementById(`f-${name}-group`);
  if (!container) return [];
  return [...container.querySelectorAll('input[type="checkbox"]:checked')].map((cb) => cb.value);
}

// -----------------------------------------------------------------------
// Scene candidate helpers
// -----------------------------------------------------------------------

// Same derivation as server.js deriveStructure(): the leading integer of
// the scene number, looked up in the story-structure ranges. The server
// is authoritative (it re-derives on save and on load); this copy only
// exists so the Act/Sequence boxes update live while a scene is typed,
// and so scene cards can show where a scene sits in the story.
function deriveStructure(scene) {
  const m = /\d+/.exec(String(scene || ""));
  if (!m) return { act: "", sequence: "" };
  const n = parseInt(m[0], 10);
  const row = state.structure.find((r) => n >= r.start && n <= r.finish);
  return row ? { act: row.act, sequence: row.sequence } : { act: "", sequence: "" };
}

function structureLabel(act, sequence) {
  return [act, sequence].filter(Boolean).join(" · ");
}

function sceneNumberOf(asset) {
  const m = /\d+(\.\d+)?/.exec(String(asset.scene || ""));
  return m ? parseFloat(m[0]) : Infinity;
}

function getProductionDay(asset) {
  return (asset.media_intel || {}).production_day || null;
}

function getCandidateScenes(asset) {
  const pd = getProductionDay(asset);
  return (pd && Array.isArray(pd.scenes)) ? pd.scenes : [];
}

function sceneStatusOf(asset) {
  const hasAssignedScene = !!(asset.scene && String(asset.scene).trim());
  if (hasAssignedScene) return "assigned";
  const pd = getProductionDay(asset);
  const scenes = getCandidateScenes(asset);
  if (!pd) return "no-candidates";
  if (scenes.length === 0) return "no-candidates"; // day description only, no scene rows
  if (scenes.length === 1) return "single-candidate";
  return "ambiguous";
}

// -----------------------------------------------------------------------
// Filtering / search / sort
// -----------------------------------------------------------------------

// `characters` is supposed to always be an array (see collectFormFields()
// below) -- but a past one-off script wrote it as a single comma-joined
// STRING on a couple hundred assets instead (a real, pre-existing data
// bug, not something this app did). Code that does `(asset.characters ||
// []).join(...)` looks safe but isn't: a non-empty string is truthy, so
// `|| []` never kicks in, and `"a string".join` doesn't exist -- that
// throws "... .join is not a function" from inside the Array.filter()
// callback in getVisibleAssets(), which aborts the whole filter and is
// exactly why the search box stopped filtering the moment the search
// term reached one of those assets (and why opening one of them directly
// in the detail pane could fail the same way). This normalizes either
// shape to an array; a legacy string is split the same way
// collectFormFields() would produce it, so the next Save on that asset
// naturally rewrites it as a proper array.
function charactersArray(value) {
  if (Array.isArray(value)) return value;
  if (typeof value === "string") {
    return value.split(",").map((s) => s.trim()).filter(Boolean);
  }
  return [];
}

function matchesSearch(asset, q) {
  if (!q) return true;
  q = q.toLowerCase();
  const pd = getProductionDay(asset);
  const haystack = [
    asset.asset_id, asset.filename, asset.path, asset.device, asset.act,
    asset.sequence, asset.scene, asset.take, asset.location, asset.notes, asset.transcript, asset.action,
    asset.dialogue, charactersArray(asset.characters).join(" "),
    pd && pd.day_description,
  ].filter(Boolean).join(" \n ").toLowerCase();
  return haystack.includes(q);
}

// sceneStatusOf() returns "assigned" once a scene is entered, and otherwise
// one of "no-candidates" / "single-candidate" / "ambiguous" describing how
// the (still-unassigned) clip matches the schedule -- there's no single
// status value it returns for "unassigned" as such, since that's really the
// union of all three not-yet-assigned outcomes. This is that union check,
// used instead of a plain equality so the "Scene not yet assigned" filter
// option actually matches something.
function matchesSceneStatusFilter(asset, filterValue) {
  if (!filterValue) return true;
  const status = sceneStatusOf(asset);
  if (filterValue === "unassigned") return status !== "assigned";
  return status === filterValue;
}

function matchesFilters(asset) {
  const f = state.filters;
  const mi = asset.media_intel || {};

  if (f.act && asset.act !== f.act) return false;
  if (f.sequence && asset.sequence !== f.sequence) return false;
  if (f.device && asset.device !== f.device) return false;
  if (f.date && mi.shoot_date !== f.date) return false;
  if (!matchesSceneStatusFilter(asset, f.sceneStatus)) return false;
  if (f.usedInEditOnly && !asset.used_in_edit) return false;
  if (f.lowConfidenceOnly && mi.detection_confidence !== "low") return false;
  if (!f.includeMissing && mi.missing_on_disk) return false;
  if (f.hideCrFloor && asset.cr_floor) return false;
  return true;
}

function getVisibleAssets() {
  const assets = state.db.assets || [];
  let visible = assets.filter((a) => matchesFilters(a) && matchesSearch(a, state.search));

  const shootDate = (a) => (a.media_intel || {}).shoot_date || "";
  const candidateCount = (a) => getCandidateScenes(a).length;

  switch (state.sort) {
    case "date-desc":
      visible.sort((a, b) => shootDate(b).localeCompare(shootDate(a)) || a.filename.localeCompare(b.filename));
      break;
    case "candidates-desc":
      visible.sort((a, b) => candidateCount(b) - candidateCount(a) || shootDate(a).localeCompare(shootDate(b)));
      break;
    case "filename":
      visible.sort((a, b) => a.filename.localeCompare(b.filename));
      break;
    case "story":
      // Script order: scene number (unassigned clips last), then take.
      visible.sort((a, b) =>
        sceneNumberOf(a) - sceneNumberOf(b) ||
        (parseFloat(a.take) || 0) - (parseFloat(b.take) || 0) ||
        a.filename.localeCompare(b.filename));
      break;
    case "date-asc":
    default:
      visible.sort((a, b) => shootDate(a).localeCompare(shootDate(b)) || a.filename.localeCompare(b.filename));
  }
  return visible;
}

// -----------------------------------------------------------------------
// Sidebar list rendering
// -----------------------------------------------------------------------

function renderList() {
  const visible = getVisibleAssets();
  renderTopbar();

  const html = visible.map((a) => {
    const mi = a.media_intel || {};
    const candCount = getCandidateScenes(a).length;
    const status = sceneStatusOf(a);
    const sceneLabel = a.scene ? `Scene ${escapeHtml(a.scene)}${a.take ? " / take " + escapeHtml(a.take) : ""}` : "— unassigned —";
    const structLabel = structureLabel(a.act, a.sequence);

    const badges = [];
    if (a.kind === "audio") badges.push(`<span class="badge kind-audio">AUDIO</span>`);
    if (mi.shoot_date) badges.push(`<span class="badge">${escapeHtml(mi.shoot_date)}</span>`);
    if (status === "ambiguous") badges.push(`<span class="badge candidates">${candCount} candidates</span>`);
    if (status === "single-candidate") badges.push(`<span class="badge candidates">1 candidate</span>`);
    if (mi.detection_confidence === "low") badges.push(`<span class="badge low-confidence">verify device</span>`);
    if (mi.missing_on_disk) badges.push(`<span class="badge missing">missing</span>`);
    if (a.used_in_edit) badges.push(`<span class="badge used">used</span>`);
    // CR2's takeStatus "override": "CR Floor" -- a CR-floored clip shows
    // that instead of its take status wherever status would appear.
    if (a.cr_floor) badges.push(`<span class="badge cr-floor">CR FLOOR</span>`);
    else if (a.take_status) badges.push(`<span class="badge take-status">${escapeHtml(a.take_status)}</span>`);

    return `
      <div class="asset-row${a.path === state.selectedPath ? " selected" : ""}${a.cr_floor ? " cr-floored" : ""}${mi.missing_on_disk ? " missing-on-disk" : ""}" data-path="${escapeHtml(a.path)}">
        <div class="row-filename">${escapeHtml(a.filename)}</div>
        <div class="row-meta">
          <span class="row-scene${a.scene ? "" : " unassigned"}">${sceneLabel}</span>
          ${structLabel ? `<span class="row-structure">${escapeHtml(structLabel)}</span>` : ""}
        </div>
        <div class="row-meta">${badges.join(" ")}</div>
      </div>`;
  }).join("");

  els["asset-list"].innerHTML = html || `<div style="padding:20px;color:var(--text-dim);">No clips match the current filters.</div>`;

  els["asset-list"].querySelectorAll(".asset-row").forEach((row) => {
    row.addEventListener("click", () => selectAsset(row.dataset.path));
  });
}

// -----------------------------------------------------------------------
// Detail pane
// -----------------------------------------------------------------------

// Looks an asset up by its file path. A handful of asset_ids in this
// project's DB are duplicated across more than one folder (the same
// physical clip cataloged twice before a cleanup pass -- see the
// media-intel-db-unification doc), so asset_id can't tell the copies
// apart; the path always can. Clicking either duplicate row opens, and
// saves to, exactly that copy.
function findAsset(assetPath) {
  if (!assetPath) return null;
  return (state.db.assets || []).find((a) => a.path === assetPath) || null;
}

function selectAsset(assetPath) {
  if (state.dirty && !confirm("You have unsaved changes on this clip. Discard them?")) return;
  state.selectedPath = assetPath;
  state.dirty = false;
  state.sceneCandidatesExpanded = false;
  renderList();
  renderDetail(findAsset(assetPath));
}

function humanDuration(asset) {
  return asset.duration || "";
}

function renderDetail(asset) {
  if (!asset) {
    els["detail-empty"].classList.remove("hidden");
    els["detail-content"].classList.add("hidden");
    return;
  }
  els["detail-empty"].classList.add("hidden");
  els["detail-content"].classList.remove("hidden");

  const mi = asset.media_intel || {};
  const tech = mi.technical || {};
  const device = mi.device || {};

  els["cr-floor-banner"].classList.toggle("hidden", !asset.cr_floor);

  els["clip-filename"].textContent = asset.filename;
  renderClipMeta(asset);

  const techBits = [
    tech.resolution_label, tech.frame_rate ? `${tech.frame_rate}fps` : "",
    tech.video_codec, tech.audio_codec ? `audio: ${tech.audio_codec}` : "",
    device.model ? `device: ${device.model}` : "",
  ].filter(Boolean);
  els["clip-tech"].textContent = techBits.join("  ·  ");
  els["clip-tech"].title = asset.path;

  renderMedia(asset);
  renderSceneBrowser(asset);
  fillEditorForm(asset);
}

// The badge line under the filename. Separate from renderDetail() so the
// background disk check can refresh "missing on disk" without re-filling
// (and discarding) whatever is being edited in the form.
function renderClipMeta(asset) {
  const mi = asset.media_intel || {};
  const dupes = (state.db.assets || []).filter((a) => a.asset_id === asset.asset_id).length;
  els["clip-meta"].innerHTML = [
    `<span>${escapeHtml(asset.asset_id)}</span>`,
    dupes > 1 ? `<span class="badge duplicate" title="This asset_id appears ${dupes} times in the DB (same clip in more than one folder). Edits here apply only to this copy's path.">${dupes} copies</span>` : "",
    asset.kind === "audio" ? `<span class="badge kind-audio">AUDIO</span>` : "",
    `<span>${escapeHtml(asset.device || "Unknown device")}</span>`,
    mi.detection_confidence === "low" ? `<span class="badge low-confidence">verify device ID</span>` : "",
    mi.shoot_date ? `<span>${escapeHtml(mi.shoot_date)}</span>` : "",
    humanDuration(asset) ? `<span>${escapeHtml(humanDuration(asset))}</span>` : "",
    mi.missing_on_disk ? `<span class="badge missing">missing on disk</span>` : "",
    asset.cr_floor ? `<span class="badge cr-floor">CR FLOOR</span>`
      : (asset.take_status ? `<span class="badge take-status">${escapeHtml(asset.take_status)}</span>` : ""),
  ].filter(Boolean).join(" ");
}

// -----------------------------------------------------------------------
// Video / audio preview
// -----------------------------------------------------------------------

function renderMedia(asset) {
  const isAudio = asset.kind === "audio";
  const shownPlayer = isAudio ? els["audio-player"] : els["video-player"];
  const hiddenPlayer = isAudio ? els["video-player"] : els["audio-player"];
  const fallback = els["video-fallback"];

  hiddenPlayer.classList.add("hidden");
  hiddenPlayer.removeAttribute("src");
  fallback.classList.add("hidden");
  shownPlayer.classList.remove("hidden");
  shownPlayer.onerror = () => {
    shownPlayer.classList.add("hidden");
    fallback.classList.remove("hidden");
    els["video-fallback-path"].textContent = asset.path;
  };
  shownPlayer.src = `/api/video?path=${encodeURIComponent(asset.path)}`;
  shownPlayer.load();
}

// -----------------------------------------------------------------------
// Scene browser
// -----------------------------------------------------------------------

function sceneCardHtml(scene, opts) {
  opts = opts || {};
  const chars = scene.characters ? `<div class="scene-card-meta">Characters: ${escapeHtml(scene.characters)}</div>` : "";
  const dateLine = opts.showDate
    ? (scene.date ? `<span>${escapeHtml(scene.date)}</span> · ` : `<span style="color:var(--warn)">not yet scheduled</span> · `)
    : "";
  const d = deriveStructure(scene.scene_number);
  const struct = structureLabel(d.act, d.sequence);
  return `
    <div class="scene-card" data-scene="${escapeHtml(JSON.stringify(scene))}">
      ${struct ? `<div class="scene-card-structure">${escapeHtml(struct)}</div>` : ""}
      <div class="scene-card-title">${dateLine}Scene ${escapeHtml(scene.scene_number || "?")} — ${escapeHtml(scene.int_ext || "")} ${escapeHtml(scene.location || "")}</div>
      <div class="scene-card-meta">${escapeHtml(scene.status || "")}${scene.script_page ? " · script p." + escapeHtml(scene.script_page) : ""}${scene.row_complete === false ? " · (partial row in schedule file)" : ""}</div>
      <div class="scene-card-desc">${escapeHtml(scene.description || "")}</div>
      ${chars}
      <div class="apply-hint">Click to fill in the editor →</div>
    </div>`;
}

// -----------------------------------------------------------------------
// Call sheets
// -----------------------------------------------------------------------
// production_day.call_sheet_reference is free text from the shoot-dates
// docx and names files by their ORIGINAL names; the PDFs in call-sheets/
// were renamed since (OC-DAY##-YYYY-MM-DD-Name.pdf). So a clip's call
// sheets are found, in order, by:
//   1. an exact (case-insensitive) match on a filename in the reference;
//   2. call-sheet-overrides.csv rows for the clip's shoot date;
//   3. any PDF whose filename contains the shoot date (YYYY-MM-DD).
// Referenced names that don't exist are listed as "not found", so a
// missing PDF is visible rather than silently absent.

function resolveCallSheets(pd, shootDate) {
  const sheets = state.callSheets || { files: [], overrides: {} };
  const links = [];
  const unresolved = [];
  const add = (file, how) => { if (!links.some((l) => l.file === file)) links.push({ file, how }); };

  const ref = (pd && pd.call_sheet_reference) || "";
  // A filename runs up to ".pdf" and can't span "(", ")", ":" or a
  // newline, so "(Revised version: X.pdf)" yields just "X.pdf".
  for (const raw of ref.match(/[A-Za-z0-9][^\n():]*?\.pdf/gi) || []) {
    const name = raw.trim();
    const hit = sheets.files.find((f) => f.toLowerCase() === name.toLowerCase());
    if (hit) add(hit, "named in the call sheet reference");
    else unresolved.push(name);
  }
  for (const f of (shootDate && sheets.overrides[shootDate]) || []) add(f, "call-sheet-overrides.csv");
  if (shootDate) {
    for (const f of sheets.files) if (f.includes(shootDate)) add(f, `filename contains shoot date ${shootDate}`);
  }
  return { ref, links, unresolved };
}

function callSheetBlockHtml(pd, shootDate) {
  const { ref, links, unresolved } = resolveCallSheets(pd, shootDate);
  if (!ref && !links.length) return "";
  const linkHtml = links.map((l) =>
    `<a href="/api/callsheet?file=${encodeURIComponent(l.file)}" class="call-sheet-link" data-file="${escapeHtml(l.file)}" title="Found via: ${escapeHtml(l.how)}. Click to view here.">${escapeHtml(l.file)}</a>`
  ).join("");
  // The docx names are the pre-rename filenames, so an unmatched name is
  // normal once a PDF has been found by date. Only flag it when nothing
  // at all was found for this clip.
  const missing = unresolved.length && !links.length
    ? `<div class="call-sheet-missing" title="If one of the PDFs in call-sheets/ is this day's sheet under another name or date, add a row for ${escapeHtml(shootDate || "this date")} to call-sheets/call-sheet-overrides.csv.">No PDF found in call-sheets/ for this day — add a row to call-sheet-overrides.csv to link one</div>`
    : "";
  return `
    <div class="call-sheet">
      ${ref ? `<div>Call sheet: ${escapeHtml(ref).replace(/\n/g, " ")}</div>` : ""}
      ${linkHtml ? `<div class="call-sheet-links">${linkHtml}</div>` : ""}
      ${missing}
    </div>`;
}

function toggleCallSheetViewer(file) {
  const viewer = document.getElementById("call-sheet-viewer");
  if (!viewer.classList.contains("hidden") && viewer.dataset.file === file) {
    closeCallSheetViewer();
    return;
  }
  const url = `/api/callsheet?file=${encodeURIComponent(file)}`;
  viewer.dataset.file = file;
  document.getElementById("call-sheet-viewer-title").textContent = file;
  document.getElementById("call-sheet-viewer-newtab").href = url;
  document.getElementById("call-sheet-frame").src = url;
  viewer.classList.remove("hidden");
  document.querySelectorAll(".call-sheet-link").forEach((a) => a.classList.toggle("active", a.dataset.file === file));
}

function closeCallSheetViewer() {
  const viewer = document.getElementById("call-sheet-viewer");
  viewer.classList.add("hidden");
  viewer.dataset.file = "";
  document.getElementById("call-sheet-frame").removeAttribute("src");
  document.querySelectorAll(".call-sheet-link.active").forEach((a) => a.classList.remove("active"));
}

function renderSceneBrowser(asset) {
  const mi = asset.media_intel || {};
  const pd = mi.production_day;

  // Sequences shot that day (from the enricher's production_day.sequences),
  // grouped under their act so the day's spread across the story is clear.
  let sequencesLine = "";
  if (pd && Array.isArray(pd.sequences) && pd.sequences.length) {
    const byAct = new Map();
    for (const seq of pd.sequences) {
      const row = state.structure.find((r) => r.sequence === seq);
      const act = row ? row.act : "—";
      if (!byAct.has(act)) byAct.set(act, []);
      byAct.get(act).push(seq.replace(/^Sequence\s+/i, ""));
    }
    sequencesLine = `<div class="day-sequences">Sequences shot this day: ${
      [...byAct].map(([act, seqs]) => `<strong>${escapeHtml(act)}</strong> ${escapeHtml(seqs.join(", "))}`).join(" · ")
    }</div>`;
  }

  const callSheetHtml = callSheetBlockHtml(pd, mi.shoot_date);

  if (pd && (pd.day_description || pd.call_sheet_reference || sequencesLine)) {
    els["scene-day-info"].innerHTML = [
      pd.day_description ? escapeHtml(pd.day_description).replace(/\n/g, "<br/>") : "",
      callSheetHtml,
      sequencesLine,
    ].filter(Boolean).join("");
  } else if (mi.shoot_date) {
    els["scene-day-info"].innerHTML = `No schedule entry for ${escapeHtml(mi.shoot_date)}. Use the search below, or assign manually.${callSheetHtml}`;
  } else {
    els["scene-day-info"].innerHTML = `This clip has no derivable shoot date, so it can't be matched to a shoot day automatically. Use the search below.`;
  }
  els["scene-day-info"].querySelectorAll(".call-sheet-link").forEach((a) => {
    a.addEventListener("click", (e) => {
      e.preventDefault();
      toggleCallSheetViewer(a.dataset.file);
    });
  });
  closeCallSheetViewer();

  const scenes = getCandidateScenes(asset);

  // Once a scene's been assigned, the candidate list that helped pick it
  // is rarely needed again -- collapse it by default so the day info and
  // scene search (still useful for re-assigning) aren't buried below a
  // long list of cards. Still one click away via the toggle. A clip with
  // no scene assigned yet always shows the full list -- that's the actual
  // worklist.
  const hasScene = !!(asset.scene && String(asset.scene).trim());
  if (hasScene) {
    const expanded = !!state.sceneCandidatesExpanded;
    els["scene-candidates-toggle-row"].classList.remove("hidden");
    els["scene-candidates-toggle"].textContent = expanded
      ? "Hide candidate scenes"
      : `Scene ${asset.scene} assigned — show ${scenes.length} candidate scene${scenes.length === 1 ? "" : "s"}`;
    els["scene-candidates"].classList.toggle("hidden", !expanded);
  } else {
    els["scene-candidates-toggle-row"].classList.add("hidden");
    els["scene-candidates"].classList.remove("hidden");
  }

  els["scene-candidates"].innerHTML = scenes.length
    ? scenes.map((s) => sceneCardHtml(s)).join("")
    : `<div style="color:var(--text-dim);font-size:13px;">No candidate scenes for this date.</div>`;

  els["scene-candidates"].querySelectorAll(".scene-card").forEach((card) => {
    card.addEventListener("click", () => applySceneToForm(JSON.parse(card.dataset.scene)));
  });

  // reset global search box per-selection
  els["scene-search-input"].value = "";
  els["scene-global-results"].innerHTML = "";
}

const runGlobalSceneSearch = debounce(() => {
  const q = els["scene-search-input"].value.trim().toLowerCase();
  const results = els["scene-global-results"];
  if (!q) { results.innerHTML = ""; return; }

  const catalog = (state.db.schedule_catalog || {}).scenes || [];
  const matches = catalog.filter((s) => {
    const d = deriveStructure(s.scene_number);
    const hay = [s.scene_number, d.act, d.sequence, s.int_ext, s.location, s.description, s.characters, s.date].filter(Boolean).join(" ").toLowerCase();
    return hay.includes(q);
  }).slice(0, 30);

  results.innerHTML = matches.length
    ? matches.map((s) => sceneCardHtml(s, { showDate: true })).join("")
    : `<div style="color:var(--text-dim);font-size:13px;">No matches.</div>`;

  results.querySelectorAll(".scene-card").forEach((card) => {
    card.addEventListener("click", () => applySceneToForm(JSON.parse(card.dataset.scene)));
  });
}, 200);

function applySceneToForm(scene) {
  document.getElementById("f-scene").value = scene.scene_number || "";
  document.getElementById("f-location").value = scene.location || "";
  if (scene.characters) {
    // "A.J.(6),BILLY(4)" -> "A.J., BILLY"
    const names = scene.characters.split(",").map((c) => c.replace(/\(\d+\)/g, "").trim()).filter(Boolean);
    document.getElementById("f-characters").value = names.join(", ");
  }
  const actionField = document.getElementById("f-action");
  if (!actionField.value.trim() && scene.description) {
    actionField.value = scene.description;
  }
  updateStructureFields();
  markDirty();
}

// Act/Sequence in the form are read-only mirrors of the Scene box. They
// aren't sent on save -- the server derives and writes them from the
// saved scene -- so what's shown here is exactly what will be stored.
function updateStructureFields() {
  const scene = els["f-scene"].value.trim();
  const d = deriveStructure(scene);
  els["f-act"].value = d.act;
  els["f-sequence"].value = d.sequence;
  const outside = scene && !d.act && state.structure.length;
  els["f-act"].placeholder = outside ? "scene outside story structure" : (state.structureError ? "structure CSV unavailable" : "from scene");
  els["f-sequence"].placeholder = outside ? "" : "from scene";
}

// -----------------------------------------------------------------------
// Editor form
// -----------------------------------------------------------------------

// Text/textarea/select fields that all read and write via plain .value.
const SIMPLE_FIELD_IDS = [
  "scene", "take", "timecode_out", "location", "shot_type", "angle",
  "action", "dialogue", "take_status", "continuity", "notes",
];
const CHECKBOX_FIELD_IDS = ["used_in_edit", "cr_floor"];
// MULTISELECT_FIELDS (quality_flags, performance_indicators) handled via
// fillMultiselect/collectMultiselect above.

function fillEditorForm(asset) {
  for (const name of SIMPLE_FIELD_IDS) {
    const el = document.getElementById(`f-${name}`);
    if (!el) continue;
    if (el.tagName === "SELECT") ensureSelectHasValue(el, asset[name]);
    el.value = asset[name] || "";
  }
  for (const name of CHECKBOX_FIELD_IDS) {
    const el = document.getElementById(`f-${name}`);
    if (el) el.checked = !!asset[name];
  }
  for (const name of Object.keys(MULTISELECT_FIELDS)) {
    fillMultiselect(name, asset[name]);
  }
  document.getElementById("f-characters").value = charactersArray(asset.characters).join(", ");
  els["f-transcript"].value = asset.transcript || "";
  updateStructureFields();
  setDirty(false);
}

function collectFormFields() {
  const fields = {};
  for (const name of SIMPLE_FIELD_IDS) {
    const el = document.getElementById(`f-${name}`);
    if (!el) continue;
    fields[name] = el.value;
  }
  for (const name of CHECKBOX_FIELD_IDS) {
    const el = document.getElementById(`f-${name}`);
    if (el) fields[name] = el.checked;
  }
  for (const name of Object.keys(MULTISELECT_FIELDS)) {
    fields[name] = collectMultiselect(name);
  }
  fields.characters = document.getElementById("f-characters").value
    .split(",").map((s) => s.trim()).filter(Boolean);
  fields.transcript = els["f-transcript"].value;
  return fields;
}

function markDirty() { setDirty(true); }

function setDirty(isDirty) {
  state.dirty = isDirty;
  els["dirty-indicator"].textContent = isDirty ? "Unsaved changes" : "";
  els["save-btn"].disabled = !isDirty;
}

function flashPathStatus(msg, isError) {
  els["clip-path-status"].textContent = msg;
  els["clip-path-status"].classList.toggle("error", !!isError);
  setTimeout(() => {
    if (els["clip-path-status"].textContent === msg) els["clip-path-status"].textContent = "";
  }, 2500);
}

function bindEditorForm() {
  els["editor-form"].addEventListener("input", markDirty);
  els["editor-form"].addEventListener("change", markDirty);
  els["f-transcript"].addEventListener("input", markDirty);
  els["f-scene"].addEventListener("input", updateStructureFields);
  els["save-btn"].addEventListener("click", saveCurrentAsset);
  els["scene-search-input"].addEventListener("input", runGlobalSceneSearch);

  document.getElementById("call-sheet-viewer-close").addEventListener("click", closeCallSheetViewer);

  els["scene-candidates-toggle"].addEventListener("click", () => {
    state.sceneCandidatesExpanded = !state.sceneCandidatesExpanded;
    const asset = findAsset(state.selectedPath);
    if (asset) renderSceneBrowser(asset);
  });

  els["copy-path-btn"].addEventListener("click", async () => {
    const asset = findAsset(state.selectedPath);
    if (!asset) return;
    try {
      await navigator.clipboard.writeText(asset.path);
      flashPathStatus("Copied");
    } catch (err) {
      flashPathStatus("Couldn't copy -- select the path from the tooltip instead", true);
    }
  });

  els["reveal-btn"].addEventListener("click", async () => {
    const asset = findAsset(state.selectedPath);
    if (!asset) return;
    els["reveal-btn"].disabled = true;
    try {
      const res = await fetch("/api/reveal", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: asset.path }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.error || `reveal failed: ${res.status}`);
      }
      const body = await res.json();
      flashPathStatus(body.fileSelected
        ? "Opened in Explorer"
        : `File not on disk -- opened ${body.opened} instead`, !body.fileSelected);
    } catch (err) {
      flashPathStatus(err.message, true);
    } finally {
      els["reveal-btn"].disabled = false;
    }
  });

  // CR1: "nuclear" confirm before flipping CR Floor on. Unchecking (i.e.
  // pulling something back off the floor) needs no confirmation.
  const crFloorEl = document.getElementById("f-cr_floor");
  if (crFloorEl) {
    crFloorEl.addEventListener("change", (e) => {
      if (e.target.checked) {
        const ok = confirm(
          "Mark this ENTIRE shot as CR Floor (cutting-room floor)?\n\n" +
          "This flags it as rejected/unusable everywhere this app shows it. " +
          "It won't write anything until you click \"Save changes\" -- you can still uncheck it first."
        );
        if (!ok) { e.target.checked = false; return; }
      }
      els["cr-floor-banner"].classList.toggle("hidden", !e.target.checked);
    });
  }
}

async function saveCurrentAsset() {
  const asset = findAsset(state.selectedPath);
  if (!asset) return;

  els["save-btn"].disabled = true;
  els["save-indicator"].textContent = "Saving...";

  try {
    const res = await fetch("/api/asset", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        asset_id: asset.asset_id,
        path: asset.path,
        fields: collectFormFields(),
        expectedMtimeMs: state.mtimeMs,
      }),
    });

    if (res.status === 409) {
      els["save-indicator"].textContent = "";
      const body = await res.json();
      alert(
        "The DB file on disk changed since you loaded it (a rescan/enrichment run, or a git pull?). " +
        "Reloading now so you don't overwrite it -- please re-apply your edit."
      );
      await loadDb();
      renderList();
      renderDetail(findAsset(state.selectedPath));
      return;
    }

    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.error || `save failed: ${res.status}`);
    }

    const body = await res.json();
    state.mtimeMs = body.mtimeMs;
    const idx = state.db.assets.findIndex((a) => a.path === asset.path);
    if (idx >= 0) state.db.assets[idx] = body.asset;

    setDirty(false);
    updateStructureFields();
    els["save-indicator"].textContent = "Saved";
    setTimeout(() => { if (els["save-indicator"].textContent === "Saved") els["save-indicator"].textContent = ""; }, 1500);
    renderList();
  } catch (err) {
    els["save-indicator"].textContent = "";
    alert(`Could not save: ${err.message}`);
    els["save-btn"].disabled = false;
  }
}

// -----------------------------------------------------------------------
// Global controls
// -----------------------------------------------------------------------

function bindGlobalControls() {
  els["search-input"].addEventListener("input", debounce((e) => {
    state.search = e.target.value;
    renderList();
  }, 150));

  els["filter-act"].addEventListener("change", (e) => {
    state.filters.act = e.target.value;
    // Drop a sequence choice that doesn't belong to the newly chosen act.
    const row = state.structure.find((r) => r.sequence === state.filters.sequence);
    if (state.filters.act && row && row.act !== state.filters.act) state.filters.sequence = "";
    populateSequenceFilter();
    renderList();
  });
  els["filter-sequence"].addEventListener("change", (e) => { state.filters.sequence = e.target.value; renderList(); });
  els["filter-device"].addEventListener("change", (e) => { state.filters.device = e.target.value; renderList(); });
  els["filter-date"].addEventListener("change", (e) => { state.filters.date = e.target.value; renderList(); });
  els["filter-scene-status"].addEventListener("change", (e) => { state.filters.sceneStatus = e.target.value; renderList(); });
  els["filter-used-in-edit"].addEventListener("change", (e) => { state.filters.usedInEditOnly = e.target.checked; renderList(); });
  els["filter-low-confidence"].addEventListener("change", (e) => { state.filters.lowConfidenceOnly = e.target.checked; renderList(); });
  els["filter-missing"].addEventListener("change", (e) => { state.filters.includeMissing = e.target.checked; renderList(); });
  els["filter-hide-cr-floor"].addEventListener("change", (e) => { state.filters.hideCrFloor = e.target.checked; renderList(); });
  els["sort-select"].addEventListener("change", (e) => { state.sort = e.target.value; renderList(); });

  els["reload-btn"].addEventListener("click", async () => {
    if (state.dirty && !confirm("You have unsaved changes. Discard and reload from disk?")) return;
    await loadDb();
    setDirty(false);
    renderList();
    renderDetail(findAsset(state.selectedPath));
  });
}

init().catch((err) => {
  document.body.innerHTML = `<div style="padding:40px;color:#e0605c;font-family:monospace;">Failed to start: ${escapeHtml(err.message)}<br/><br/>Is the server pointed at the right --db path?</div>`;
  console.error(err);
});
