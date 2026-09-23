#!/usr/bin/env python3
"""
orange_crush_schedule_enricher.py

Enriches the Orange Crush media intelligence DB (orange-crush-media-intel-db.json,
produced/updated by orange_crush_video_intel_scanner.py -- video AND audio
assets share this one DB now, distinguished by each asset's "kind" field)
with production schedule context from:

  1. A pipe-delimited production strips / scene breakdown export
     (e.g. orange-crush-prodstrips.dsv) -- one row per scene, with a
     Production Date column.
  2. Optionally, the "shoot dates" summary docx (a table of
     Shoot Date | Description of Shoot | Call Sheet Attachment Name).

WHAT THIS DOES AND DOESN'T DO
------------------------------
It does NOT try to guess which scene/take a given clip is -- that's a
human (or a transcript-driven) judgment call, and doing it wrong would be
worse than leaving it blank. What it DOES do: every clip already has a
`shoot_date` (derived by the scanner from its folder name) -- this works
identically for a video clip or a .wav recording, since shoot_date is
derived from the file's path, not its media type. This script looks up
that date in the schedule and attaches every scene that was slated to be
shot that day, as a candidate list, plus that day's narrative description
and call sheet filename if available. An editor looking at a clip from
2022-11-12 (or a room-tone .wav recorded that same day) can then see the
26 scenes scheduled for that day instead of scrolling through call sheet
PDFs.

Everything this script discovers goes into one additive block,
`media_intel.production_day`, alongside the `media_intel` block the
scanner already writes. It never touches `scene`, `take`, `characters`,
or any other hand-authored editorial field.

DATA QUALITY NOTE ON THE .dsv
------------------------------
Real-world production spreadsheets exported to pipe-delimited text are
messy. In the copy this was built against:
  - Columns 0-8 (ShootDay, Status, Production Date, Seq, Scene Number,
    Scene Intro, Location, SceneDesc, ScriptPage) are reliably present
    and positioned on every row, including ragged ones.
  - Trailing/mid-row columns (Sub Location, Time, Shoot Location, the
    per-character speaking-part flags, ...) get silently dropped when
    blank on some rows, shifting later columns left. Those are only
    trusted when the row has exactly the expected column count.
  - A couple of rows are missing their newline in the source export and
    two scene records land on one physical line. This script detects
    those (by drastically-too-long column count) and reports them by
    line number instead of guessing where to split them -- fix the
    source file (add the missing line break) and re-run.
  - A Production Date of 01/00/1900 means "not yet scheduled" (an Excel
    empty-date artifact), not a real date -- those rows are counted
    separately and never joined.

USAGE
-----
    python3 orange_crush_schedule_enricher.py \\
        --db "C:\\orange-crush\\film-post-prod\\20-video\\orange-crush-media-intel-db.json" \\
        --schedule "C:\\orange-crush\\film-post-prod\\20-video\\orange-crush-prodstrips.dsv" \\
        --shoot-dates-docx "C:\\orange-crush\\film-post-prod\\20-video\\call-sheets\\00-orange-crush-shoot-dates.docx"

Re-run any time the schedule changes. Stdlib only -- no pip install
required (the docx table is read via zipfile + xml.etree, not
python-docx).
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import sys
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger("orange_crush_schedule_enricher")

DATE_SLASH_RE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")
UNSCHEDULED_DATE = "01/00/1900"

# Columns 0-8 are the reliable ones (see module docstring). Everything
# after this is only used when the row has exactly EXPECTED_COLUMNS.
CORE_COLUMNS = [
    "shoot_day",
    "status",
    "production_date",
    "seq",
    "scene_number",
    "scene_intro",
    "location",
    "scene_desc",
    "script_page",
]
# Best-effort extra columns, by (name, index) -- only trusted on
# full-width rows. Indexes match the header this script was built
# against; if your export's header differs, --dump-header will show you
# the real positions to adjust here.
EXTRA_COLUMNS = [
    ("scene_page_length", 9),
    ("est_master_shot_take_sec", 10),
    ("est_time_full_coverage", 11),
    ("est_min", 12),
    ("character", 13),
    ("num_speaking_characters", 14),
    ("sub_location", 15),
    ("time", 16),
    ("shoot_location", 17),
]

MERGED_ROW_THRESHOLD_MULTIPLIER = 1.5  # a row this many times wider than the header is treated as 2+ rows glued together


# --------------------------------------------------------------------------
# .dsv (pipe-delimited production strips) parsing
# --------------------------------------------------------------------------

def normalize_slash_date(raw: str) -> Optional[str]:
    raw = (raw or "").strip()
    if not raw or raw == UNSCHEDULED_DATE:
        return None
    m = DATE_SLASH_RE.match(raw)
    if not m:
        return None
    mm, dd, yyyy = m.groups()
    try:
        return f"{int(yyyy):04d}-{int(mm):02d}-{int(dd):02d}"
    except ValueError:
        return None


def parse_prodstrips(path: Path) -> tuple[dict[str, list[dict]], dict]:
    """Returns (scenes_by_date, report)."""
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.reader(f, delimiter="|"))

    if not rows:
        return {}, {"error": "empty file"}

    header = rows[0]
    expected_cols = len(header)
    merged_threshold = expected_cols * MERGED_ROW_THRESHOLD_MULTIPLIER

    scenes_by_date: dict[str, list[dict]] = {}
    report = {
        "expected_columns": expected_cols,
        "total_data_rows": len(rows) - 1,
        "merged_rows": [],       # rows glued together in the source file
        "unscheduled_scenes": 0,  # Production Date == 01/00/1900
        "unscheduled_scene_list": [],  # the actual scene dicts, date=None -- kept so the global catalog can still surface them (a "not yet dated" scene is exactly the case where date-based matching can't help a clip)
        "unparseable_date_rows": [],
        "ragged_rows": 0,        # column count != expected, but not merged
        "scenes_indexed": 0,
    }

    for line_no, row in enumerate(rows[1:], start=2):
        if len(row) < len(CORE_COLUMNS):
            report["unparseable_date_rows"].append((line_no, "row too short to read"))
            continue

        if len(row) > merged_threshold:
            report["merged_rows"].append((line_no, len(row)))
            continue  # don't guess how to split it -- report and skip

        if len(row) != expected_cols:
            report["ragged_rows"] += 1

        core = dict(zip(CORE_COLUMNS, row[: len(CORE_COLUMNS)]))
        raw_date = core["production_date"]

        if raw_date.strip() == UNSCHEDULED_DATE:
            report["unscheduled_scenes"] += 1
            unscheduled_scene = dict(core)
            unscheduled_scene["source_line"] = line_no
            unscheduled_scene["row_complete"] = len(row) == expected_cols
            if len(row) == expected_cols:
                for name, idx in EXTRA_COLUMNS:
                    if idx < len(row):
                        unscheduled_scene[name] = row[idx]
            report["unscheduled_scene_list"].append(unscheduled_scene)
            continue

        iso_date = normalize_slash_date(raw_date)
        if iso_date is None:
            report["unparseable_date_rows"].append((line_no, raw_date))
            continue

        scene = dict(core)
        scene["source_line"] = line_no
        scene["row_complete"] = len(row) == expected_cols
        if len(row) == expected_cols:
            for name, idx in EXTRA_COLUMNS:
                if idx < len(row):
                    scene[name] = row[idx]

        scenes_by_date.setdefault(iso_date, []).append(scene)
        report["scenes_indexed"] += 1

    return scenes_by_date, report


# --------------------------------------------------------------------------
# .docx "shoot dates" table parsing (stdlib zipfile + xml.etree; no
# python-docx dependency, matching the scanner's no-pip-install stance)
# --------------------------------------------------------------------------

W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

MONTH_DATE_RE = re.compile(
    r"([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{4})"
)


def _cell_text(tc: ET.Element) -> str:
    parts = []
    for node in tc.iter():
        tag = node.tag
        if tag == f"{W_NS}t":
            parts.append(node.text or "")
        elif tag in (f"{W_NS}br", f"{W_NS}p"):
            if parts and parts[-1] != "\n":
                parts.append("\n")
    return "".join(parts).strip()


def parse_month_date(raw: str) -> Optional[str]:
    m = MONTH_DATE_RE.search(raw)
    if not m:
        return None
    month_str, day_str, year_str = m.groups()
    for fmt in ("%b", "%B"):
        try:
            month = datetime.strptime(month_str[:3].title(), "%b").month
            return f"{int(year_str):04d}-{month:02d}-{int(day_str):02d}"
        except ValueError:
            continue
    return None


def parse_shoot_dates_docx(path: Path) -> dict[str, dict]:
    """Reads the first table in the docx as
    Shoot Date | Description | Call Sheet Attachment Name
    and returns iso_date -> {description, call_sheet_reference, raw_label}.
    Best-effort: if the docx structure doesn't match, returns {} rather
    than raising, since this input is optional/supplementary.
    """
    result: dict[str, dict] = {}
    try:
        with zipfile.ZipFile(path) as z:
            xml_bytes = z.read("word/document.xml")
    except (OSError, KeyError, zipfile.BadZipFile) as exc:
        log.warning("Could not open %s as a docx: %s", path, exc)
        return result

    root = ET.fromstring(xml_bytes)
    tables = root.iter(f"{W_NS}tbl")
    for tbl in tables:
        trs = list(tbl.iter(f"{W_NS}tr"))
        if len(trs) < 2:
            continue
        for tr in trs[1:]:  # skip header row
            tcs = list(tr.findall(f"{W_NS}tc"))
            if len(tcs) < 2:
                continue
            date_label = _cell_text(tcs[0])
            description = _cell_text(tcs[1]) if len(tcs) > 1 else ""
            call_sheet_ref = _cell_text(tcs[2]) if len(tcs) > 2 else ""
            iso_date = parse_month_date(date_label)
            if not iso_date:
                continue
            result[iso_date] = {
                "raw_label": date_label,
                "description": description,
                "call_sheet_reference": call_sheet_ref,
            }
        break  # only the first table is the shoot-dates table
    return result


# --------------------------------------------------------------------------
# Enrichment
# --------------------------------------------------------------------------

def enrich_db(db: dict, scenes_by_date: dict, day_summaries: dict, source_names: dict) -> dict:
    stats = {
        "enriched": 0,
        "no_date_on_clip": 0,
        "date_not_in_schedule": 0,
    }
    dates_with_footage = set()
    dates_matched = set()

    for asset in db.get("assets", []):
        mi = asset.get("media_intel")
        if not isinstance(mi, dict):
            continue
        shoot_date = mi.get("shoot_date")
        if not shoot_date:
            stats["no_date_on_clip"] += 1
            continue

        dates_with_footage.add(shoot_date)
        scenes = scenes_by_date.get(shoot_date)
        day_info = day_summaries.get(shoot_date)

        if not scenes and not day_info:
            stats["date_not_in_schedule"] += 1
            mi.pop("production_day", None)
            continue

        dates_matched.add(shoot_date)
        production_day = {
            "source": source_names,
            "enriched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        if scenes:
            shoot_days = sorted({s["shoot_day"] for s in scenes if s.get("shoot_day")})
            statuses = sorted({s["status"] for s in scenes if s.get("status")})
            sequences = sorted({s["seq"] for s in scenes if s.get("seq")})
            production_day.update(
                {
                    "shoot_day_numbers": shoot_days,
                    "statuses": statuses,
                    "sequences": sequences,
                    "scene_count": len(scenes),
                    "scenes": [
                        {
                            "scene_number": s.get("scene_number"),
                            "int_ext": (s.get("scene_intro") or "").strip(),
                            "location": (s.get("location") or "").strip(),
                            "description": (s.get("scene_desc") or "").strip(),
                            "script_page": s.get("script_page"),
                            "status": s.get("status"),
                            "characters": s.get("character"),
                            "shoot_location": s.get("shoot_location"),
                            "row_complete": s.get("row_complete"),
                        }
                        for s in scenes
                    ],
                }
            )
        if day_info:
            production_day["day_description"] = day_info.get("description")
            production_day["call_sheet_reference"] = day_info.get("call_sheet_reference")

        mi["production_day"] = production_day
        stats["enriched"] += 1

    stats["dates_with_footage_but_no_schedule_match"] = sorted(dates_with_footage - dates_matched)
    stats["schedule_dates_with_no_footage"] = sorted(set(scenes_by_date) - dates_with_footage)
    return stats


def _scene_catalog_entry(s: dict, date: Optional[str]) -> dict:
    return {
        "date": date,
        "shoot_day": s.get("shoot_day"),
        "status": s.get("status"),
        "seq": s.get("seq"),
        "scene_number": s.get("scene_number"),
        "int_ext": (s.get("scene_intro") or "").strip(),
        "location": (s.get("location") or "").strip(),
        "description": (s.get("scene_desc") or "").strip(),
        "script_page": s.get("script_page"),
        "characters": s.get("character"),
        "shoot_location": s.get("shoot_location"),
        "row_complete": s.get("row_complete"),
    }


def build_schedule_catalog(scenes_by_date: dict, unscheduled_scenes: list, source_names: dict) -> dict:
    """A flat, full list of every scene in the production strips -- dated
    and not-yet-dated alike -- stored once at the top level of the DB, not
    per-asset. Lets a consumer (e.g. the media intel editor web app) offer
    a global scene search/browser for clips whose date has no per-clip
    production_day match (wrong/missing date folder, a pickup day not yet
    in the spreadsheet, or -- just as often -- because the scene itself
    has no Production Date yet) rather than only the same-day candidates
    attached to each asset."""
    scenes = []
    for date, day_scenes in sorted(scenes_by_date.items()):
        for s in day_scenes:
            scenes.append(_scene_catalog_entry(s, date))
    for s in unscheduled_scenes:
        scenes.append(_scene_catalog_entry(s, None))
    return {
        "source": source_names,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "scene_count": len(scenes),
        "unscheduled_scene_count": len(unscheduled_scenes),
        "scenes": scenes,
    }


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def write_db(db_path: Path, db: dict) -> None:
    tmp_path = db_path.with_suffix(db_path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(db, f, indent=2, ensure_ascii=False)
        f.write("\n")
    tmp_path.replace(db_path)


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Attach production-schedule context (by shoot date) to the "
        "Orange Crush media intelligence DB (video + audio assets).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--db", help="Path to orange-crush-media-intel-db.json (updated in place). Required unless --dump-header.")
    p.add_argument("--schedule", required=True, help="Path to the pipe-delimited production strips (.dsv).")
    p.add_argument("--shoot-dates-docx", help="Optional path to the shoot-dates summary docx (Shoot Date | Description | Call Sheet Attachment Name table).")
    p.add_argument("--dry-run", action="store_true", help="Report what would happen but don't write the DB.")
    p.add_argument("--dump-header", action="store_true", help="Print the .dsv header with column indexes and exit (use this if EXTRA_COLUMNS needs adjusting for a different export).")
    p.add_argument("--verbose", "-v", action="store_true")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s: %(message)s")

    schedule_path = Path(args.schedule)
    if args.dump_header:
        with open(schedule_path, newline="", encoding="utf-8-sig") as f:
            header = next(csv.reader(f, delimiter="|"))
        for i, c in enumerate(header):
            print(i, c)
        return 0

    if not args.db:
        log.error("--db is required (unless using --dump-header).")
        return 1
    db_path = Path(args.db)
    if not db_path.exists():
        log.error("DB file not found: %s", db_path)
        return 1
    if not schedule_path.exists():
        log.error("Schedule file not found: %s", schedule_path)
        return 1

    scenes_by_date, dsv_report = parse_prodstrips(schedule_path)

    day_summaries = {}
    if args.shoot_dates_docx:
        docx_path = Path(args.shoot_dates_docx)
        if docx_path.exists():
            day_summaries = parse_shoot_dates_docx(docx_path)
        else:
            log.warning("--shoot-dates-docx path not found: %s (continuing without it)", docx_path)

    print("\n--- Schedule parse report (%s) ---" % schedule_path.name)
    print(f"  Data rows read:            {dsv_report['total_data_rows']}")
    print(f"  Scenes indexed by date:    {dsv_report['scenes_indexed']}")
    print(f"  Distinct dates in schedule:{len(scenes_by_date)}")
    print(f"  Not-yet-scheduled scenes (Production Date blank): {dsv_report['unscheduled_scenes']}")
    print(f"  Ragged rows (trailing columns dropped, core fields still trusted): {dsv_report['ragged_rows']}")
    if dsv_report["merged_rows"]:
        print(f"  MERGED ROWS -- two+ scenes glued onto one line, SKIPPED (fix the source file's line break and re-run):")
        for line_no, width in dsv_report["merged_rows"]:
            print(f"    line {line_no}: {width} columns (expected {dsv_report['expected_columns']})")
    if dsv_report["unparseable_date_rows"]:
        print(f"  Rows with an unparseable Production Date: {len(dsv_report['unparseable_date_rows'])}")
        for line_no, raw in dsv_report["unparseable_date_rows"][:10]:
            print(f"    line {line_no}: {raw!r}")
    if day_summaries:
        print(f"  Shoot-day descriptions loaded from docx: {len(day_summaries)}")

    with open(db_path, "r", encoding="utf-8") as f:
        db = json.load(f)

    source_names = {"schedule": schedule_path.name}
    if day_summaries:
        source_names["shoot_dates_docx"] = Path(args.shoot_dates_docx).name

    stats = enrich_db(db, scenes_by_date, day_summaries, source_names)
    db["schedule_catalog"] = build_schedule_catalog(
        scenes_by_date, dsv_report["unscheduled_scene_list"], source_names
    )

    print("\n--- Enrichment summary ---")
    print(f"  Clips enriched with a production day:     {stats['enriched']}")
    print(f"  Clips with no shoot_date at all (skipped): {stats['no_date_on_clip']}")
    print(f"  Clips whose date has no schedule match:    {stats['date_not_in_schedule']}")
    if stats["dates_with_footage_but_no_schedule_match"]:
        print("  Footage dates with NO matching schedule rows (reshoots/pickups not yet in the spreadsheet?):")
        for d in stats["dates_with_footage_but_no_schedule_match"]:
            print(f"    {d}")
    if stats["schedule_dates_with_no_footage"]:
        print("  Schedule dates with NO matching footage on disk yet:")
        for d in stats["schedule_dates_with_no_footage"]:
            print(f"    {d}")

    if args.dry_run:
        print("\n(--dry-run set: DB file was not written)")
        return 0

    write_db(db_path, db)
    print(f"\nWrote enriched DB back to {db_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
