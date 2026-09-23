# Media Intelligence Editor

A tiny local web app for correcting `orange-crush-media-intel-db.json` -- search, filter, browse the candidate scenes a clip could be, and fix up scene/take assignments where the automated scanner + schedule enrichment could only narrow things down, not decide for you. Video clips and standalone `.wav` recordings (e.g. a Zoom H4n Pro) share this one DB -- each asset's `kind` field ("video" or "audio") is what tells them apart, not separate files or a separate app.

```
VS Code
   │
   ├── Git
   │
   ├── media-intel-db.json          <- stays a normal file. This app just reads/writes it.
   │
   └── Media Intelligence Editor    <- this folder
          │
          ├── Search
          ├── Filters
          ├── Asset editor
          ├── Scene browser
          └── Transcript viewer
```

This tool writes the JSON file in only two ways. Explicit "Save changes" clicks patch exactly one clip's editorial fields (scene, take, location, characters, notes, transcript, etc.) and rewrite the file. Loads and background checks update two derived values, `act`/`sequence` (from scene) and `media_intel.missing_on_disk` (from the disk), but only when they've actually changed. See "Act / sequence" and "Not on disk" below. Nothing about how you use Git changes -- commit, diff, and review this file exactly as you would any other JSON in the repo. `media_intel` and `schedule_catalog` (written by the scanner/enricher scripts) are read-only from this app's point of view and always round-trip untouched.

## Requirements

Node.js (v18+; this was built and tested on v22). No `npm install` needed -- the server has zero dependencies, only Node's built-in `http`/`fs`/`path` modules. The frontend is plain HTML/CSS/JS with no build step.

## Setup

Put this folder wherever you like in the repo -- next to the scanner and enricher scripts is a reasonable spot (e.g. `film-post-prod/20-video/media-intel-editor/`). By default it looks for the DB at `../orange-crush-media-intel-db.json` relative to `server.js`, i.e. one level up. Override with `--db` if you keep it somewhere else.

## Run it

```
node server.js
```

or, from VS Code, open a terminal in this folder and run `npm start`. Then open the printed URL (`http://localhost:5173` by default) in a browser, or in VS Code's own Simple Browser (Command Palette -> "Simple Browser: Show").

Options:

```
node server.js --db "path\to\orange-crush-media-intel-db.json" --structure "path\to\orange-crush-story-structure.csv" --port 5173
```

`--structure` defaults to `../orange-crush-story-structure.csv`, next to the DB.

The server binds to `127.0.0.1` only -- it's not reachable from other machines on your network, only from this one.

## What it does

**Search** -- a single box that matches across filename, device, act, sequence, scene, location, notes, transcript, dialogue, action, and characters. Filters client-side as you type; there are about 1,100 clips, so this is instant.

**Filters** -- act, sequence (narrowed to the chosen act), device, shoot date, and a "scene status" filter that's the one built specifically for the correction workflow:
- *Multiple candidate scenes* -- clips where the shoot date matched several scheduled scenes and a human needs to pick the right one. This is your worklist.
- *Exactly one candidate scene* -- probably right, but not auto-applied; worth a quick glance.
- *No schedule match* -- the date didn't line up with anything in the production strips (a pickup day not yet logged, or a clip with no derivable date at all).
- *Scene not yet assigned* / *Scene assigned* -- independent of schedule matching, tracks whether you've actually filled in the `scene` field yet.

The device filter lists every device the scanner has classified across both video and audio -- camera models (5D Mark II, Sony, DJI...) and "ZOOM H4n Pro" (the default for every `.wav`) side by side in the same dropdown, since they're all just values of the same `device` field now.

There's also a low-confidence-device-ID filter (surfaces the `DJIPocketRecovery`-style clips, and any recovered/bkup-folder `.wav`, that the scanner couldn't classify with certainty) and a used-in-edit filter. **Hide CR Floor clips** is checked by default -- clips flagged CR Floor (see below) drop out of the main list so they don't clutter the working set; uncheck it to see them again. An orange **AUDIO** badge marks standalone recordings in the list and detail header, so they're never mistaken for a camera clip while you're scanning through.

