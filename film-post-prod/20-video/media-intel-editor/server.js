#!/usr/bin/env node
"use strict";

/**
 * Media Intelligence Editor -- tiny local server.
 *
 * Zero dependencies (Node built-ins only: http, fs, path, url). Nothing
 * to `npm install`. Run it with `node server.js` (or `npm start`) and
 * open the printed URL.
 *
 * What it does:
 *   - Serves the static frontend (public/).
 *   - GET  /api/db            -> reads orange-crush-media-intel-db.json fresh
 *                                 off disk every time and returns it,
 *                                 plus the file's mtime for optimistic
 *                                 concurrency. Video and audio assets share
 *                                 this one file/DB; each asset's "kind"
 *                                 field says which it is. Before returning,
 *                                 it runs two derived-data syncs and, if
 *                                 either changed anything, writes the file
 *                                 back (see syncPresence / syncStructure):
 *                                   - media_intel.missing_on_disk is checked
 *                                     against the actual disk, so removing a
 *                                     file/folder shows up on the next load
 *                                     or Reload without a rescan;
 *                                   - act / sequence are re-derived from
 *                                     each asset's scene via the story-
 *                                     structure CSV.
 *   - POST /api/presence      -> the same on-disk check as above, on its
 *                                 own. The browser polls this so a file
 *                                 removed while the editor is open gets
 *                                 flagged without a manual Reload. Only
 *                                 writes if the browser's mtime is current.
 *   - POST /api/asset         -> patches ONE asset's editable fields and
 *                                 writes the file back atomically. Rejects
 *                                 with 409 if the file changed on disk
 *                                 since the browser last loaded it (e.g.
 *                                 you re-ran the scanner or enricher
 *                                 while the editor was open), so a save
 *                                 can never silently clobber a rescan.
 *   - GET  /api/video?path=.. -> streams a camera-original or recorder-
 *                                 original file straight off disk (Range-
 *                                 request aware, for scrubbing) so you can
 *                                 preview a clip -- or play back a .wav --
 *                                 while deciding which scene it is. Only
 *                                 a path that exactly matches an asset
 *                                 already in the DB will be served --
 *                                 this is a local single-user tool, but
 *                                 there's no reason to let it read
 *                                 arbitrary files off your disk. (Name is
 *                                 historical; it serves audio too now.)
 *   - POST /api/reveal        -> opens Windows Explorer with the given
 *                                 clip's file selected ("Show in Explorer"
 *                                 in the UI). Same known-path check as
 *                                 /api/video. Windows only -- returns 501
 *                                 on any other platform.
 *   - GET  /api/version        -> returns EDITOR_VERSION + VERSION_HISTORY
 *                                 below. The topbar badge fetches this on
 *                                 load. If the badge ever shows an older
 *                                 version than you expect (or no badge at
 *                                 all), the files on disk are stale --
 *                                 something reverted them (a sync tool, a
 *                                 git checkout/pull, an old backup restore)
 *                                 -- not a browser caching quirk.
 *
 * The JSON file itself stays a normal file on disk -- this app never
 * touches git, and only ever rewrites the one file you point it at.
 * Editorial fields are the only thing a save ever changes; everything
 * else (media_intel, schedule_catalog, ...) round-trips untouched.
 *
 * Usage:
 *   node server.js [--db <path-to-orange-crush-media-intel-db.json>]
 *                  [--structure <path-to-orange-crush-story-structure.csv>]
 *                  [--port 5173]
 *
 * Defaults: --db ../orange-crush-media-intel-db.json and --structure
 * ../orange-crush-story-structure.csv (both relative to this file, i.e.
 * the 20-video folder if you keep this tool at
 * 20-video/media-intel-editor/), --port 5173.
 */

const http = require("http");
const fs = require("fs");
const path = require("path");
const { URL } = require("url");
const { execFile } = require("child_process");

// ---------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------

function parseArgs(argv) {
  const args = { db: null, structure: null, callSheets: null, port: 5173 };
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === "--db") args.db = argv[++i];
    else if (argv[i] === "--structure") args.structure = argv[++i];
    else if (argv[i] === "--call-sheets") args.callSheets = argv[++i];
    else if (argv[i] === "--port") args.port = parseInt(argv[++i], 10);
  }
  return args;
}

