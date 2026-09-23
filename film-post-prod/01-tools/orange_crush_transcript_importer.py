#!/usr/bin/env python3
"""
orange_crush_transcript_importer.py

Folds scene-stealer.py's transcript output (a pipe-delimited .dsv -- see
scene-stealer.py's header for the column list) into the Orange Crush
media intelligence DB (orange-crush-media-intel-db.json), so the DB is
the one place everything ends up: what the scanner found on disk, what
the schedule enricher matched to production days, and now what
scene-stealer heard on the audio track.

You can pass more than one .dsv in a single run (e.g. the video-clip
transcripts and the standalone-audio transcripts together) -- every
matched row updates the same DB.

MATCHING
--------
Each .dsv row is matched to a DB asset primarily by absolute file path
(normalized for case and slash direction, since this project's tooling
runs on Windows). If a row also carries a non-blank asset_id (scene-
stealer writes one when its source was intel_db mode; it's blank for a
folder-mode run, e.g. the older scene-stealer-audio.yaml folder scan), the
importer also indexes by asset_id and uses it as a fallback for any row
that path-matching didn't resolve, e.g. after a file's been moved and
rescanned since the transcript was generated.

WHAT GETS WRITTEN
------------------
1. A machine-owned block, media_intel.transcription, with everything
   scene-stealer captured (full text, slate_text, language, confidence,
   duration, model, timestamp, status/error) -- refreshed in full on every
   import, exactly like the scanner's own media_intel block. This is the
   permanent record of what the transcription pass found, whether or not
   a human has since edited the transcript.
2. The human-facing `transcript` field (the same one the media-intel-editor
   UI already shows/edits, previously hand-typed only). This is filled
   in ONLY when it's currently blank, by default -- a transcript an editor
   already typed or corrected by hand is never overwritten. Pass
   --overwrite-transcript to replace it anyway (e.g. after fixing a bad
   transcription run), which prints exactly how many existing values that
   will clobber before it does anything, in --dry-run first if you want
   to check.

Only status="ok" rows with actual (possibly VAD-empty) transcript text
touch the human-facing field. status="error"/"no_audio" rows still get
their media_intel.transcription block (so a clip's "why is there no
transcript" is visible in the DB itself), but never touch `transcript`.

Nothing else in the asset record is touched -- scene, take, characters,
etc. are completely untouched by this script, same as the scanner and
enricher.

USAGE
-----
    python3 orange_crush_transcript_importer.py \\
        --db "C:\\orange-crush\\film-post-prod\\20-video\\orange-crush-media-intel-db.json" \\
        --transcripts \\
            "C:\\orange-crush\\film-post-prod\\20-video\\scene-stealer-transcripts.dsv" \\
            "C:\\orange-crush\\film-post-prod\\20-video\\scene-stealer-transcripts-audio.dsv"

Re-run any time you have new or re-run transcripts -- it's a full re-import
each time (matched rows' media_intel.transcription is always refreshed),
so there's no separate "resume" mode to think about here; scene-stealer's
own --retry-errors / resume settings already control what's *in* the dsv.
Stdlib only -- no pip install required.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger("orange_crush_transcript_importer")


def normalize_path_key(path: str) -> str:
    """A best-effort case/slash-insensitive join key for matching a dsv
    row's filepath to a DB asset's path -- this project's paths are all
    Windows (backslashes, case-insensitive filesystem), but this is kept
    slash-tolerant so it also behaves if either side ever uses forward
    slashes."""
    return path.strip().replace("/", "\\").lower()


def load_db(db_path: Path) -> dict:
    with open(db_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    data.setdefault("assets", [])
    return data


def write_db(db_path: Path, db: dict) -> None:
    tmp_path = db_path.with_suffix(db_path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(db, f, indent=2, ensure_ascii=False)
        f.write("\n")
    tmp_path.replace(db_path)


def _float_or_none(v: str) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except ValueError:
        return None


def read_transcript_rows(dsv_path: Path) -> list[dict]:
    with open(dsv_path, "r", encoding="utf-8", newline="") as f:
        reader = csv.reader(f, delimiter="|")
        header = next(reader, None)
        if not header:
            log.warning("%s is empty -- nothing to import from it.", dsv_path)
            return []
        rows = []
        skipped = 0
        for line_num, row in enumerate(reader, start=2):
            if not row:
                continue
            if len(row) != len(header):
                # A row with the wrong number of fields would silently
                # misalign every column after the first mismatch if we
                # zipped it anyway (e.g. a stray "|" in a transcript, or a
                # truncated write) -- safer to skip it and say so than to
                # file a transcript under the wrong asset or wrong column.
                skipped += 1
                log.warning(
                    "%s line %d: expected %d fields, got %d -- skipping this row.",
                    dsv_path, line_num, len(header), len(row),
                )
                continue
            rows.append(dict(zip(header, row)))
    log.info("Read %d row(s) from %s.%s", len(rows), dsv_path,
              f" ({skipped} malformed row(s) skipped)" if skipped else "")
    return rows


def build_transcription_block(row: dict, source_dsv: str) -> dict:
    return {
        "text": row.get("transcription", ""),
        "slate_text": row.get("slate_text", ""),
        "language": row.get("language") or None,
        "language_probability": _float_or_none(row.get("language_probability")),
        "avg_logprob": _float_or_none(row.get("avg_logprob")),
        "duration_sec": _float_or_none(row.get("duration_sec")),
        "model": row.get("model") or None,
        # NOTE: scene-stealer's own "device"/"compute_type" columns mean the
        # GPU/CPU + precision Whisper ran on -- unrelated to (and not to be
        # confused with) this DB's top-level asset "device" field, which
        # means the CAMERA or RECORDER that captured the clip. Prefixed
        # "engine_" here to keep that unambiguous wherever this block is
        # read later.
        "engine_device": row.get("device") or None,
        "engine_compute_type": row.get("compute_type") or None,
        "transcribed_at_utc": row.get("transcribed_at_utc") or None,
        "status": row.get("status") or None,
        "error": row.get("error") or "",
        "source_dsv": source_dsv,
        "imported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def import_rows(
    db: dict, rows: list[dict], source_dsv: str, overwrite_transcript: bool
) -> dict:
    stats = {
        "matched_by_path": 0, "matched_by_asset_id": 0, "unmatched": 0,
        "transcript_filled": 0, "transcript_overwritten": 0, "transcript_skipped_has_value": 0,
        "status_ok": 0, "status_error": 0, "status_no_audio": 0, "status_other": 0,
    }
    unmatched_samples: list[str] = []

    assets = db.get("assets", [])
    by_path = {normalize_path_key(a["path"]): a for a in assets if a.get("path")}
    by_asset_id = {a["asset_id"]: a for a in assets if a.get("asset_id")}

    for row in rows:
        filepath = row.get("filepath", "")
        asset_id = row.get("asset_id", "")
        asset = by_path.get(normalize_path_key(filepath)) if filepath else None
        matched_via = "path"
        if asset is None and asset_id:
            asset = by_asset_id.get(asset_id)
            matched_via = "asset_id"

        if asset is None:
            stats["unmatched"] += 1
            if len(unmatched_samples) < 20:
                unmatched_samples.append(filepath or asset_id or "(no filepath or asset_id in row)")
            continue

        stats["matched_by_path" if matched_via == "path" else "matched_by_asset_id"] += 1

        status = row.get("status", "")
        stats["status_ok" if status == "ok" else
              "status_error" if status == "error" else
              "status_no_audio" if status == "no_audio" else
              "status_other"] += 1

        mi = asset.setdefault("media_intel", {})
        mi["transcription"] = build_transcription_block(row, source_dsv)

        if status == "ok":
            text = (row.get("transcription") or "").strip()
            if text:
                existing = (asset.get("transcript") or "").strip()
                if not existing:
                    asset["transcript"] = row["transcription"]
                    stats["transcript_filled"] += 1
                elif overwrite_transcript:
                    asset["transcript"] = row["transcription"]
                    stats["transcript_overwritten"] += 1
                else:
                    stats["transcript_skipped_has_value"] += 1

    return {"stats": stats, "unmatched_samples": unmatched_samples}


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Import scene-stealer.py .dsv transcript output into the Orange Crush media intelligence DB.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--db", required=True, help="Path to orange-crush-media-intel-db.json (updated in place).")
    p.add_argument("--transcripts", nargs="+", required=True,
                    help="One or more scene-stealer .dsv files to import (e.g. the video and audio transcript runs).")
    p.add_argument("--overwrite-transcript", action="store_true",
                    help="Replace an asset's existing `transcript` field with the fresh transcription, even if a "
                         "human already typed/edited something there. Default: only fill blank transcript fields.")
    p.add_argument("--dry-run", action="store_true", help="Report what would happen but don't write the DB.")
    p.add_argument("--verbose", "-v", action="store_true")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s: %(message)s")

    db_path = Path(args.db)
    if not db_path.exists():
        log.error("DB not found: %s", db_path)
        return 1
    db = load_db(db_path)

    totals = {
        "matched_by_path": 0, "matched_by_asset_id": 0, "unmatched": 0,
        "transcript_filled": 0, "transcript_overwritten": 0, "transcript_skipped_has_value": 0,
        "status_ok": 0, "status_error": 0, "status_no_audio": 0, "status_other": 0,
    }
    all_unmatched: list[str] = []

    for dsv_arg in args.transcripts:
        dsv_path = Path(dsv_arg)
        if not dsv_path.exists():
            log.warning("Transcript file not found, skipping: %s", dsv_path)
            continue
        rows = read_transcript_rows(dsv_path)
        result = import_rows(db, rows, dsv_path.name, args.overwrite_transcript)
        for k, v in result["stats"].items():
            totals[k] += v
        all_unmatched.extend(f"{dsv_path.name}: {p}" for p in result["unmatched_samples"])

    print("\n--- Transcript import summary ---")
    print(f"  Matched by path:              {totals['matched_by_path']}")
    print(f"  Matched by asset_id fallback: {totals['matched_by_asset_id']}")
    print(f"  Unmatched (no DB asset found):{totals['unmatched']}")
    if all_unmatched:
        print("  Sample unmatched rows:")
        for s in all_unmatched[:20]:
            print("    -", s)
        if totals["unmatched"] > len(all_unmatched):
            print(f"    ... and {totals['unmatched'] - len(all_unmatched)} more")
    print(f"  Rows by status: ok={totals['status_ok']}  error={totals['status_error']}  "
          f"no_audio={totals['status_no_audio']}  other={totals['status_other']}")
    print(f"  transcript field filled (was blank):     {totals['transcript_filled']}")
    print(f"  transcript field overwritten (had text): {totals['transcript_overwritten']}")
    print(f"  transcript field left alone (had text, --overwrite-transcript not set): "
          f"{totals['transcript_skipped_has_value']}")

    if args.dry_run:
        print("\n(--dry-run set: DB file was not written)")
        return 0

    write_db(db_path, db)
    print(f"\nWrote updated DB to {db_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