**Sort** -- by shoot date, filename, most candidate scenes, or **story order** (scene number, then take; clips with no scene go last).

**Act / sequence** -- every asset has `act` and `sequence` fields, stored just before `scene` in the JSON:

```json
"act": "ACT III",
"sequence": "Sequence S",
"scene": "153",
"take": "1",
```

They're **derived from `scene`, never typed**. The ranges live in `20-video/orange-crush-story-structure.csv` (`Act,Sequence,Start_Scene,Finish_Scene`, inclusive), which is the single source of truth and agrees with the `seq` column in the production strips. Keeping them derived means they can't drift from the scene they describe:

- In the asset editor they're read-only boxes above Scene, and they update as you type a scene number. A scene outside every range shows "scene outside story structure".
- On save, the server re-derives them from the saved scene and ignores any act/sequence the browser sends.
- On every load or Reload, the server re-derives them for all assets and writes back any that changed. That catches scenes written by other tools (the scene reconciler, a hand edit in VS Code) and any change to the CSV itself. To restructure the film, edit the CSV and click Reload.
- Scene numbers such as `9.1` use their leading integer (9).
- If the CSV is missing, has overlapping ranges, or won't parse, act/sequence are left exactly as they are, never blanked, and the server logs a warning.

Scene cards in the scene browser show each scene's act and sequence. The all-scenes search matches them too (for example "sequence r"). The day info lists the sequences shot that day, grouped by act, from the enricher's `media_intel.production_day.sequences`.

**Not on disk** -- `media_intel.missing_on_disk` is kept live instead of waiting for the next scanner run. Every load or Reload, a background check every 20 seconds, and returning focus to the window all compare each asset's path with the disk. The flag is set to `true` when the file is gone and removed when it's back, the same way the scanner does it. Delete a duplicate clip or folder and it drops out of the list (unless "Include missing-on-disk" is ticked) within 20 seconds, with no rescan. The topbar shows "N not on disk".
- If a whole drive isn't connected, its clips are left as they were and the topbar shows "drive offline: E:\". Unplugging the drive doesn't flag every clip as missing.
- The background check only writes if the browser's copy of the DB is current. If something else changed the file, it writes nothing and the topbar says "DB changed on disk — click Reload".
- A load that changed anything says so briefly in the topbar ("Synced: ..."), so a file change after a Reload is expected, not a surprise in git.

**Duplicate asset_ids** -- a few clips are cataloged at more than one path. The list, detail pane and saves are all keyed on the exact file path, so clicking a duplicate row opens and saves that copy. The header shows an "N copies" badge on these.

**Asset editor** -- every editorial field from the schema as a form: scene, take, location, timecode out, shot type, angle, characters, action, dialogue, take status, continuity, quality flags, performance flags, used in edit, CR Floor, and notes. Nothing saves until you click "Save changes" -- there's no autosave, so a half-finished edit never gets written half-done.

- **Shot type**, **angle**, and **take status** are dropdowns, constrained to a fixed list (shot sizes, camera angles, and take dispositions like Circle Take / NG / Hold for Review) so the same concept always gets entered the same way -- useful once you're searching or filtering hundreds of clips. If a clip already has a value that predates the dropdown (or came from somewhere else) and isn't on the standard list, the form keeps it selected and visible rather than silently blanking it out; save without changing it and it round-trips untouched.
- **Quality flags** and **performance flags** are multi-select -- click as many technical issues (focus, lighting, boom in frame, rolling shutter...) or performance issues (line flub, pacing, continuity...) as apply. Same "don't drop unrecognized existing data" behavior as the dropdowns above, shown as an extra chip marked with `*`.
- **CR Floor** is the "cutting room floor" flag -- a nuclear, whole-shot reject, distinct from `used_in_edit`. Checking it asks for confirmation, and CR-floored clips get a clear red badge everywhere in the app (list, detail header) and are hidden from the main list by default (see the filter above). It intentionally overrides the take-status badge wherever both would otherwise show -- a shot flagged CR Floor is CR Floor regardless of what its take status says.