const cli = parseArgs(process.argv.slice(2));
const DB_PATH = path.resolve(cli.db || path.join(__dirname, "..", "orange-crush-media-intel-db.json"));
const STRUCTURE_PATH = path.resolve(cli.structure || path.join(__dirname, "..", "orange-crush-story-structure.csv"));
const CALL_SHEETS_DIR = path.resolve(cli.callSheets || path.join(__dirname, "..", "call-sheets"));
const CALL_SHEET_OVERRIDES_PATH = path.join(CALL_SHEETS_DIR, "call-sheet-overrides.csv");
const PORT = cli.port || 5173;
const PUBLIC_DIR = path.join(__dirname, "public");

// ---------------------------------------------------------------------
// Version
// ---------------------------------------------------------------------
// Bump EDITOR_VERSION (and add a line to VERSION_HISTORY) with every
// change to server.js or public/*. GET /api/version exposes it, and the
// topbar badge fetches it on load -- so at a glance you can tell whether
// what's on screen is actually the build you think it is, rather than an
// older copy that got put back on disk by something else (a sync tool, a
// git checkout, a restored backup, etc.).
const EDITOR_VERSION = "2026.09.23-1";
const VERSION_HISTORY = [
  { version: "2026.09.23-1", notes: "Call sheets in the scene browser are now links that open the PDF in a viewer inside the editor (or a new tab). Served from 20-video/call-sheets/. The PDFs were renamed after the shoot-dates docx was written, so each clip's call sheet is found by the exact referenced filename, then by a PDF whose filename contains the clip's shoot date, then by call-sheets/call-sheet-overrides.csv (Shoot_Date,File) for rescheduled or renamed days. A reference with no PDF found is shown as 'not found'." },
  { version: "2026.09.22-3", notes: "(Supersedes a 22-2 Explorer fix recorded in the notes that never reached disk.) Show in Explorer now lands on the clip's folder: Node was wrapping the whole '/select,<path>' argument in quotes, which explorer.exe can't parse, so it fell back to Documents. The argument is now passed verbatim as /select,\"<path>\", and if the file itself is gone it opens the nearest folder that still exists. Not-on-disk status is now live: every load/Reload (and a background check every 20s and on window focus) compares each asset's path against the disk and updates media_intel.missing_on_disk -- previously only a scanner rerun ever set it. A drive that isn't connected at all is reported as offline rather than flagging every clip on it as missing. Duplicate asset_ids: selection and saves are now keyed on the exact file path, so the right copy is opened and saved. New derived act / sequence fields (from orange-crush-story-structure.csv, keyed on scene): shown read-only next to Scene, recomputed by the server on every save and on load, searchable, filterable, and a new story-order sort." },
  { version: "2026.09.22-1", notes: "Fixed the search box (and, for the same reason, opening certain clips in the detail pane) throwing and silently stopping mid-filter. Root cause: a past one-off script had saved `characters` as a comma-joined string instead of an array on ~186 assets; code that assumed it was always an array (`(asset.characters || []).join(...)`) threw 'not a function' on those specific assets the moment a search reached them, since a non-empty string is truthy and skips the `|| []` fallback. Added charactersArray() to normalize either shape wherever `characters` is read; a legacy string self-heals into a proper array the next time that asset is saved." },
  { version: "2026.09.21-2", notes: "Fixed the 'Scene not yet assigned' filter -- it never matched anything because sceneStatusOf() had no 'unassigned' status value to compare against (only 'assigned' plus the three candidate-count outcomes). It's now the union of those three." },
  { version: "2026.09.21-1", notes: "Scene browser auto-collapses once a scene is assigned (with a toggle to re-open it); Copy path + Show in Explorer buttons on the clip header; version badge added." },
  { version: "2026.09.20-2", notes: "Device-label suffix for recovered/unconfirmed files shortened to [!], with a legend under the Device filter." },
  { version: "2026.09.20-1", notes: "First tracked build: asset editor, scene browser, transcript viewer." },
];

// Fields an editor is allowed to change from the UI. Everything else in
// an asset record (asset_id, filename, path, device, kind, duration,
// timecode_in, media_intel, ...) is read-only from this app's point of
// view -- it's owned by the scanner/enricher scripts.
const EDITABLE_FIELDS = new Set([
  "scene",
  "take",
  "timecode_out",
  "characters",
  "location",
  "shot_type",
  "angle",
  "action",
  "dialogue",
  "take_status",
  "quality_flags",
  "performance_indicators",
  "continuity",
  "notes",
  "transcript",
  "used_in_edit",
  "cr_floor",
]);

