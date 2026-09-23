# indie-film-media-intel

Post-production tooling from **Orange Crush**, an indie feature shot on a mix of consumer and prosumer cameras: Canon 5D Mark II, Sony, DJI Pocket 2, a drone, and a Zoom H4n Pro recorder.

It builds a single JSON "media intelligence" database of every camera and audio file. It attaches the production schedule, transcribes the on-set audio, and gives the editor a small local web app for assigning each clip to its act, sequence, scene and take. The goal is to reach the edit in Premiere knowing what every clip is.

Nothing here is tied to one film. The scripts read a footage folder, a production-strips export, and an optional shoot-dates document. Point them at your own and they'll work the same way.

## What's in the repo

```
film-post-prod/
├── 01-tools/                         Python pipeline (see 01-tools/README.md)
│   ├── orange_crush_video_intel_scanner.py   find every video/.wav, identify the device, pull tech metadata (ffprobe/exiftool)
│   ├── orange_crush_schedule_enricher.py     attach the day's scheduled scenes + call-sheet info to each clip by shoot date
│   ├── scene-stealer.py                      transcribe clip audio (faster-whisper, GPU optional)
│   ├── orange_crush_transcript_importer.py   fold transcripts into the DB
│   └── orange_crush_scene_reconciler.py      auto-assign scenes by matching transcripts to script dialogue (report first, --apply to write)
├── 20-video/
│   ├── media-intel-editor/           local web app for reviewing and correcting the DB (Node, zero dependencies)
│   ├── orange-crush-media-intel-db.json      the database (about 1,100 clips)
│   ├── orange-crush-prodstrips.dsv           production strips export (pipe-delimited)
│   ├── orange-crush-story-structure.csv      act / sequence / scene ranges
│   └── call-sheets/call-sheet-overrides.csv  pins a call-sheet PDF to a shoot date
└── 30-audio/
```

**Deliberately not published:** the screenplay, the call-sheet PDFs and shoot-dates docx (they contain cast and crew contact details), the footage itself, and local DB backups. See `.gitignore`. The tools run without them. Where one is an input, such as the `.fdx` for the scene reconciler or the docx for the enricher, use your own film's equivalent.

## Quick start

Requirements:
- **Python 3.10+** for the pipeline. Most scripts use only the standard library. `scene-stealer.py` needs `faster-whisper` and `pyyaml`. The reconciler optionally uses `rapidfuzz`.
- **ffprobe** (from FFmpeg) and, ideally, **exiftool** on your PATH for the scanner.
- **Node.js 18+** for the editor. No `npm install` needed.

Pipeline order and what to re-run when something changes: [`film-post-prod/01-tools/README.md`](film-post-prod/01-tools/README.md).

Editor:

```
node film-post-prod/20-video/media-intel-editor/server.js
```

Then open http://localhost:5173. Features and design notes are in the [editor README](film-post-prod/20-video/media-intel-editor/README.md).

## Adapting it to your film

- **Footage layout:** the scanner walks any folder tree. Device detection and naming patterns (for example `OC_YYYY_MM_DD_NNNN.MOV`, `MVI_NNNN`, `ZOOMNNNN`) are near the top of the scanner script.
- **Schedule:** export your strips as pipe-delimited text. The expected columns are documented in the enricher.
- **Story structure:** edit `orange-crush-story-structure.csv` (`Act,Sequence,Start_Scene,Finish_Scene`). The editor derives each clip's act and sequence from its scene number.
- **Call sheets:** name them `OC-DAY##-YYYY-MM-DD-Name.pdf`, or any name that contains the shoot date, and put them in `20-video/call-sheets/`.

## Working on the DB with git

The DB is plain JSON on purpose, so every editor save and every tool run shows up as a reviewable diff. Commit after a session of edits. If a file is ever changed unexpectedly, `git diff` shows exactly what changed and `git restore <file>` puts it back.

## License

MIT. See [LICENSE](LICENSE). Use it, fork it, adapt it for your own film.
