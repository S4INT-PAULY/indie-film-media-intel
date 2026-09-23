#!/usr/bin/env python3
"""
orange_crush_scene_reconciler.py

Fills in the `scene` field (and, along with it, `location`/`characters`/
`action` -- see below) for assets that don't have one yet, by finding a
short run of consecutive words from each asset's `transcript` inside the
dialogue of the Final Draft script (Orange-Crush-Production.fdx), the
same way a human would recognize a clip by ear against the page.

WHY THIS EXISTS
---------------
`orange_crush_schedule_enricher.py` already attaches each asset's
candidate scene list for its shoot day (`media_intel.production_day.scenes`)
-- but on a day with 15-30 scenes scheduled, that still leaves a human
scrubbing through clips to figure out *which* one. Where a clip's
transcript happens to contain a short stretch of the actual scripted
dialogue, this script can usually narrow that down automatically, or at
least rule most of the candidates out.

WHAT'S IN SCOPE (mirrors the request this was built from)
-----------------------------------------------------------
  RQ1  Only assets with a blank `scene` are touched (never overwrites a
       scene a human -- or an earlier pass -- already entered). The DB
       represents "no scene yet" as `""`, not JSON `null`; both (and
       whitespace-only strings) are treated as blank.
  RQ2  `transcript` is the dialogue-performance text to search with.
  RQ3  An in-scope asset with a blank `transcript` is skipped outright --
       there's nothing to match against.
  RQ4  Performance vs. page drift and transcription errors are expected;
       this is a best-effort match, not a guarantee. See "MATCHING
       ALGORITHM" below for how that's handled.
  RQ5  The match unit is a "pattern" of PATTERN_SIZE consecutive words
       (`--pattern-size`, default 5).
  RQ6  Scenes are identified by `<Paragraph Type="Scene Heading" Number="...">`
       in the .fdx; dialogue is `<Paragraph Type="Dialogue">`. (Only
       *top-level* Content/Paragraph elements count -- SceneProperties/
       Summary and ScriptNote also contain nested <Paragraph> elements
       describing/annotating a scene, which are NOT script dialogue and
       must not be walked into. Also: Final Draft splits one line of
       dialogue across multiple sibling <Text> runs -- for style/revision
       reasons -- so a paragraph's full text is the *concatenation* of
       all its <Text> children, not just the first one.)
  RQ7  All matching is case-insensitive (and punctuation-insensitive --
       see normalization below).
  RQ8  On a match, the matched scene's metadata is applied to the asset
       *exactly* the way clicking that scene's card in the editor UI
       does (`applySceneToForm()` in app.js):
         scene      <- the matched scene's scene_number
         location   <- the matched scene's location (always overwritten,
                        even to blank, same as the UI)
         characters <- the matched scene's character list, split on ",",
                        with any "(<digits>)" speaking-count suffix
                        stripped, as a JSON ARRAY of names (only touched
                        if the scene has a characters value at all)
         action     <- the matched scene's description, ONLY if `action`
                        is currently blank (same conditional the UI uses)
       Note on `characters`'s type: the UI (app.js's collectFormFields())
       always saves this as an array. The earlier one-off
       scene_take_autofill.py saved it as a single joined string instead
       -- that's a real, pre-existing inconsistency in the DB right now
       (spot-checked: 186 assets have `characters` as a string, 110 as an
       array). This script follows app.js, not that script, so everything
       it writes is an array.
       `take` is intentionally NOT touched -- the UI's scene-card click
       doesn't set it either, and take-from-slate is a different, already
       -built tool (scene_take_autofill.py).

MATCHING ALGORITHM
-------------------
1. Parse the .fdx once. Walk top-level Content/Paragraph elements in
   document order, and cut the script into per-scene spans at each Scene
   Heading. Within each scene's span, take only the Dialogue paragraphs
   (each one kept as its own token run -- see "why not one big blob"
   below), normalize each to lowercase word tokens, and slide a
   PATTERN_SIZE window across it to populate an inverted index:
   {normalized 5-gram string -> set of scene numbers it appears in}.

   Why not concatenate all of a scene's dialogue into one stream before
   windowing: a 5-gram that straddles the boundary between two different
   characters' lines was never actually spoken as a unit -- it's an
   artifact of two adjacent paragraphs in the file, not something an
   actor said. Windowing per-paragraph avoids ever indexing a phrase that
   wasn't contiguous in the performance as written.

2. For each in-scope asset, normalize its transcript the same way and
   slide the same window across it. Every window position that hits the
   index votes for one or more scene numbers. For each scene number with
   at least one hit, the score is (a) the longest run of *consecutive*
   window positions that hit that scene (converted to an actual matched-
   word count: run-length + pattern_size - 1) and (b) the total number of
   hit positions, as a tiebreaker. Longest contiguous run wins because
   it's a much stronger signal than the same total number of hits
   scattered non-contiguously (which is more likely to be short common
   phrases -- "I don't know", "come on" -- recurring by coincidence).

3. A raw text match is not enough by itself to write anything: the
   matched scene number must also appear in *that asset's own*
   `media_intel.production_day.scenes[]` (its candidate list for its own
   shoot day) -- the same gate `scene_take_autofill.py` uses, and the
   same reason: it's what the editor UI would actually let you click.
   A scene that matches the dialogue but wasn't even scheduled that day
   is far more likely a coincidental short phrase than a real ID, and
   applying it would mean fabricating location/character metadata the UI
   was never able to hand you in the first place (there's no candidate
   list entry to read it from).

   - Exactly one scheduled scene has the (uniquely) best score -> applied.
   - Two or more scheduled scenes are tied at the best score -> left
     alone, reported as "ambiguous" with all tied candidates.
   - The best-scoring scene(s) matched the dialogue but aren't in this
     asset's own schedule -> left alone, reported as "matched, not
     scheduled" (this also covers the ~50 assets with no
     `production_day` at all -- nothing can ever be "eligible" for them).
   - No scene's dialogue produced any hit at all -> "no match".

4. Optional, report-only: for anything not cleanly applied, if rapidfuzz
   is installed, a fuzzy top-N ranking (whole-transcript vs. each
   scheduled candidate scene's dialogue, via token_set_ratio) is added to
   the report as a human-facing suggestion -- never auto-applied. This is
   the "dialogue fallback path" already sketched in the transcription
   research doc for this project, just wired up as a byproduct here
   rather than built as its own tool. Skipped quietly if rapidfuzz isn't
   installed (`pip install rapidfuzz`).

SAFETY / WHY THIS DEFAULTS TO NOT WRITING
-------------------------------------------
Every other script in 01-tools/ derives its output deterministically
(a schedule row for a date, a transcript for a path) and writes by
default, with an opt-in `--dry-run` to preview. This script's output is a
best-effort *guess*, however well-gated -- so it inverts that default:
nothing is written unless `--apply` is passed. `--dry-run` is also
accepted as a synonym for "don't apply" (the default) so the flag reads
naturally either way.

When `--apply` is used: a timestamped snapshot of the DB is written to
`_archive/` first (same convention as the pre-rebuild backup already
there); the write is the same temp-file-then-rename used by every other
tool here; and -- because this project has already hit a case where a
"successful" DB write didn't durably stick (see the ops-risk notes in the
media-intel-editor features doc) -- the file is immediately re-read off
disk after writing and hash-compared against what was meant to be
written, with a loud failure (nonzero exit) if they don't match, rather
than trusting a "wrote OK" message alone.

Usage:
    python3 orange_crush_scene_reconciler.py \\
        --db "..\\20-video\\orange-crush-media-intel-db.json" \\
        --fdx "..\\10-script\\Orange-Crush-Production.fdx" \\
        [--pattern-size 5] [--apply] [--report "..\\20-video\\scene-reconcile-report.json"] [--verbose]

Stdlib only for the core matching/writing. Fuzzy suggestions (report-only)
use rapidfuzz if it's installed; everything else works without it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger("orange_crush_scene_reconciler")

try:
    from lxml import etree as ET
    _XML_BACKEND = "lxml"
except ImportError:  # pragma: no cover - lxml not installed
    import xml.etree.ElementTree as ET
    _XML_BACKEND = "stdlib"


# ---------------------------------------------------------------------
# Text normalization / n-gram indexing
# ---------------------------------------------------------------------

_APOSTROPHES = str.maketrans({"’": "'", "‘": "'", "´": "'", "`": "'"})
_STRIP_RE = re.compile(r"[^a-z0-9'\s]")
_WS_RE = re.compile(r"\s+")


def normalize_tokens(text: str) -> list[str]:
    """Lowercase, fold curly quotes to straight apostrophes, drop every
    other punctuation/symbol character (case-insensitive per RQ7), and
    split on whitespace. Keeps contractions ("don't") as single tokens;
    everything else that isn't a letter, digit, or apostrophe becomes a
    space, so e.g. "pussy, Bill?" -> ["pussy", "bill"]."""
    if not text:
        return []
    text = text.lower().translate(_APOSTROPHES)
    text = _STRIP_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text).strip()
    return text.split(" ") if text else []


def ngrams(tokens: list[str], n: int):
    """Yield (start_index, ngram_string) for every window of size n."""
    for i in range(len(tokens) - n + 1):
        yield i, " ".join(tokens[i:i + n])


def longest_run(positions: set[int]) -> int:
    """Longest run of consecutive integers in `positions`."""
    if not positions:
        return 0
    best = cur = 1
    prev = None
    for p in sorted(positions):
        if prev is not None and p == prev + 1:
            cur += 1
        else:
            cur = 1
        best = max(best, cur)
        prev = p
    return best


# ---------------------------------------------------------------------
# .fdx parsing
# ---------------------------------------------------------------------

def paragraph_text(p) -> str:
    """Concatenate every direct <Text> child's text content. A single
    line of dialogue is routinely split across several sibling <Text>
    runs in Final Draft's XML (different revision/style runs) -- taking
    only the first one silently truncates most lines."""
    parts = []
    for t in p.findall("Text"):
        if t.text:
            parts.append(t.text)
    return "".join(parts)


class SceneIndex:
    def __init__(self, pattern_size: int):
        self.pattern_size = pattern_size
        self.index: dict[str, set[str]] = {}          # ngram -> {scene_number}
        self.scene_dialogue: dict[str, list[str]] = {}  # scene_number -> raw dialogue lines (for reports)
        self.scene_order: list[str] = []               # scene numbers in script order
        self.dialogue_paragraph_count = 0
        self.dialogue_word_count = 0

    def add_paragraph(self, scene_number: str, raw_text: str):
        text = raw_text.strip()
        if not text:
            return
        self.dialogue_paragraph_count += 1
        self.scene_dialogue.setdefault(scene_number, []).append(text)
        tokens = normalize_tokens(text)
        self.dialogue_word_count += len(tokens)
        if len(tokens) < self.pattern_size:
            return
        for _, gram in ngrams(tokens, self.pattern_size):
            self.index.setdefault(gram, set()).add(scene_number)


def build_scene_index(fdx_path: Path, pattern_size: int) -> SceneIndex:
    tree = ET.parse(str(fdx_path))
    root = tree.getroot()
    content = root.find("Content")
    if content is None:
        raise ValueError("No top-level <Content> element found in the .fdx -- is this a Final Draft file?")

    idx = SceneIndex(pattern_size)
    current_scene: Optional[str] = None
    for p in content.findall("Paragraph"):
        ptype = p.get("Type")
        if ptype == "Scene Heading":
            current_scene = p.get("Number")
            if current_scene is not None:
                idx.scene_order.append(current_scene)
            continue
        if ptype == "Dialogue" and current_scene is not None:
            idx.add_paragraph(current_scene, paragraph_text(p))
    return idx


# ---------------------------------------------------------------------
# Scene metadata helpers (mirrors app.js's applySceneToForm exactly)
# ---------------------------------------------------------------------

def norm_scene_num(s) -> Optional[str]:
    if s is None:
        return None
    s = str(s).strip()
    return s.upper() or None


def is_blank(v) -> bool:
    return v is None or (isinstance(v, str) and v.strip() == "")


def format_characters(raw: str) -> list[str]:
    """"UMA(4),AMITA(2)" -> ["UMA", "AMITA"] -- same split/strip/filter
    app.js does across applySceneToForm() + collectFormFields(), just
    landing on the array collectFormFields() actually saves instead of
    the joined display string in between."""
    names = [re.sub(r"\(\d+\)", "", c).strip() for c in raw.split(",")]
    return [n for n in names if n]


def candidate_scenes(asset: dict) -> list[dict]:
    pd = (asset.get("media_intel") or {}).get("production_day") or {}
    scenes = pd.get("scenes")
    return scenes if isinstance(scenes, list) else []


def apply_scene_to_asset(asset: dict, scene: dict, note_line: str) -> None:
    asset["scene"] = scene.get("scene_number") or ""
    asset["location"] = scene.get("location") or ""
    if scene.get("characters"):
        asset["characters"] = format_characters(scene["characters"])
    if is_blank(asset.get("action")) and scene.get("description"):
        asset["action"] = scene["description"]
    existing_notes = (asset.get("notes") or "").strip()
    asset["notes"] = note_line if not existing_notes else f"{note_line}\n{existing_notes}"


# ---------------------------------------------------------------------
# Per-asset matching
# ---------------------------------------------------------------------

class MatchResult:
    __slots__ = ("outcome", "applied_scene", "ranked", "eligible_ranked", "tokens")

    def __init__(self):
        self.outcome = None            # "applied" | "ambiguous" | "unscheduled" | "no-match" | "skip-blank-transcript"
        self.applied_scene = None      # scene dict from production_day.scenes, if applied
        self.ranked = []               # [{scene_number, matched_words, hits}], best first, across the WHOLE script
        self.eligible_ranked = []      # same, filtered to this asset's own candidate scenes
        self.tokens = 0


def score_transcript(idx: SceneIndex, transcript: str) -> list[dict]:
    tokens = normalize_tokens(transcript)
    if len(tokens) < idx.pattern_size:
        return []
    hits: dict[str, set[int]] = {}
    for i, gram in ngrams(tokens, idx.pattern_size):
        scene_numbers = idx.index.get(gram)
        if not scene_numbers:
            continue
        for sn in scene_numbers:
            hits.setdefault(sn, set()).add(i)
    ranked = []
    for sn, positions in hits.items():
        run = longest_run(positions)
        ranked.append({
            "scene_number": sn,
            "matched_words": run + idx.pattern_size - 1,
            "hits": len(positions),
        })
    ranked.sort(key=lambda r: (-r["matched_words"], -r["hits"], r["scene_number"]))
    return ranked


def match_asset(idx: SceneIndex, asset: dict) -> MatchResult:
    res = MatchResult()
    transcript = asset.get("transcript") or ""
    if is_blank(transcript):
        res.outcome = "skip-blank-transcript"
        return res

    res.ranked = score_transcript(idx, transcript)
    if not res.ranked:
        res.outcome = "no-match"
        return res

    cands = candidate_scenes(asset)
    eligible_by_num = {norm_scene_num(c.get("scene_number")): c for c in cands if c.get("scene_number")}
    res.eligible_ranked = [r for r in res.ranked if norm_scene_num(r["scene_number"]) in eligible_by_num]

    if not res.eligible_ranked:
        res.outcome = "unscheduled"
        return res

    top = res.eligible_ranked[0]
    tied = [r for r in res.eligible_ranked
            if r["matched_words"] == top["matched_words"] and r["hits"] == top["hits"]]
    if len(tied) > 1:
        res.outcome = "ambiguous"
        return res

    res.outcome = "applied"
    res.applied_scene = eligible_by_num[norm_scene_num(top["scene_number"])]
    return res


# ---------------------------------------------------------------------
# Fuzzy fallback (report-only, optional)
# ---------------------------------------------------------------------

def try_import_rapidfuzz():
    try:
        from rapidfuzz import fuzz
        return fuzz
    except ImportError:
        return None


def fuzzy_suggestions(fuzz, idx: SceneIndex, asset: dict, top_n: int) -> list[dict]:
    transcript = asset.get("transcript") or ""
    cands = candidate_scenes(asset)
    scene_numbers = [c.get("scene_number") for c in cands if c.get("scene_number")]
    if not scene_numbers:
        # No schedule for this asset at all -- fall back to every scene
        # in the script that has any indexed dialogue.
        scene_numbers = list(idx.scene_dialogue.keys())
    scored = []
    for sn in scene_numbers:
        norm_sn = norm_scene_num(sn)
        lines = None
        for k in idx.scene_dialogue:
            if norm_scene_num(k) == norm_sn:
                lines = idx.scene_dialogue[k]
                break
        if not lines:
            continue
        scene_text = " ".join(lines)
        score = fuzz.token_set_ratio(transcript, scene_text)
        scored.append({"scene_number": sn, "fuzzy_score": round(score, 1)})
    scored.sort(key=lambda r: -r["fuzzy_score"])
    return scored[:top_n]


# ---------------------------------------------------------------------
# DB I/O (same conventions as the rest of 01-tools/)
# ---------------------------------------------------------------------

def load_db(db_path: Path) -> dict:
    with open(db_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    data.setdefault("assets", [])
    return data


def serialize_db(db: dict) -> bytes:
    return (json.dumps(db, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def write_db_verified(db_path: Path, db: dict) -> None:
    payload = serialize_db(db)
    expected_hash = hashlib.sha256(payload).hexdigest()

    backup_dir = db_path.parent / "_archive"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    backup_path = backup_dir / f"{db_path.stem}.backup-pre-scene-reconcile-{stamp}{db_path.suffix}"
    if db_path.exists() and not backup_path.exists():
        backup_path.write_bytes(db_path.read_bytes())
        log.info("Backed up current DB to %s", backup_path)

    tmp_path = db_path.with_suffix(db_path.suffix + ".tmp")
    with open(tmp_path, "wb") as f:
        f.write(payload)
    tmp_path.replace(db_path)

    # Hard-learned lesson from this project (see the media-intel-editor
    # features doc's ops-risk notes): a "successful" write has, at least
    # once, not durably stuck. Don't just trust it -- re-read and verify.
    actual_hash = hashlib.sha256(db_path.read_bytes()).hexdigest()
    if actual_hash != expected_hash:
        log.error(
            "DB write did NOT verify: what's on disk right now doesn't hash-match what "
            "this script just wrote (expected %s, found %s). Something is intercepting "
            "or reverting writes to this file -- do not trust the DB until this is "
            "understood. A pre-write backup is at %s.",
            expected_hash[:12], actual_hash[:12], backup_path,
        )
        raise SystemExit(3)
    log.info("Write verified: on-disk hash matches (%s).", expected_hash[:12])


# ---------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------

def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Match blank-scene assets' transcripts against the Final Draft "
        "script's dialogue to auto-fill scene/location/characters/action.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    here = Path(__file__).resolve().parent
    p.add_argument("--db", default=str(here / ".." / "20-video" / "orange-crush-media-intel-db.json"),
                    help="Path to orange-crush-media-intel-db.json (updated in place with --apply).")
    p.add_argument("--fdx", default=str(here / ".." / "10-script" / "Orange-Crush-Production.fdx"),
                    help="Path to the Final Draft script (.fdx).")
    p.add_argument("--pattern-size", type=int, default=5,
                    help="Consecutive-word window size for matching (RQ5). Default 5.")
    p.add_argument("--apply", action="store_true",
                    help="Actually write the DB. Without this (or with --dry-run), only a report is produced.")
    p.add_argument("--dry-run", action="store_true", help="Synonym for the default (don't write).")
    p.add_argument("--report", default=None,
                    help="Path to write a detailed JSON report (every in-scope asset's outcome). "
                    "Defaults to scene-reconcile-report.json next to the DB.")
    p.add_argument("--no-fuzzy-suggestions", action="store_true",
                    help="Skip the optional rapidfuzz-based suggestions for unresolved assets.")
    p.add_argument("--fuzzy-top-n", type=int, default=3,
                    help="How many fuzzy candidates to record per unresolved asset (default 3).")
    p.add_argument("--limit", type=int, default=None,
                    help="Only process the first N in-scope assets (for a quick test run).")
    p.add_argument("--verbose", "-v", action="store_true")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s: %(message)s")

    db_path = Path(args.db)
    fdx_path = Path(args.fdx)
    if not db_path.exists():
        log.error("DB file not found: %s", db_path)
        return 1
    if not fdx_path.exists():
        log.error(".fdx file not found: %s", fdx_path)
        return 1

    log.info("Parsing %s (xml backend: %s)...", fdx_path.name, _XML_BACKEND)
    idx = build_scene_index(fdx_path, args.pattern_size)
    log.info("Indexed %d scenes, %d dialogue paragraphs, %d words (pattern-size=%d).",
              len(idx.scene_order), idx.dialogue_paragraph_count, idx.dialogue_word_count, args.pattern_size)

    fuzz = None if args.no_fuzzy_suggestions else try_import_rapidfuzz()
    if not args.no_fuzzy_suggestions and fuzz is None:
        log.info("rapidfuzz not installed -- skipping fuzzy suggestions for unresolved assets "
                 "(pip install rapidfuzz to enable; exact matching above is unaffected).")

    db = load_db(db_path)
    assets = db.get("assets", [])

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    note_line = (f"Scene auto-matched from dialogue transcript against the script "
                 f"(pattern-size={args.pattern_size}) -- unverified. "
                 f"(orange_crush_scene_reconciler.py, {today})")

    stats = {
        "total_assets": len(assets),
        "already_had_scene": 0,
        "out_of_scope_blank_transcript": 0,
        "in_scope": 0,
        "applied": 0,
        "ambiguous": 0,
        "unscheduled": 0,
        "no_match": 0,
    }
    report_rows = []
    processed = 0

    for asset in assets:
        if not is_blank(asset.get("scene")):
            stats["already_had_scene"] += 1
            continue

        transcript = asset.get("transcript")
        if is_blank(transcript):
            stats["out_of_scope_blank_transcript"] += 1
            continue

        stats["in_scope"] += 1
        if args.limit is not None and processed >= args.limit:
            continue
        processed += 1

        res = match_asset(idx, asset)
        row = {
            "asset_id": asset.get("asset_id"),
            "filename": asset.get("filename"),
            "path": asset.get("path"),
            "outcome": res.outcome,
            "candidate_scene_numbers": [c.get("scene_number") for c in candidate_scenes(asset)],
            "ranked_matches": res.ranked[:5],
            "eligible_ranked_matches": res.eligible_ranked[:5],
        }

        if res.outcome == "applied":
            stats["applied"] += 1
            scene = res.applied_scene
            row["applied_scene_number"] = scene.get("scene_number")
            row["applied_location"] = scene.get("location")
            if not args.apply:
                # Preview only -- don't mutate the asset in a dry run.
                pass
            else:
                apply_scene_to_asset(asset, scene, note_line)
        elif res.outcome == "ambiguous":
            stats["ambiguous"] += 1
        elif res.outcome == "unscheduled":
            stats["unscheduled"] += 1
        elif res.outcome == "no-match":
            stats["no_match"] += 1

        if res.outcome != "applied" and fuzz is not None:
            row["fuzzy_suggestions"] = fuzzy_suggestions(fuzz, idx, asset, args.fuzzy_top_n)

        report_rows.append(row)

    print("\n--- Scene reconciliation summary (pattern-size=%d) ---" % args.pattern_size)
    print(f"  Total assets in DB:                          {stats['total_assets']}")
    print(f"  Already had a scene (untouched):              {stats['already_had_scene']}")
    print(f"  In scope but blank transcript (skipped, RQ3): {stats['out_of_scope_blank_transcript']}")
    print(f"  In scope (blank scene, has transcript):       {stats['in_scope']}")
    if args.limit is not None:
        print(f"    ...of those, processed this run (--limit): {processed}")
    print(f"  Matched + scheduled + unambiguous -> applied: {stats['applied']}")
    print(f"  Matched but tied between 2+ scheduled scenes: {stats['ambiguous']}")
    print(f"  Matched a scene not on that asset's schedule: {stats['unscheduled']}")
    print(f"  No dialogue pattern matched at all:           {stats['no_match']}")

    report_path = Path(args.report) if args.report else (db_path.parent / "scene-reconcile-report.json")
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "pattern_size": args.pattern_size,
        "applied_to_db": bool(args.apply),
        "stats": stats,
        "assets": report_rows,
    }
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"\nDetailed per-asset report written to {report_path}")

    if not args.apply:
        print("\n(--apply not set: DB file was not written. Re-run with --apply once you've "
              "reviewed the report -- especially the 'ambiguous' and 'unscheduled' rows, which "
              "are never auto-applied regardless.)")
        return 0

    write_db_verified(db_path, db)
    print(f"\nWrote updated DB to {db_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