// ---------------------------------------------------------------------
// DB read/write helpers
// ---------------------------------------------------------------------

function readDb() {
  const raw = fs.readFileSync(DB_PATH, "utf-8");
  const stat = fs.statSync(DB_PATH);
  return { db: JSON.parse(raw), mtimeMs: stat.mtimeMs };
}

function writeDbAtomic(db) {
  const tmpPath = DB_PATH + ".tmp";
  fs.writeFileSync(tmpPath, JSON.stringify(db, null, 2) + "\n", "utf-8");
  fs.renameSync(tmpPath, DB_PATH);
  return fs.statSync(DB_PATH).mtimeMs;
}

// ---------------------------------------------------------------------
// Story structure: act / sequence, derived from scene
// ---------------------------------------------------------------------
// `act` and `sequence` are never typed by hand. They're a pure function of
// an asset's `scene` number plus orange-crush-story-structure.csv (one row
// per sequence: Act,Sequence,Start_Scene,Finish_Scene, inclusive). That
// CSV is the single source of truth -- it agrees with the `seq` column in
// the production strips -- and keeping these fields derived means they can
// never drift from the scene they describe:
//   - every save that touches `scene` re-derives them (handlePostAsset);
//   - every load re-derives them for all assets (syncStructure), which
//     also catches a scene written by another tool (e.g. the scene
//     reconciler) or an edit to the CSV itself.
// If the CSV is missing or invalid, act/sequence are left exactly as they
// are -- a bad CSV never blanks them out.

function loadStructure() {
  const result = { path: STRUCTURE_PATH, rows: [], error: null };
  let text;
  try {
    text = fs.readFileSync(STRUCTURE_PATH, "utf-8").replace(/^﻿/, "");
  } catch (err) {
    result.error = `Could not read story structure CSV: ${err.message}`;
    return result;
  }
  const lines = text.split(/\r?\n/).map((l) => l.trim()).filter(Boolean);
  const header = (lines.shift() || "").split(",").map((h) => h.trim().toLowerCase());
  const col = {
    act: header.indexOf("act"),
    sequence: header.indexOf("sequence"),
    start: header.indexOf("start_scene"),
    finish: header.indexOf("finish_scene"),
  };
  if (Object.values(col).some((i) => i < 0)) {
    result.error = "Story structure CSV must have columns Act,Sequence,Start_Scene,Finish_Scene.";
    return result;
  }
  for (const line of lines) {
    const cells = line.split(",").map((c) => c.trim());
    const row = {
      act: cells[col.act] || "",
      sequence: cells[col.sequence] || "",
      start: parseInt(cells[col.start], 10),
      finish: parseInt(cells[col.finish], 10),
    };
    if (!row.act || !row.sequence || isNaN(row.start) || isNaN(row.finish) || row.start > row.finish) {
      result.error = `Bad story structure row: "${line}"`;
      return result;
    }
    const overlap = result.rows.find((r) => row.start <= r.finish && r.start <= row.finish);
    if (overlap) {
      result.error = `Story structure rows overlap: ${overlap.sequence} (${overlap.start}-${overlap.finish}) and ${row.sequence} (${row.start}-${row.finish})`;
      return result;
    }
    result.rows.push(row);
  }
  if (!result.rows.length) result.error = "Story structure CSV has no rows.";
  return result;
}

// Scene numbers are strings ("153", "9.1", maybe "32A" one day) -- the
// leading integer is what places a scene in the structure.
function deriveStructure(rows, scene) {
  const m = /\d+/.exec(String(scene || ""));
  if (!m) return { act: "", sequence: "" };
  const n = parseInt(m[0], 10);
  const row = rows.find((r) => n >= r.start && n <= r.finish);
  return row ? { act: row.act, sequence: row.sequence } : { act: "", sequence: "" };
}

