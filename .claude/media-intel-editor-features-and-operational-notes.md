# media-intel-editor: recent features, a scene/take auto-fill pass, and two open operational risks

Decision record covering a follow-on session's work against `media-intel-editor` (`film-post-prod/20-video/media-intel-editor/`) and a one-time data-fill script. Picks up after the CR1/CR2 and DB-unification work documented in the other two docs in this project.

## New editor features

**Scene browser auto-collapse.** Once a clip has a `scene` value, the candidate-scenes list collapses to a one-line toggle ("Scene 137 assigned — show 2 candidate scenes") instead of showing the full list, since re-visiting an already-assigned scene is rare. A click re-expands it; state resets to collapsed on every new clip selection. Unassigned clips still show the full candidate list as before.

**Copy path / Show in Explorer.** Two buttons next to the filename in the clip header. Copy path uses the clipboard API. Show in Explorer POSTs to a new `/api/reveal` endpoint, which calls `explorer.exe /select,<path>` server-side (Windows only, 501 elsewhere) — guarded by the same "path must already be a known asset in the DB" check as the existing `/api/video` endpoint, so it can't be used to browse or open arbitrary paths.

**Version badge.** The topbar now shows a small `v2026.09.21-2`-style badge, fetched from a new `GET /api/version`, with `EDITOR\_VERSION` + a short `VERSION\_HISTORY` array at the top of `server.js`. This exists because of the incident below — bump the version string (and add a history line) with every future change to `server.js` or `public/\*`.

**Bugfix: "Scene not yet assigned" filter did nothing.** `sceneStatusOf()` in `app.js` only ever returned `"assigned"` or one of `"no-candidates"`/`"single-candidate"`/`"ambiguous"` — never the literal string `"unassigned"` that the filter dropdown compared against, so that filter option always matched zero rows. This was a pre-existing bug, not introduced this session. Fixed by adding `matchesSceneStatusFilter()`, which treats `"unassigned"` as the union of the three not-yet-assigned outcomes; the other filter values are unchanged. Version `2026.09.21-2`.

**Device-label suffix.** The `\[recovered/cache -- verify]` suffix the scanner appends to a low-confidence device reading (recovery/cache/bkup folder token, or a filename that doesn't match the device's strict clip-naming pattern) was too long for the UI and is now `\[!]`, with an italic legend under the Device filter explaining it.

## Bugfix: search box crash (v2026.09.22-1)

Paul reported the sidebar search box "not working." Root cause: `characters` is supposed to always be a JSON array (that's what `collectFormFields()` in `app.js` saves), but the one-off `scene\_take\_autofill.py` from the previous session (see below) actually saved it as a single comma-joined **string** instead — a real bug in that script, caught while building the `.fdx`-dialogue scene-reconciler tool (see the scene-reconciliation doc, which has the full 186-string/110-array breakdown). Two spots in `app.js` did `(asset.characters || \[]).join(...)` assuming the array shape: `matchesSearch()` (the main search box) and `fillEditorForm()` (populating the detail pane's Characters field). A non-empty string is truthy, so `|| \[]` never kicks in, and `"a string".join` doesn't exist — `TypeError: ... .join is not a function`, thrown from inside the `Array.filter()` callback in `getVisibleAssets()`. That aborts the whole filter, which is exactly why the search box stopped filtering the instant a search term's scan reached one of the \~186 affected assets (reproduced end to end against the live DB: searching "billy" hit the crash on the 8th asset in the array before the fix, returned 483 correct matches after). The same bug meant opening one of those specific assets in the detail pane could fail too.

**Fix:** added `charactersArray(value)` — returns the value as-is if it's already an array, splits-and-trims it if it's a legacy string, and returns `\[]` otherwise — used at both call sites. A legacy string self-heals into a proper array the next time that particular asset is saved from the UI (since `collectFormFields()` always writes the array shape), so this cleans itself up over time without a separate data migration. Version `2026.09.22-1`. The underlying string-vs-array inconsistency itself was left as-is in the DB (out of scope for this fix; tracked in the scene-reconciliation doc).

## Bugfix: Show in Explorer landing on the wrong folder (v2026.09.22-2)

Paul reported Show in Explorer opening `C:\\Users\\paulw\\OneDrive\\Documents` instead of revealing the clip (e.g. `E:\\Orange Crush\\Original\_Footage\\Orange Crush Fantasy Scene\\DJI\_0001.MOV`) — Copy path confirmed the frontend had the right path, so the bug was server-side, in how `handleRevealPath()` invoked Explorer.

**Root cause:** `execFile("explorer.exe", \[\\`/select,${requestedPath}`], ...)`put the`/select,`switch and the path in \*one\* argv element.`execFile`still has to hand Windows'`CreateProcess` a single command-line string, and Node quotes any argv element that contains a space — but it quotes the \*whole\* element. Since almost every path in this project has a space in it (`Orange Crush Fantasy Scene`, etc.), the actual command line Explorer received was `explorer.exe "/select,E:\\Orange Crush...\\DJI\_0001.MOV"`— the switch ended up \*inside\* the quotes along with the path, rather than outside them. explorer.exe doesn't parse its command line the normal argv way; it scans the raw string for the literal substring`/select,` and treats everything after it as the target, quotes and all — so it still found the switch, but the "path" it extracted had a stray trailing quote character glued onto it and didn't exist on disk. Explorer doesn't error on that; it just silently falls back to its default folder, which is exactly why this always "worked" (200 OK, no error surfaced) while opening the wrong place.