**Scene browser** -- for the clip you're looking at, shows the day's narrative description and call sheet (when the shoot-dates docx was used during enrichment), then every scene scheduled that day as a clickable card. Clicking one fills the scene number, location, and characters into the asset editor (and the description into Action, if Action is still empty) -- it doesn't save anything by itself, so you can review before committing. Below that is a search across *every* scene in the production strips (including ones with no date yet), for when the per-day candidates don't have the right one -- a pickup day, a scene that was moved, or a clip whose date folder was wrong.

**Call sheets** -- the day's call sheet PDFs appear as links in the scene browser. Click one to view it inside the editor, click it again or Close to hide it, or use "Open in new tab". The PDFs are served from `20-video/call-sheets/` (override with `--call-sheets <dir>`). The filenames in the shoot-dates docx are the originals, and the PDFs have been renamed since, so each clip's call sheets are found in this order:
1. an exact match on a filename in the reference;
2. rows for the clip's shoot date in `call-sheets/call-sheet-overrides.csv` (`Shoot_Date,File`);
3. any PDF whose filename contains the shoot date (`YYYY-MM-DD`).

If nothing matches, the scene browser says "No PDF found". For a rescheduled or renamed day, add an override row (for example `2023-01-14,OC-DAY11-2022-12-17-Office-Redux.pdf`) and click Reload.

**Transcript viewer** -- a plain editable text area for the `transcript` field. No transcription pipeline is wired up here; it's just a place to view/type notes against what's said in the clip while you're scene-hunting.

**Video / audio preview** -- an inline player so you can actually look (or listen) while deciding. It streams the file directly off disk (with range-request support, so scrubbing works) from wherever `path` points in the DB -- a `<video>` player for video assets, an `<audio>` player for `.wav` assets, switched automatically based on the asset's `kind`. If a codec/container doesn't play inline in your browser (most likely on older 5D Mark II `.MOV` files, depending on your browser version), it falls back to showing the file path so you can open it in whatever player you use.

**Copy path / Show in Explorer** -- on the clip header. Show in Explorer opens the clip's folder with the file selected. If the file is gone, it opens the nearest folder that still exists and says so.

## Version

The topbar shows a small `v2026.09.21-1`-style badge next to the project name, fetched live from `GET /api/version` when the page loads. `EDITOR_VERSION` and a short `VERSION_HISTORY` live at the top of `server.js` -- bump the version string there with every change to `server.js` or anything in `public/`.

The badge is the fast way to tell whether what's on screen is actually the build you think it is. If it shows an older version than expected, or shows `v?` in orange, or there's no badge at all, the files on disk are stale -- something put an older copy back (see the git note right below), not a browser caching issue. Hover the badge for a short changelog.

**A note on git:** this tool only ever writes files directly to disk -- it never runs `git add`/`commit` on your behalf. So after a change lands here, it sits as an *uncommitted* change in your working tree until you commit it yourself. If you (or VS Code's Source Control panel, or a `git pull`/`checkout`/`stash`) discard uncommitted changes or check out a different ref, any edits that were never committed -- including ones just pushed here -- are gone, and the files silently fall back to whatever was last committed. Commit after a change lands if you want it to survive a git operation; the version badge is what will tell you if that ever happens again.

## Concurrency / safety

- Saving is scoped to exactly one clip's editorial fields -- everything else about that record, and every other record, is left alone.
- Every save re-reads the file fresh and checks its modified time against what the browser last loaded. If the file changed on disk in the meantime (you re-ran the scanner or enricher, or pulled from git, while the editor tab was open), the save is rejected with a clear message instead of silently overwriting -- reload and re-apply.
- Writes go through a temp-file-then-rename, same pattern as the scanner/enricher scripts, so a crash mid-write can't leave the JSON half-written.
- The video-streaming endpoint only ever serves a path that's already listed as an asset's `path` in the current DB -- it can't be used to browse or read arbitrary files.

## A note on the workflow

The whole point of the scene browser is *not* to guess for you. The scanner and schedule-enrichment scripts can narrow "which scene is this" down to a handful of candidates by matching shoot date, but going from "one of these 19 scenes shot that day" to "this specific one" needs someone who's actually looked at the footage. That's what this app is for -- it puts the candidates, the day's context, and the clip itself in front of you at the same time, so the actual decision is fast even though it can't be automated away.