// Returns a copy of the asset with act/sequence set, placed immediately
// before `scene` so the JSON reads act -> sequence -> scene -> take.
function withStructureFields(asset, act, sequence) {
  const out = {};
  let placed = false;
  for (const [k, v] of Object.entries(asset)) {
    if (k === "act" || k === "sequence") continue;
    if (k === "scene" && !placed) {
      out.act = act;
      out.sequence = sequence;
      placed = true;
    }
    out[k] = v;
  }
  if (!placed) {
    out.act = act;
    out.sequence = sequence;
  }
  return out;
}

// Re-derives act/sequence for every asset in place. Returns how many changed.
function syncStructure(db, structure) {
  if (structure.error) return 0;
  const assets = db.assets || [];
  let updated = 0;
  for (let i = 0; i < assets.length; i++) {
    const a = assets[i];
    const d = deriveStructure(structure.rows, a.scene);
    if (!("act" in a) || !("sequence" in a) || a.act !== d.act || a.sequence !== d.sequence) {
      assets[i] = withStructureFields(a, d.act, d.sequence);
      updated++;
    }
  }
  return updated;
}

// ---------------------------------------------------------------------
// Call sheets
// ---------------------------------------------------------------------
// media_intel.production_day.call_sheet_reference is free text copied
// from the shoot-dates docx, e.g. "OC-DAY-13-OFFICE-REDUX-01-14-2023.pdf
// (Revised version: ...b.pdf)". The PDFs in call-sheets/ were renamed
// afterwards (OC-DAY##-YYYY-MM-DD-Name.pdf), so most references don't
// name a file that exists. The browser resolves each clip's links from
// this listing (see resolveCallSheets() in app.js); the server only lists
// what's there and serves a PDF that's in that listing.
//
// call-sheet-overrides.csv (Shoot_Date,File) pins a PDF to a shoot date
// when neither the referenced name nor the date in the filename matches,
// e.g. a day that was rescheduled after its call sheet was issued.

function loadCallSheets() {
  const result = { dir: CALL_SHEETS_DIR, files: [], overrides: {}, error: null };
  try {
    result.files = fs.readdirSync(CALL_SHEETS_DIR).filter((f) => /\.pdf$/i.test(f)).sort();
  } catch (err) {
    result.error = `Could not read call sheets folder: ${err.message}`;
    return result;
  }
  let text = "";
  try {
    text = fs.readFileSync(CALL_SHEET_OVERRIDES_PATH, "utf-8").replace(/^﻿/, "");
  } catch (err) {
    return result; // no overrides file -- fine
  }
  const lines = text.split(/\r?\n/).map((l) => l.trim()).filter((l) => l && !l.startsWith("#"));
  lines.shift(); // header
  for (const line of lines) {
    const i = line.indexOf(",");
    if (i < 0) continue;
    const date = line.slice(0, i).trim();
    const file = line.slice(i + 1).trim();
    if (!result.files.includes(file)) {
      console.log(`  WARNING: call-sheet-overrides.csv names ${file}, which isn't in ${CALL_SHEETS_DIR}`);
      continue;
    }
    (result.overrides[date] = result.overrides[date] || []).push(file);
  }
  return result;
}

// ---------------------------------------------------------------------
// Disk presence: media_intel.missing_on_disk
// ---------------------------------------------------------------------
// The scanner sets this flag when it runs, but nothing refreshed it after
// that -- so deleting a duplicate clip or folder left the DB claiming it
// was still there until the next full rescan. This checks every asset's
// path against the disk (about 10 ms for ~1,100 files) and updates the
// flag the same way the scanner does: `true` when absent, key removed
// when present.
//
// A drive root that isn't reachable at all (external drive unplugged) is
// reported as offline and its assets are left untouched -- otherwise
// unplugging E: would flag every clip on it as missing.

function syncPresence(db) {
  const rootOnline = new Map();
  const offlineRoots = new Set();
  const changes = [];
  let missingCount = 0;

  for (const a of db.assets || []) {
    if (!a.path) continue;
    const root = path.parse(a.path).root;
    if (!rootOnline.has(root)) rootOnline.set(root, !!root && fs.existsSync(root));
    if (!rootOnline.get(root)) {
      offlineRoots.add(root || "(no root)");
      if ((a.media_intel || {}).missing_on_disk) missingCount++;
      continue;
    }
    const isMissing = !fs.existsSync(a.path);
    const wasMissing = !!(a.media_intel || {}).missing_on_disk;
    if (isMissing) missingCount++;
    if (isMissing === wasMissing) continue;
    if (isMissing) {
      a.media_intel = a.media_intel || {};
      a.media_intel.missing_on_disk = true;
    } else {
      delete a.media_intel.missing_on_disk;
    }
    changes.push({ asset_id: a.asset_id, path: a.path, missing_on_disk: isMissing });
  }
  return { changes, missingCount, offlineRoots: [...offlineRoots] };
}