**Fix:** build the exact literal command line ourselves — `/select,"<path>"`, comma immediately followed by the quoted path, no space, matching Microsoft's documented syntax — and pass `windowsVerbatimArguments: true` so Node doesn't add its own quoting on top and re-break it. Added a defensive 400 if `requestedPath` ever contains a `"` (can't happen from a real Windows path — `"` is an illegal filename character — but cheap insurance against interpolating it unescaped into the command line). Verified the resulting literal command line matches Explorer's documented `/select,"path"` form exactly; not verified against the live Explorer window itself (no shell access to this machine from this session — Paul, please confirm it actually reveals the clip now). Version `2026.09.22-2`.

## Scene/take auto-fill from transcripts (one-time script)

Paul wrote a `jq` script (`getslate.jq`) that reads each asset's `transcript` field, finds the text up to the word "Action" (the slate call), and pulls a scene and take number out of it via number-word parsing. Its own output, `scene-take-extract.json`, only records `filename` — not `path` — which is unsafe to join back against the DB as-is, since \~24 filenames in this project aren't unique across folders (the same duplicate-footage-folder issue as the 66 duplicate `asset\_id`s below).

Instead of joining that file back in, `getslate.jq`'s exact logic was reimplemented in Python (`scene\_take\_autofill.py`, delivered to Paul, not installed into `01-tools/` since it's one-time) operating directly on each asset's own `path` + `transcript`, sidestepping the filename-collision risk entirely. Verified against `getslate.jq`'s real output before running: 502/503 records matched exactly; the one mismatch was a timing artifact (that asset's `transcript` had been refreshed by a transcript-import between the two runs), not a logic difference.

**Gate:** an extracted scene number is only used if it matches a scene actually scheduled for that asset's shoot day (`media\_intel.production\_day.scenes\[].scene\_number`). This is what filters out most of the known false positives (e.g. a misheard "1.53"-style timecode-looking read just doesn't match any real scene number and is skipped).

**What it writes**, per matched, not-already-assigned asset: `scene`, `location`, `characters`, and `action` (only if currently blank) exactly as the UI's scene-card click would set them; `take` verbatim from the extraction (something the UI click does not itself do); and a provenance line prepended to `notes` — `"Scene/take auto-filled from transcript slate read -- unverified. (scene\_take\_autofill.py, <date>)"` — so these are searchable/spot-checkable later rather than indistinguishable from a human-confirmed entry. Assets that already had a scene (whether from earlier manual work or an earlier pass) are never touched.

**Result:** 276 of 1131 assets filled (272 with a take), out of 1131 total; 628 had no slate pattern at all, 83 had a slate but no parseable number, 139 extracted a scene that didn't match anything scheduled. Verified via before/after diff that exactly the intended assets changed, only in the intended fields, before pushing. **Correction (found later, see the bugfix section above): this script's `\_format\_characters()` saved `characters` as a joined string instead of an array like the UI does — a real bug in this script, not caught at the time.**

## Two open operational risks, unresolved

**1. The editor's static files (`server.js`/`public/\*`) reverted to an older version on their own at least once.** After a clean push (files pushed = files on disk after another chat verified size/mtime), the same four files were rewritten back to older, smaller versions hours later, all within the same second — with no `.git` repository anywhere in `C:\\orange-crush` (checked recursively) to explain it as an uncommitted-changes-discarded event, and no other Claude session or scheduled task active at the time. Leading unconfirmed hypothesis: VS Code's Auto Save (`onFocusChange`/`onWindowChange`) flushing stale, already-open editor tabs that held older buffer content back over the newer files the moment focus left VS Code. Not confirmed. The version badge above exists specifically so this is visible at a glance instead of silently invisible. **Recommendation for whoever picks this up next: consider `git init`-ing this project (or at least the `media-intel-editor` folder) so a revert like this shows up as a diff instead of a mystery, and so it's trivially reversible. Offered to Paul, not yet actioned.**

**2. A DB push (the scene/take auto-fill's first write) reported success but a re-read minutes later showed the pre-write content — then, after redoing the whole fill a second time, a re-read showed the *original* (correct) first write after all, not the second write.** Net effect: no data was lost (verified byte-for-byte, twice, including that all 5 of Paul's manually-entered scenes survived both rounds untouched), but the intermediate reads were genuinely inconsistent with each other and with what was actually pushed, for reasons never fully pinned down. **Practical takeaway adopted from this point forward: after any DB write to the device, immediately re-fetch and hash-compare it against the intended content before reporting success — don't trust a "written" response alone.** Worth keeping in mind if something similar happens again.

## Still open

