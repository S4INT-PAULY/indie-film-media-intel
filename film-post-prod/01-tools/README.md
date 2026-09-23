# Orange Crush media pipeline — `01-tools`

Four scripts that build and maintain `orange-crush-media-intel-db.json` (in
`20-video/`), plus the `media-intel-editor` app (its own
[README](../20-video/media-intel-editor/README.md)) for reviewing and
correcting what they produce.

Each script owns a different, non-overlapping slice of the DB, and never
touches another script's slice or anything a human typed in the editor.
That's what makes it safe to re-run any one of them on its own -- see
"Routine maintenance" below for exactly when you need to.

| Script | Owns |
|---|---|
| `orange_crush_video_intel_scanner.py` | File discovery, device classification, technical metadata (`media_intel.technical`, `.file`, `.device`, `.device_source`, `kind`, `device`, `asset_id`, `duration`, `timecode_in`) |
| `orange_crush_schedule_enricher.py` | `schedule_catalog` (DB-level) and `media_intel.production_day` (per asset) |
| `orange_crush_transcript_importer.py` | `media_intel.transcription` and the `transcript` field (only fills it if blank) |
| `scene-stealer.py` | Nothing in the DB directly -- reads it (in `intel_db` mode) and writes a `.dsv` transcript file for the importer above to consume |
| `orange_crush_scene_reconciler.py` | `scene`, `location`, `characters`, `action`, `notes` -- but **only** on an asset whose `scene` is still blank, and only when its `transcript` can be matched against a scheduled candidate scene's dialogue in the `.fdx` (see the script's own docstring for the matching/gating logic). Writes exactly the fields the editor UI's scene-card click would set; never touches an asset that already has a scene. Report-only (nothing written) unless you pass `--apply`. |
| `media-intel-editor` (derived data) | `act` / `sequence` (re-derived from `scene` via `20-video/orange-crush-story-structure.csv` on every editor load and save; never hand-edited) and `media_intel.missing_on_disk` (re-checked against the disk on every load and every 20 s while the editor is open; the scanner also still sets it) |
| `media-intel-editor` | Everything else: `scene`, `take`, `characters`, `location`, `shot_type`, `angle`, `action`, `dialogue`, `take_status`, `quality_flags`, `performance_indicators`, `continuity`, `notes`, `used_in_edit`, `cr_floor` -- and the human-facing `transcript` text once it's been filled |

## First-time run

Run in this order. Paths below assume you're running from `01-tools/`;
adjust if not.

| # | Command | What it does |
|---|---|---|
| 1 | `python orange_crush_video_intel_scanner.py --root "E:\Orange Crush\Original_Footage" --db "..\20-video\orange-crush-media-intel-db.json"` | Walks the footage root, discovers every video + `.wav` file, classifies the device, pulls technical metadata, and creates one asset record per file with every editorial field blank. |
| 2 | `python orange_crush_schedule_enricher.py --db "..\20-video\orange-crush-media-intel-db.json" --schedule "..\20-video\orange-crush-prodstrips.dsv" --shoot-dates-docx "..\20-video\call-sheets\00-orange-crush-shoot-dates.docx"` | Reads the production strips (+ shoot-dates summary), builds `schedule_catalog`, and attaches each asset's candidate scenes for its `shoot_date`. |
| 3 | `python scene-stealer.py --config scene-stealer.yaml` | Transcribes every clip's audio (faster-whisper), writes `scene-stealer-transcripts.dsv`. Needs a GPU-capable machine; this is the slow step. |
| 4 | `python orange_crush_transcript_importer.py --db "..\20-video\orange-crush-media-intel-db.json" --transcripts "..\20-video\scene-stealer-transcripts.dsv"` | Folds the transcripts into the DB (`media_intel.transcription` + fills blank `transcript` fields). |
| 5 | `node "..\20-video\media-intel-editor\server.js"` | Open the editor and start assigning scenes/takes, reviewing transcripts, flagging quality issues, etc. |

Steps 1-4 are all safe to re-run from scratch on an existing DB -- they
merge by file path and never clobber editorial fields or each other's
data. Step 5 just serves whatever's currently in the DB file.

## Routine maintenance -- what to re-run for what

You almost never need to redo the whole sequence above. Once the DB
exists, only re-run the step(s) that own whatever actually changed:

| What changed | Re-run | Why that's enough |
|---|---|---|
| New footage or `.wav` files added to the drive | Step 1 (scanner) only | It discovers the new files and adds them as new assets; every existing asset's editorial fields, transcripts, and `production_day` are left exactly as they were. |
| **`orange-crush-prodstrips.dsv` or the shoot-dates docx changed** (new/moved scenes, corrected dates, a new call sheet) | **Step 2 (enricher) only** | It works purely off each asset's already-recorded `shoot_date` against the fresh schedule data -- it doesn't touch footage or scan any drive. Nothing else in the DB is read or written. |
| New or re-run transcription output from scene-stealer | Step 4 (importer) only | It just re-reads the `.dsv` and updates `media_intel.transcription`/`transcript`; steps 1-3 don't need to happen again unless the footage or schedule also changed. |
| Only some clips failed transcription last time | `python scene-stealer.py --retry-errors`, then step 4 | Re-attempts only the rows currently marked `status=error` in the existing `.dsv` instead of redoing all 900+ clips. |
| Editing scene/take/notes/quality flags/transcripts by hand | Nothing -- just use the editor | Saves go straight to the DB from the editor's Save button; none of the other four tools will ever overwrite them. |
| Want another automatic pass at still-unassigned scenes (e.g. after new transcripts came in, or after correcting the schedule) | `python orange_crush_scene_reconciler.py`, review the printed report (especially `ambiguous` / `unscheduled` rows -- those are never auto-applied), then re-run with `--apply` | It only ever looks at assets whose `scene` is still blank; anything already assigned (by hand or by an earlier run of this script) is left alone. Safe to re-run as often as you like. |

### A note on scene-stealer's `output.mode`

`scene-stealer.yaml`'s `output.mode` controls whether a re-run
starts over or picks up where it left off:
- `"overwrite"` (the default) -- redoes every clip in the DB from scratch. Fine for a first full run, wasteful for "I just added 20 new clips."
- `"resume"` -- skips any clip already present in the existing `.dsv` output and only transcribes what's new. Switch to this for the "new footage added" row above once you've done the first full pass, so adding a handful of new clips doesn't mean re-transcribing everything.