// ---------------------------------------------------------------------
// Small HTTP helpers
// ---------------------------------------------------------------------

function sendJson(res, status, obj) {
  const body = JSON.stringify(obj);
  res.writeHead(status, {
    "Content-Type": "application/json; charset=utf-8",
    "Content-Length": Buffer.byteLength(body),
  });
  res.end(body);
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    let chunks = [];
    let size = 0;
    req.on("data", (c) => {
      size += c.length;
      if (size > 10 * 1024 * 1024) {
        reject(new Error("request body too large"));
        req.destroy();
        return;
      }
      chunks.push(c);
    });
    req.on("end", () => resolve(Buffer.concat(chunks).toString("utf-8")));
    req.on("error", reject);
  });
}

const STATIC_CONTENT_TYPES = {
  ".html": "text/html; charset=utf-8",
  ".js": "application/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".svg": "image/svg+xml",
  ".ico": "image/x-icon",
};

const VIDEO_CONTENT_TYPES = {
  ".mp4": "video/mp4",
  ".m4v": "video/mp4",
  ".mov": "video/quicktime",
  ".mxf": "application/mxf",
  ".avi": "video/x-msvideo",
  ".wav": "audio/wav", // ZOOM H4n Pro recordings -- served through the same /api/video
                        // endpoint as camera clips; the name is historical (it just
                        // streams whatever known asset path it's given, video or audio).
};

function serveStatic(req, res, urlPath) {
  let rel = urlPath === "/" ? "/index.html" : urlPath;
  const filePath = path.join(PUBLIC_DIR, rel);
  // guard against escaping the public dir
  if (!filePath.startsWith(PUBLIC_DIR)) {
    res.writeHead(403);
    res.end("forbidden");
    return;
  }
  fs.readFile(filePath, (err, data) => {
    if (err) {
      res.writeHead(404);
      res.end("not found");
      return;
    }
    const ext = path.extname(filePath).toLowerCase();
    res.writeHead(200, {
      "Content-Type": STATIC_CONTENT_TYPES[ext] || "application/octet-stream",
    });
    res.end(data);
  });
}

// ---------------------------------------------------------------------
// API handlers
// ---------------------------------------------------------------------

function handleGetDb(req, res) {
  let db, mtimeMs;
  try {
    ({ db, mtimeMs } = readDb());
  } catch (err) {
    sendJson(res, 500, { error: `Could not read DB: ${err.message}` });
    return;
  }

  // A load is by definition a fresh read, so the browser can't be holding
  // a stale copy that this write would invalidate -- it gets the new mtime
  // in this same response.
  const structure = loadStructure();
  const presence = syncPresence(db);
  const structureUpdated = syncStructure(db, structure);
  if (presence.changes.length || structureUpdated) {
    try {
      mtimeMs = writeDbAtomic(db);
    } catch (err) {
      sendJson(res, 500, { error: `Could not write DB after sync: ${err.message}` });
      return;
    }
    console.log(
      `  Load sync: ${presence.changes.length} missing-on-disk flag(s) changed, ` +
      `${structureUpdated} asset(s) had act/sequence re-derived.`
    );
  }
  if (structure.error) console.log(`  WARNING: ${structure.error} -- act/sequence left as-is.`);

  res.setHeader("X-Db-Mtime", String(mtimeMs));
  sendJson(res, 200, {
    db,
    mtimeMs,
    structure: { path: structure.path, rows: structure.rows, error: structure.error },
    callSheets: loadCallSheets(),
    sync: {
      presenceChanges: presence.changes.length,
      missingCount: presence.missingCount,
      offlineRoots: presence.offlineRoots,
      structureUpdated,
    },
  });
}