* The 66 duplicate-`asset\_id` cleanup described in the DB-unification doc is still not done (count may have changed since Paul's from-scratch DB rebuild — recheck before acting).
* The git-safety-net idea above (risk #1) — offered, not actioned.
* `video-media-intel-db.json` and other stale/legacy files Paul moved out of `20-video/` himself; nothing in the active pipeline reads them.
* The `characters` string-vs-array inconsistency (186 vs 110 assets) is not migrated, just made non-fatal. Fine as-is (self-heals on save), but a one-time normalization pass would tidy it up if anyone ever consumes `characters` programmatically outside this editor.
* ~~The Show in Explorer fix (v2026.09.22-2) … not confirmed.~~ **Superseded by v2026.09.22-3 below.** The 22-2 build never reached disk: `server.js` was still at 22-1 with the quoting bug.

## v2026.09.22-3 and v2026.09.23-1 (Claude Code sessions with local shell access, 2026-09-22/23)

**Heads-up:** this notes file was replaced on disk once after this section was first written (last write 2026-09-22 18:29), which is likely a stale editor tab saving over it (risk #1). The code files were not affected. Check the version badge.

**Show in Explorer (22-3), verified on the real Explorer window.** The fix is `/select,"<path>"` with `windowsVerbatimArguments: true`, plus a 400 if the path contains `"`. If the file is gone, it opens the nearest existing folder. Confirmed via `Shell.Application.Windows()`: the window opened `...\Orange Crush Fantasy Scene\tcode` with `DJI_0001.mov` selected.

**Live "not on disk" (22-3).** `missing_on_disk` was only ever set by the scanner. Now `syncPresence()` in `server.js` checks every path on each `GET /api/db` (load/Reload) and on `POST /api/presence`, which the browser polls every 20 s and on window focus. It sets or removes the flag like the scanner does.
- An unreachable drive root is reported as "drive offline" and its assets are left alone.
- `/api/presence` writes only if the client's `expectedMtimeMs` is current; otherwise it returns `stale: true`.
- The topbar shows "N not on disk". There were 17 as of 2026-09-23.

**Act / sequence (22-3).** The source of truth is `20-video/orange-crush-story-structure.csv` (Act,Sequence,Start_Scene,Finish_Scene). It agrees with `schedule_catalog.scenes[].seq` for all 157 scenes. The fields are **derived, never edited**:
- The server re-derives them on every save that touches `scene` (ignoring any client value) and for all assets on every load.
- They're stored just before `scene`. The leading integer of the scene is used, so `9.1` counts as 9.
- A bad or missing CSV leaves them untouched.

In the UI: read-only boxes above Scene, Act and Sequence filters, search, a Story-order sort, act · sequence on scene cards, and the day's `production_day.sequences` grouped by act. The scanner's `EDITORIAL_FIELDS` includes blank act/sequence. Backup: `_archive/orange-crush-media-intel-db.backup-pre-act-sequence-2026-09-22.json`.

**Path-keyed selection (22-3).** The list, `findAsset()` and `POST /api/asset` (`{asset_id, path, …}`) now go by `path`, because 24 asset_ids are duplicated across folders. The header shows an "N copies" badge.

**Call-sheet links (23-1).** Call sheets in the scene browser are links that open the PDF in an inline viewer (an iframe, with "Open in new tab" and Close). `GET /api/callsheet?file=` serves only names in the `20-video/call-sheets/` listing. The docx's `call_sheet_reference` uses the pre-rename filenames, so the frontend's `resolveCallSheets()` matches in this order:
1. The exact referenced name.
2. `call-sheets/call-sheet-overrides.csv` (Shoot_Date,File; `#` comments allowed).
3. Any PDF whose filename contains the shoot date.

This resolves 10 of 11 days. **2023-01-14 (Day 13 Office Redux) has no PDF.** The only candidate, `OC-DAY11-2022-12-17-Office-Redux.pdf`, is dated differently, and it wasn't linked because we don't know it's the same sheet. Waiting on Paul to add an override row if it is. The early-draft, pink and email variants named in references aren't in the folder, except `...CommunityCenter-pink.pdf`.

**Git + GitHub (2026-09-23).** Risk #1's recommendation is done. `C:\orange-crush` is a git repo (branch `main`) pushed to the **public** repo https://github.com/S4INT-PAULY/indie-film-media-intel (MIT).
- Commits use the repo-local identity `S4INT-PAULY <332992456+S4INT-PAULY@users.noreply.github.com>`. Don't use Paul's Gmail or the mass.gov work account (ehs-ptrainor) for this project.
- `.gitignore` deliberately keeps these private: the screenplay (`10-script/`), call-sheet PDFs and the shoot-dates docx (cast/crew contact details), `_archive/`, `_scene_stealer_tmp/`, `Claude outputs/` and all media. Don't publish them without asking.
- `core.autocrlf=false` (repo-local).
- Git for Windows was installed via winget. Claude Code's shell sets `GCM_INTERACTIVE=never`, so the first push needed `$env:GCM_INTERACTIVE='always'`. Credentials are cached now.

If a file is mysteriously reverted again, `git status` / `git diff` shows it and `git restore <file>` undoes it.

**Open:** no act/sequence for clips without a scene (would need an override design). `scene-stealer.py` reads the persisted `missing_on_disk` flag.