async function handlePostPresence(req, res) {
  let payload;
  try {
    payload = JSON.parse((await readBody(req)) || "{}");
  } catch (err) {
    sendJson(res, 400, { error: `Bad JSON body: ${err.message}` });
    return;
  }

  let db, mtimeMs;
  try {
    ({ db, mtimeMs } = readDb());
  } catch (err) {
    sendJson(res, 500, { error: `Could not read DB: ${err.message}` });
    return;
  }

  const presence = syncPresence(db);
  const summary = { missingCount: presence.missingCount, offlineRoots: presence.offlineRoots };

  // The browser's copy is out of date (a rescan, a git pull...). Don't
  // write -- persisting here would hand the browser a new mtime and hide
  // the fact that something else changed the file. Just say so.
  if (typeof payload.expectedMtimeMs === "number" && Math.abs(mtimeMs - payload.expectedMtimeMs) > 1) {
    sendJson(res, 200, { stale: true, changes: [], mtimeMs, ...summary });
    return;
  }

  if (presence.changes.length) {
    try {
      mtimeMs = writeDbAtomic(db);
    } catch (err) {
      sendJson(res, 500, { error: `Could not write DB: ${err.message}` });
      return;
    }
    console.log(`  Presence check: ${presence.changes.length} missing-on-disk flag(s) changed.`);
  }
  sendJson(res, 200, { stale: false, changes: presence.changes, mtimeMs, ...summary });
}

async function handlePostAsset(req, res) {
  let payload;
  try {
    payload = JSON.parse(await readBody(req));
  } catch (err) {
    sendJson(res, 400, { error: `Bad JSON body: ${err.message}` });
    return;
  }

  const { asset_id, path: assetPath, fields, expectedMtimeMs } = payload || {};
  if (!asset_id || typeof fields !== "object" || fields === null) {
    sendJson(res, 400, { error: "Body must include asset_id and fields." });
    return;
  }

  let db, mtimeMs;
  try {
    ({ db, mtimeMs } = readDb());
  } catch (err) {
    sendJson(res, 500, { error: `Could not read DB: ${err.message}` });
    return;
  }

  if (typeof expectedMtimeMs === "number" && Math.abs(mtimeMs - expectedMtimeMs) > 1) {
    // The file changed on disk since the browser last loaded it (a
    // rescan, an enrichment run, a git pull...). Refuse to save blind.
    sendJson(res, 409, {
      error: "The DB file changed on disk since you loaded it. Reload and re-apply your edit.",
      currentMtimeMs: mtimeMs,
    });
    return;
  }

  // Some asset_ids in this DB are duplicated across more than one folder
  // (the same physical clip cataloged from two+ locations before a
  // cleanup pass -- see the media-intel-db-unification doc), so asset_id
  // alone is ambiguous. The browser sends the exact path of the record it
  // has open, and that's what's matched. Without a path (an older
  // client), prefer whichever asset_id match is still on disk.
  const assets = db.assets || [];
  let index = assetPath
    ? assets.findIndex((a) => a.asset_id === asset_id && a.path === assetPath)
    : -1;
  if (index < 0 && !assetPath) {
    const candidates = assets
      .map((a, i) => ({ a, i }))
      .filter(({ a }) => a.asset_id === asset_id);
    const pick = candidates.find(({ a }) => !(a.media_intel || {}).missing_on_disk) || candidates[0];
    index = pick ? pick.i : -1;
  }
  if (index < 0) {
    sendJson(res, 404, { error: `asset not found: ${asset_id}${assetPath ? ` at ${assetPath}` : ""}` });
    return;
  }

  let asset = assets[index];
  for (const [key, value] of Object.entries(fields)) {
    if (!EDITABLE_FIELDS.has(key)) continue; // silently ignore anything not editor-owned
    asset[key] = value;
  }

  // act/sequence are derived, never taken from the request -- see
  // loadStructure(). Re-derive whenever the save touched scene.
  if ("scene" in fields) {
    const structure = loadStructure();
    if (!structure.error) {
      const d = deriveStructure(structure.rows, asset.scene);
      asset = withStructureFields(asset, d.act, d.sequence);
      assets[index] = asset;
    }
  }

  let newMtimeMs;
  try {
    newMtimeMs = writeDbAtomic(db);
  } catch (err) {
    sendJson(res, 500, { error: `Could not write DB: ${err.message}` });
    return;
  }

  sendJson(res, 200, { ok: true, asset, mtimeMs: newMtimeMs });
}

function handleGetVideo(req, res, reqUrl) {
  const requestedPath = reqUrl.searchParams.get("path");
  if (!requestedPath) {
    res.writeHead(400);
    res.end("missing path");
    return;
  }

  let db;
  try {
    ({ db } = readDb());
  } catch (err) {
    res.writeHead(500);
    res.end("could not read DB");
    return;
  }

  const known = (db.assets || []).some((a) => a.path === requestedPath);
  if (!known) {
    // Only ever serve a file that is literally listed as an asset path
    // in the current DB -- keeps this from becoming a generic file server.
    res.writeHead(403);
    res.end("path is not a known asset in the DB");
    return;
  }

  fs.stat(requestedPath, (err, stat) => {
    if (err) {
      res.writeHead(404);
      res.end("file not found on disk (drive not connected? file moved?)");
      return;
    }

    const ext = path.extname(requestedPath).toLowerCase();
    const contentType = VIDEO_CONTENT_TYPES[ext] || "application/octet-stream";
    const range = req.headers.range;

    if (!range) {
      res.writeHead(200, {
        "Content-Type": contentType,
        "Content-Length": stat.size,
        "Accept-Ranges": "bytes",
      });
      fs.createReadStream(requestedPath).pipe(res);
      return;
    }

    const match = /bytes=(\d*)-(\d*)/.exec(range);
    let start = match && match[1] ? parseInt(match[1], 10) : 0;
    let end = match && match[2] ? parseInt(match[2], 10) : stat.size - 1;
    if (isNaN(start) || isNaN(end) || start > end || end >= stat.size) {
      res.writeHead(416, { "Content-Range": `bytes */${stat.size}` });
      res.end();
      return;
    }

    res.writeHead(206, {
      "Content-Type": contentType,
      "Content-Range": `bytes ${start}-${end}/${stat.size}`,
      "Accept-Ranges": "bytes",
      "Content-Length": end - start + 1,
    });
    fs.createReadStream(requestedPath, { start, end }).pipe(res);
  });
}

async function handleRevealPath(req, res) {
  let payload;
  try {
    payload = JSON.parse(await readBody(req));
  } catch (err) {
    sendJson(res, 400, { error: `Bad JSON body: ${err.message}` });
    return;
  }

  const requestedPath = payload && payload.path;
  if (!requestedPath) {
    sendJson(res, 400, { error: "missing path" });
    return;
  }

  let db;
  try {
    ({ db } = readDb());
  } catch (err) {
    sendJson(res, 500, { error: `Could not read DB: ${err.message}` });
    return;
  }

  // Same guard as /api/video -- only ever act on a path that's already a
  // known asset, not an arbitrary path the browser hands us.
  const known = (db.assets || []).some((a) => a.path === requestedPath);
  if (!known) {
    sendJson(res, 403, { error: "path is not a known asset in the DB" });
    return;
  }

  if (process.platform !== "win32") {
    sendJson(res, 501, {
      error: "Show-in-Explorer only works on Windows -- this server isn't running on win32.",
    });
    return;
  }

  // explorer.exe does its own command-line parsing and only understands
  // /select,"<path>" -- with the quotes around the path, not around the
  // whole argument. By default Node quotes any argv element containing a
  // space, so every path under "E:\Orange Crush\..." used to reach
  // Explorer as "/select,E:\Orange Crush\...": Explorer couldn't parse
  // that and silently opened the default (Documents) folder instead.
  // windowsVerbatimArguments hands the string over exactly as built here.
  // That's safe without a shell: the path is already verified to be a
  // known asset path, and Windows paths can't contain a double quote.
  //
  // If the file itself is gone (a removed duplicate, say), open the
  // nearest folder that does still exist instead of Explorer's default.
  if (requestedPath.includes('"')) {
    // Can't come from a real Windows path, but it's interpolated into a
    // verbatim command line below, so refuse rather than trust that.
    sendJson(res, 400, { error: "path contains a double quote" });
    return;
  }
  const target = path.normalize(requestedPath);
  let arg;
  let opened;
  if (fs.existsSync(target)) {
    arg = `/select,"${target}"`;
    opened = target;
  } else {
    let dir = path.dirname(target);
    while (!fs.existsSync(dir) && path.dirname(dir) !== dir) dir = path.dirname(dir);
    if (!fs.existsSync(dir)) {
      sendJson(res, 404, { error: "Neither the file nor any of its folders exist -- is the drive connected?" });
      return;
    }
    arg = `"${dir}"`;
    opened = dir;
  }

  // explorer.exe routinely exits non-zero even when it worked -- a known
  // quirk -- so only a genuine spawn error (explorer.exe missing) counts.
  execFile("explorer.exe", [arg], { windowsVerbatimArguments: true }, (err) => {
    if (err && err.code === "ENOENT") {
      sendJson(res, 500, { error: `Could not launch Explorer: ${err.message}` });
      return;
    }
    sendJson(res, 200, { ok: true, fileSelected: opened === target, opened });
  });
}

// Serves one call-sheet PDF inline (the browser's own PDF viewer shows
// it). Only a name that's literally in the call-sheets folder listing is
// served, so this can't reach anything else on disk.
function handleGetCallSheet(req, res, reqUrl) {
  const name = reqUrl.searchParams.get("file") || "";
  const { files, error } = loadCallSheets();
  if (error || !files.includes(name)) {
    res.writeHead(404);
    res.end("call sheet not found");
    return;
  }
  const filePath = path.join(CALL_SHEETS_DIR, name);
  fs.stat(filePath, (err, stat) => {
    if (err) {
      res.writeHead(404);
      res.end("call sheet not found");
      return;
    }
    res.writeHead(200, {
      "Content-Type": "application/pdf",
      "Content-Length": stat.size,
      "Content-Disposition": `inline; filename*=UTF-8''${encodeURIComponent(name)}`,
    });
    fs.createReadStream(filePath).pipe(res);
  });
}

function handleGetVersion(req, res) {
  sendJson(res, 200, { version: EDITOR_VERSION, history: VERSION_HISTORY });
}

// ---------------------------------------------------------------------
// Router
// ---------------------------------------------------------------------

const server = http.createServer((req, res) => {
  const reqUrl = new URL(req.url, `http://${req.headers.host}`);

  if (reqUrl.pathname === "/api/db" && req.method === "GET") {
    handleGetDb(req, res);
    return;
  }
  if (reqUrl.pathname === "/api/presence" && req.method === "POST") {
    handlePostPresence(req, res);
    return;
  }
  if (reqUrl.pathname === "/api/asset" && req.method === "POST") {
    handlePostAsset(req, res);
    return;
  }
  if (reqUrl.pathname === "/api/video" && req.method === "GET") {
    handleGetVideo(req, res, reqUrl);
    return;
  }
  if (reqUrl.pathname === "/api/reveal" && req.method === "POST") {
    handleRevealPath(req, res);
    return;
  }
  if (reqUrl.pathname === "/api/callsheet" && req.method === "GET") {
    handleGetCallSheet(req, res, reqUrl);
    return;
  }
  if (reqUrl.pathname === "/api/version" && req.method === "GET") {
    handleGetVersion(req, res);
    return;
  }
  if (req.method === "GET") {
    serveStatic(req, res, reqUrl.pathname);
    return;
  }
  res.writeHead(404);
  res.end("not found");
});

server.listen(PORT, "127.0.0.1", () => {
  console.log(`Media Intelligence Editor  (v${EDITOR_VERSION})`);
  console.log(`  DB file: ${DB_PATH}`);
  if (!fs.existsSync(DB_PATH)) {
    console.log(`  WARNING: that file does not exist yet. Pass --db <path> to point at your orange-crush-media-intel-db.json.`);
  }
  const sheets = loadCallSheets();
  console.log(`  Call sheets: ${CALL_SHEETS_DIR}`);
  if (sheets.error) console.log(`  WARNING: ${sheets.error}`);
  else console.log(`    ${sheets.files.length} PDFs, ${Object.keys(sheets.overrides).length} override date(s)`);
  const structure = loadStructure();
  console.log(`  Story structure: ${STRUCTURE_PATH}`);
  if (structure.error) console.log(`  WARNING: ${structure.error} -- act/sequence will be left as-is.`);
  else console.log(`    ${structure.rows.length} sequences, scenes ${structure.rows[0].start}-${structure.rows[structure.rows.length - 1].finish}`);
  console.log(`  Open:    http://localhost:${PORT}`);
});
