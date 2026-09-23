#!/usr/bin/env python3
"""
orange_crush_video_intel_scanner.py

Walks a root folder (and every subfolder) of ORANGE CRUSH camera-original
footage AND standalone audio recordings, discovers every video/audio file,
pulls whatever technical + device metadata it can find (container metadata
via ffprobe/exiftool, plus the Sony "NRT" XML sidecar that rides alongside
XDCAM-style clips, e.g. C0001.MP4 + C0001M01.XML), classifies which
device shot/recorded the clip (5D Mark II / Sony / DJI Pocket / DJI Drone
for video; ZOOM H4n Pro default for .wav), and upserts one JSON record
per clip into the project's unified media intelligence DB
(20-video/orange-crush-media-intel-db.json). Video and audio assets share
one DB and one schema -- each record's "kind" field ("video" or "audio")
is what tells them apart, not separate files.

DESIGN GOALS
------------
1. Never clobber human/editorial work. Fields an editor fills in by hand
   (scene, take, characters, location, shot_type, angle, action, dialogue,
   take_status, quality_flags, performance_indicators, continuity, notes,
   transcript, used_in_edit, cr_floor) are preserved across re-scans. Only
   the machine-derived fields are refreshed.
2. Everything the scanner itself discovers lives in one additive block,
   `media_intel`, so it never collides with the hand-authored schema and
   is safe to ignore by any existing tooling that only expects the
   original flat fields.
3. Incremental by default: a clip whose size+mtime haven't changed since
   the last scan is not re-probed (fast re-scans as new shoot days land).
4. Degrades gracefully. ffprobe and exiftool are both optional -- if
   neither is installed, the scanner still walks the tree, matches
   sidecars, and classifies devices from filenames/folders alone.
5. Stdlib only. No pip install required to run this script itself.

DEVICES THIS PROJECT USES (see classify_device() to tune / extend)
--------------------------------------------------------------------
 - Canon 5D Mark II      -> .MOV, named MVI_####.MOV or OC_YYYY_MM_DD_####.MOV,
                             usually paired with a .THM thumbnail sidecar.
 - Sony (XDCAM-style)    -> .MP4 named C####.MP4, paired with a
                             C####M01.XML "NonRealTimeMeta" sidecar. Often
                             multi-cam, shot into Camera2/Camera3 subfolders.
 - DJI Pocket 2 handheld -> .MP4 named DJI_####.MP4, paired with a .LRF
                             low-res proxy sidecar. Also recovered/cache
                             files with messy names live in a
                             "...Recovery" folder -- these get classified
                             as DJI but flagged low-confidence.
 - DJI Drone             -> same DJI_####.MP4 pattern, distinguished by a
                             "DRONE" folder in the path.
 - ZOOM H4n Pro           -> .WAV, any filename -- every .wav file defaults
                             to this device (see AUDIO_DEVICE_LABEL); there's
                             no per-file make/model detection for audio.
                             A file under a recovery/cache/bkup-named folder
                             still gets catalogued, just flagged low-
                             confidence, same as recovered video footage.

USAGE
-----
    python3 orange_crush_video_intel_scanner.py \\
        --root "E:\\Orange Crush\\Original_Footage" \\
        --db   "E:\\Orange Crush\\film-post-prod\\20-video\\orange-crush-media-intel-db.json"

A single --root above both the camera folders and the Audio subfolder
(e.g. Original_Footage, with 5dmkii/, Sony/, DJIFIX/, DRONE/, and Audio/
all underneath it) picks up video and audio in the same pass -- one DB,
one scan, no separate audio invocation needed.

Re-run any time -- new clips are added, existing clips are refreshed,
nothing an editor typed by hand is touched. See --help for every option,
including --exclude, --hash, --workers, --force-rescan, and
--inspect-xml (a debugging aid to see exactly what a Sony sidecar parses
to, so field-mapping can be tuned against your real files).
"""

from __future__ import annotations

import argparse
import concurrent.futures
import fnmatch
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

# Extensions treated as a "video asset" worth its own DB entry.
DEFAULT_VIDEO_EXTENSIONS = {".mp4", ".mov", ".mxf", ".m4v", ".avi"}

# Standalone audio recorder files (e.g. a Zoom H4n Pro used for production
# sound / ADR reference) -- catalogued the same way as video, just with a
# much simpler device classification (see classify_device()) since there's
# no camera make/model detection to do.
DEFAULT_AUDIO_EXTENSIONS = {".wav"}

DEFAULT_EXTENSIONS = DEFAULT_VIDEO_EXTENSIONS | DEFAULT_AUDIO_EXTENSIONS

# The device every .wav file is assumed to have come from, unless/until
# per-file device detection is worth adding (e.g. parsing BWF/iXML
# metadata some recorders embed). Matches this project's actual kit.
AUDIO_DEVICE_LABEL = "ZOOM H4n Pro"
AUDIO_DEVICE_MAKE = "Zoom"
AUDIO_DEVICE_MODEL = "H4n Pro"
AUDIO_DEVICE_CODE = "ZOOMH4N"

# Sidecar extensions we look for next to a video file (does not create its
# own asset entry, just gets attached to the matching video's record).
SIDECAR_EXTENSIONS = {".xml", ".thm", ".lrf"}

# Friendly display names for makes/models we can identify via exiftool/
# ffprobe container tags. Extend as new gear shows up on set.
CAMERA_MODEL_DISPLAY_NAMES = {
    ("canon", "canon eos 5d mark ii"): "5D MK II",
    ("canon", "eos 5d mark ii"): "5D MK II",
    ("sony", "ilce-7rm3"): "Sony ILCE-7RM3",
    ("sony", "ilce-7rm3a"): "Sony ILCE-7RM3",
    ("dji", "pocket 2"): "DJI Pocket 2",
    ("dji", "osmo pocket 2"): "DJI Pocket 2",
}

# Folders that are almost certainly not camera-original media (recovered
# cache dumps, duplicate re-orgs). They're still scanned and included --
# just flagged with lower confidence -- unless the user excludes them
# explicitly with --exclude.
LOW_CONFIDENCE_PATH_TOKENS = {"recovery", "cache", "bkup", "backup"}

DATE_FOLDER_RE = re.compile(r"(?<!\d)(20\d{2})(\d{2})(\d{2})(?!\d)")
SONY_CLIP_RE = re.compile(r"^C(\d{3,5})$", re.IGNORECASE)
DJI_CLIP_RE = re.compile(r"^DJI_?(\d{3,5})$", re.IGNORECASE)
CANON_MVI_RE = re.compile(r"^MVI_?(\d{3,5})$", re.IGNORECASE)
CANON_OC_RE = re.compile(r"^OC_(\d{4})_(\d{2})_(\d{2})_(\d{3,5})$", re.IGNORECASE)
CAMERA_UNIT_RE = re.compile(r"^Camera(\d+)$", re.IGNORECASE)
# Zoom H-series default filename pattern, e.g. ZOOM0001.WAV, ZOOM0012_LR.WAV,
# ZM0003.wav. If your files use a different convention, this just falls
# back to using the full filename stem in the asset id (see build_asset_id)
# -- nothing breaks, the ids are just less compact.
ZOOM_CLIP_RE = re.compile(r"^Z(?:OOM)?0*(\d{3,5})(?:[_-].*)?$", re.IGNORECASE)

# The hand-authored fields in the project's existing schema. New asset
# records get these blank; existing records keep whatever a human put here.
# Keys inside media_intel that THIS scanner owns and fully refreshes on
# every (re)probe. Any other key found in an existing record's media_intel
# (e.g. 'production_day', added by orange_crush_schedule_enricher.py) is
# left alone on reprobe rather than clobbered -- this scanner only ever
# overwrites its own keys, so other enrichment tools can layer their own
# additive sub-keys onto media_intel without getting wiped out the next
# time a clip is (re)probed.
SCANNER_OWNED_MEDIA_INTEL_KEYS = {
    "scan_date",
    "shoot_date",
    "device_source",
    "detection_method",
    "detection_confidence",
    "camera_unit",
    "file",
    "technical",
    "device",
    "sidecars",
    "xml_sidecar_parsed",
    "tools_used",
    "missing_on_disk",
}

EDITORIAL_FIELDS = {
    "act": "",  # derived from scene via 20-video/orange-crush-story-structure.csv; the editor fills it on load/save
    "sequence": "",  # ditto
    "scene": "",
    "take": "",
    "timecode_out": "",
    "characters": [],
    "location": "",
    "shot_type": "",  # constrained dropdown in the editor UI; see media-intel-editor/public/app.js FIELD_OPTIONS
    "angle": "",  # constrained dropdown; ditto
    "action": "",
    "dialogue": "",
    "take_status": "",  # constrained dropdown (circle take / good take / NG / etc.); supersedes the old free-text "select" field
    "quality_flags": [],  # multi-select technical issue tags; supersedes the old free-text "quality" field
    "performance_indicators": [],  # multi-select acting/performance issue tags; supersedes the old free-text "performance" field
    "continuity": "",
    "notes": "",
    "transcript": "",
    "used_in_edit": False,
    "cr_floor": False,  # "cutting room floor" -- marks the whole shot as rejected/unusable
}

log = logging.getLogger("orange_crush_scanner")


# --------------------------------------------------------------------------
# Small utilities
# --------------------------------------------------------------------------

def human_size(num_bytes: Optional[int]) -> str:
    if not num_bytes:
        return ""
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.2f} {unit}" if unit != "B" else f"{int(size)} {unit}"
        size /= 1024
    return f"{size:.2f} TB"


def seconds_to_timecode(seconds: Optional[float]) -> str:
    """HH:MM:SS.mmm -- matches the plain-string 'duration' field already
    used in the project's example DB (not drop-frame SMPTE timecode)."""
    if seconds is None:
        return ""
    try:
        seconds = float(seconds)
    except (TypeError, ValueError):
        return ""
    if seconds < 0:
        return ""
    hh = int(seconds // 3600)
    mm = int((seconds % 3600) // 60)
    ss = seconds % 60
    return f"{hh:02d}:{mm:02d}:{ss:06.3f}"


def sha256_of_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()


def which(tool: str) -> Optional[str]:
    return shutil.which(tool)


def run_json_tool(cmd: list[str]) -> Optional[Any]:
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=60, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.debug("tool call failed: %s (%s)", cmd, exc)
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None


# --------------------------------------------------------------------------
# ffprobe / exiftool probing
# --------------------------------------------------------------------------

def probe_ffprobe(path: Path) -> Optional[dict]:
    """Runs ffprobe and returns a compact dict of technical facts, or None
    if ffprobe isn't installed / the file can't be read."""
    if not which("ffprobe"):
        return None
    data = run_json_tool(
        [
            "ffprobe",
            "-v", "quiet",
            "-print_format", "json",
            "-show_format",
            "-show_streams",
            str(path),
        ]
    )
    if not data:
        return None

    fmt = data.get("format", {}) or {}
    streams = data.get("streams", []) or []
    vstream = next((s for s in streams if s.get("codec_type") == "video"), {})
    astream = next((s for s in streams if s.get("codec_type") == "audio"), {})

    fps = None
    rate = vstream.get("avg_frame_rate") or vstream.get("r_frame_rate")
    if rate and rate != "0/0":
        try:
            n, d = rate.split("/")
            n, d = float(n), float(d)
            fps = round(n / d, 3) if d else None
        except (ValueError, ZeroDivisionError):
            fps = None

    duration = None
    for candidate in (fmt.get("duration"), vstream.get("duration"), astream.get("duration")):
        if candidate:
            try:
                duration = float(candidate)
                break
            except ValueError:
                continue

    width = vstream.get("width")
    height = vstream.get("height")
    resolution_label = None
    if height:
        if height >= 2000:
            resolution_label = "4K" if (width or 0) >= 3800 else f"{height}p (UHD-ish)"
        else:
            resolution_label = f"{height}p"

    tags = fmt.get("tags", {}) or {}
    creation_time = tags.get("creation_time") or vstream.get("tags", {}).get("creation_time")
    timecode = vstream.get("tags", {}).get("timecode") or fmt.get("tags", {}).get("timecode")

    return {
        "container_format": fmt.get("format_long_name") or fmt.get("format_name"),
        "duration_seconds": duration,
        "bit_rate": int(fmt["bit_rate"]) if fmt.get("bit_rate") else None,
        "size_bytes_ffprobe": int(fmt["size"]) if fmt.get("size") else None,
        "video_codec": vstream.get("codec_name"),
        "video_codec_long": vstream.get("codec_long_name"),
        "width": width,
        "height": height,
        "resolution_label": resolution_label,
        "frame_rate": fps,
        "pix_fmt": vstream.get("pix_fmt"),
        "color_space": vstream.get("color_space"),
        "audio_codec": astream.get("codec_name"),
        "audio_channels": astream.get("channels"),
        "audio_sample_rate": astream.get("sample_rate"),
        "creation_time": creation_time,
        "timecode": timecode,
    }


def probe_exiftool(path: Path) -> Optional[dict]:
    """Runs exiftool -j and returns the first (only) result dict, or None."""
    if not which("exiftool"):
        return None
    data = run_json_tool(["exiftool", "-json", "-G0", "-a", str(path)])
    if not data or not isinstance(data, list):
        return None
    raw = data[0]
    # exiftool prefixes group names like "EXIF:Make" or "QuickTime:Make"
    # when -G0 is used; normalize to bare keys, last-one-wins.
    flat = {}
    for k, v in raw.items():
        bare = k.split(":", 1)[1] if ":" in k else k
        flat[bare] = v
    return flat


# --------------------------------------------------------------------------
# Sony "NRT" (NonRealTimeMeta) XML sidecar parsing
# --------------------------------------------------------------------------

def _strip_ns(tag: str) -> str:
    return tag.split("}", 1)[1] if "}" in tag else tag


def parse_sony_nrt_xml(xml_path: Path) -> Optional[dict]:
    """Parses a Sony-style NonRealTimeMeta XML sidecar (the
    <stem>M01.XML file next to a Sony clip). This schema varies a bit by
    camera/firmware, so this parser is deliberately defensive: it pulls
    out the fields we know how to name, and also keeps a flat 'raw' dict
    of every attribute it saw so nothing is silently lost. Run this
    script with --inspect-xml <path> against one real sidecar from your
    card to sanity-check the mapping and extend it if a field you need
    lands only in 'raw'.
    """
    try:
        tree = ET.parse(xml_path)
    except (ET.ParseError, OSError) as exc:
        log.warning("Could not parse XML sidecar %s: %s", xml_path, exc)
        return None

    root = tree.getroot()
    result: dict[str, Any] = {
        "creation_date": None,
        "duration_frames": None,
        "capture_fps": None,
        "video_codec": None,
        "width": None,
        "height": None,
        "audio_channels": None,
        "audio_sample_rate": None,
        "device_manufacturer": None,
        "device_model": None,
        "device_serial_number": None,
        "timecode_start": None,
        "raw": {},
    }

    for elem in root.iter():
        tag = _strip_ns(elem.tag)
        attrib = elem.attrib

        if not attrib:
            continue
        # keep everything, namespaced by tag, for the raw fallback bucket
        result["raw"].setdefault(tag, []).append(dict(attrib))

        if tag == "CreationDate":
            result["creation_date"] = attrib.get("value")
        elif tag == "Duration":
            result["duration_frames"] = attrib.get("value")
        elif tag == "VideoFrame":
            result["capture_fps"] = attrib.get("captureFps") or attrib.get("formatFps")
            if attrib.get("videoCodec"):
                result["video_codec"] = attrib.get("videoCodec")
        elif tag == "VideoLayout":
            try:
                result["width"] = int(attrib.get("pixel")) if attrib.get("pixel") else None
                result["height"] = int(attrib.get("numOfVerticalLine")) if attrib.get(
                    "numOfVerticalLine"
                ) else None
            except ValueError:
                pass
        elif tag == "AudioFormat":
            result["audio_channels"] = attrib.get("numOfChannel")
            result["audio_sample_rate"] = attrib.get("samplingRate")
        elif tag == "Device":
            result["device_manufacturer"] = attrib.get("manufacturer")
            result["device_model"] = attrib.get("modelName")
            result["device_serial_number"] = attrib.get("serialNo")
        elif tag in ("LtcChangeTable", "LtcChangeTableItem") and result["timecode_start"] is None:
            tc = attrib.get("value") or attrib.get("tcString")
            if tc:
                result["timecode_start"] = tc

    return result


# --------------------------------------------------------------------------
# File discovery + sidecar matching
# --------------------------------------------------------------------------

@dataclass
class DiscoveredVideo:
    video_path: Path
    sidecars: dict[str, Path] = field(default_factory=dict)  # ext -> path, e.g. ".xml": Path(...)


def find_sidecars(video_path: Path, all_files_in_dir: dict[str, Path]) -> dict[str, Path]:
    """Matches sidecar files that share the clip's stem, tolerant of the
    Sony '<stem>M01.XML' naming (stem + 'M01' + ext) as well as a plain
    '<stem>.ext' sidecar (used by .THM/.LRF)."""
    stem = video_path.stem
    sidecars = {}
    for ext in SIDECAR_EXTENSIONS:
        candidates = [f"{stem}{ext}", f"{stem}M01{ext}"]
        for cand in candidates:
            key = cand.lower()
            if key in all_files_in_dir:
                sidecars[ext] = all_files_in_dir[key]
                break
    return sidecars


def discover_videos(
    root: Path, extensions: set[str], exclude_globs: list[str]
) -> list[DiscoveredVideo]:
    discovered: list[DiscoveredVideo] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dir_path = Path(dirpath)

        if exclude_globs and any(
            fnmatch.fnmatch(str(dir_path), pat) for pat in exclude_globs
        ):
            dirnames[:] = []  # don't descend further
            continue

        # lowercase filename -> Path, for quick sidecar lookups in this dir
        files_lower = {name.lower(): dir_path / name for name in filenames}

        for name in filenames:
            p = dir_path / name
            if p.suffix.lower() not in extensions:
                continue
            if exclude_globs and any(fnmatch.fnmatch(str(p), pat) for pat in exclude_globs):
                continue
            sidecars = find_sidecars(p, files_lower)
            discovered.append(DiscoveredVideo(video_path=p, sidecars=sidecars))
    return discovered


# --------------------------------------------------------------------------
# Device classification (camera for video, fixed default for audio)
# --------------------------------------------------------------------------

def _display_name_for(make: Optional[str], model: Optional[str]) -> Optional[str]:
    if not make and not model:
        return None
    key = ((make or "").strip().lower(), (model or "").strip().lower())
    if key in CAMERA_MODEL_DISPLAY_NAMES:
        return CAMERA_MODEL_DISPLAY_NAMES[key]
    parts = [p for p in (make, model) if p]
    return " ".join(parts) if parts else None


def _camera_code_from_make_model(
    make: Optional[str], model: Optional[str], parts_lower: list[str]
) -> Optional[str]:
    """Short, stable, alnum-only code used to build asset_id -- derived
    from the actual device make/model rather than the (longer, more
    human-friendly) display label, so id generation doesn't depend on
    label text/punctuation."""
    make_l = (make or "").lower()
    model_l = (model or "").lower()
    if "sony" in make_l or "ilce" in model_l:
        return "SONY"
    if "canon" in make_l or "5d" in model_l:
        return "5DMKII"
    if "dji" in make_l:
        return "DJIDRONE" if any("drone" in seg for seg in parts_lower) else "DJIP2"
    return None


def _classify_video_device(
    video_path: Path,
    sidecars: dict[str, Path],
    xml_data: Optional[dict],
    ffprobe_data: Optional[dict],
    exif_data: Optional[dict],
    low_confidence: bool,
) -> dict:
    """Returns {'device_label', 'device_code', 'detection_method',
    'unit', 'make', 'model', 'serial_number'} (confidence is added by the
    classify_device() dispatcher, which is what computes low_confidence).

    Priority: real device metadata (exiftool, then XML sidecar Device
    element) beats filename/folder heuristics, since metadata is what the
    camera itself reported. Filename/folder patterns are the fallback for
    footage whose container doesn't carry clean make/model tags (common
    on older Canon 5D .MOV files, and on DJI recovered cache dumps).
    """
    parts_lower = [p.lower() for p in video_path.parts]
    stem = video_path.stem

    unit = None
    for seg in video_path.parts:
        m = CAMERA_UNIT_RE.match(seg)
        if m:
            unit = f"Camera{m.group(1)}"
            break

    make = model = serial = None
    detection_method = "undetected"
    label = None
    device_code = None

    if exif_data:
        make = exif_data.get("Make")
        model = exif_data.get("Model")
        serial = exif_data.get("SerialNumber") or exif_data.get("InternalSerialNumber")
        if make or model:
            label = _display_name_for(make, model)
            device_code = _camera_code_from_make_model(make, model, parts_lower)
            detection_method = "exiftool_make_model"

    if not label and xml_data and (xml_data.get("device_manufacturer") or xml_data.get("device_model")):
        make = make or xml_data.get("device_manufacturer")
        model = model or xml_data.get("device_model")
        serial = serial or xml_data.get("device_serial_number")
        label = _display_name_for(make, model)
        device_code = _camera_code_from_make_model(make, model, parts_lower)
        detection_method = "xml_sidecar_device"

    if not label:
        # --- filename / folder heuristics, tuned to this project's cards ---
        has_xml = ".xml" in sidecars
        has_thm = ".thm" in sidecars
        has_lrf = ".lrf" in sidecars

        if has_xml or "sony" in parts_lower:
            label, make, model, device_code = "Sony ILCE-7RM3", "Sony", "ILCE-7RM3", "SONY"
            detection_method = "filename_heuristic:sony_xml_or_folder"
        elif DJI_CLIP_RE.match(stem) or "dji" in " ".join(parts_lower) or has_lrf:
            if any("drone" in seg for seg in parts_lower):
                label, device_code = "DJI Drone", "DJIDRONE"
            else:
                label, device_code = "DJI Pocket 2 (Handheld Gimbal)", "DJIP2"
            if low_confidence or not DJI_CLIP_RE.match(stem):
                # Short tag by design -- this gets appended to a device
                # label that's already displayed inline in the editor UI
                # (filter dropdown, clip header), and "[recovered/cache --
                # verify]" was pushing those past a readable width. "[!]" is
                # the tag; the editor shows the full meaning as a legend
                # underneath the device filter, not repeated on every clip.
                label += " [!]"
            make, model = "DJI", "Pocket 2"
            detection_method = "filename_heuristic:dji_pattern_or_folder"
        elif (
            video_path.suffix.lower() == ".mov"
            and (CANON_MVI_RE.match(stem) or CANON_OC_RE.match(stem) or has_thm)
        ) or any(tok in seg for seg in parts_lower for tok in ("5dmkii", "5dmk2", "5d_mk_ii", "canon5d")):
            label, device_code = "5D MK II", "5DMKII"
            make, model = "Canon", "EOS 5D Mark II"
            detection_method = "filename_heuristic:5dmkii_pattern"
        else:
            label = "Unknown"
            detection_method = "undetected"

    return {
        "device_label": label,
        "device_code": device_code or "UNK",
        "detection_method": detection_method,
        "unit": unit,
        "make": make,
        "model": model,
        "serial_number": serial,
    }


def _classify_audio_device(audio_path: Path, low_confidence: bool) -> dict:
    """Standalone audio recorder files (.wav) all default to this
    project's actual field recorder -- there's no per-file make/model to
    detect the way exiftool/XML sidecars give us for cameras. If a second
    recorder ever gets used on this project, extend this (e.g. by reading
    BWF/iXML originator metadata via exiftool, which many recorders embed)
    rather than assuming every .wav is the same device."""
    label = AUDIO_DEVICE_LABEL
    if low_confidence:
        # Matches the DJI-recovered-footage precedent above: still
        # catalogued, just flagged for a human to double check -- e.g. a
        # bkup-folder .wav might not actually be a H4n Pro recording. Short
        # "[!]" tag -- see the comment on the DJI branch above for why.
        label += " [!]"
    return {
        "device_label": label,
        "device_code": AUDIO_DEVICE_CODE,
        "detection_method": "extension_default:wav_zoom_h4n",
        "unit": None,
        "make": AUDIO_DEVICE_MAKE,
        "model": AUDIO_DEVICE_MODEL,
        "serial_number": None,
    }


def classify_device(
    path: Path,
    sidecars: dict[str, Path],
    xml_data: Optional[dict],
    ffprobe_data: Optional[dict],
    exif_data: Optional[dict],
) -> dict:
    """Dispatches to the video (camera) or audio (fixed default) device
    classifier based on file extension, and adds the shared 'confidence'
    field. Folders matching LOW_CONFIDENCE_PATH_TOKENS (recovery/cache/
    bkup/backup) are flagged low-confidence either way -- for video this
    already meant "verify which camera shot this"; for audio it means
    "verify this is really a ZOOM H4n Pro file and not a stray recording
    swept up from a backup folder". Either way the file is still
    catalogued, just flagged, matching how this scanner already treats
    recovered/cache video footage -- nothing gets silently dropped from
    the catalog just for living in a bkup-named folder."""
    parts_lower = [p.lower() for p in path.parts]
    low_confidence = any(token in seg for seg in parts_lower for token in LOW_CONFIDENCE_PATH_TOKENS)

    if path.suffix.lower() in DEFAULT_AUDIO_EXTENSIONS:
        info = _classify_audio_device(path, low_confidence)
    else:
        info = _classify_video_device(path, sidecars, xml_data, ffprobe_data, exif_data, low_confidence)

    info["confidence"] = "low" if low_confidence else "normal"
    return info


def parse_shoot_date_from_path(video_path: Path) -> Optional[str]:
    for seg in video_path.parts:
        m = DATE_FOLDER_RE.search(seg)
        if m:
            return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = CANON_OC_RE.match(video_path.stem)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return None


def build_asset_id(video_path: Path, device_info: dict, shoot_date: Optional[str]) -> str:
    code = device_info.get("device_code") or "UNK"
    date_part = shoot_date.replace("-", "") if shoot_date else "UNKDATE"
    unit_part = f"-{device_info['unit'].upper()}" if device_info.get("unit") else ""

    stem = video_path.stem
    num_match = (
        SONY_CLIP_RE.match(stem) or DJI_CLIP_RE.match(stem)
        or CANON_MVI_RE.match(stem) or ZOOM_CLIP_RE.match(stem)
    )
    shot_part = stem
    if num_match:
        shot_part = num_match.group(1).zfill(4)
    else:
        oc_match = CANON_OC_RE.match(stem)
        if oc_match:
            shot_part = oc_match.group(4).zfill(4)

    return f"{code}-{date_part}{unit_part}-{shot_part}"


# --------------------------------------------------------------------------
# Building one asset record
# --------------------------------------------------------------------------

def build_media_intel(
    dv: DiscoveredVideo,
    root: Path,
    compute_hash: bool,
    tool_versions: dict,
) -> dict:
    path = dv.video_path
    stat = path.stat()

    ffprobe_data = probe_ffprobe(path)
    exif_data = probe_exiftool(path)
    xml_data = parse_sony_nrt_xml(dv.sidecars[".xml"]) if ".xml" in dv.sidecars else None

    device_info = classify_device(path, dv.sidecars, xml_data, ffprobe_data, exif_data)
    shoot_date = parse_shoot_date_from_path(path)

    duration_seconds = None
    if ffprobe_data and ffprobe_data.get("duration_seconds") is not None:
        duration_seconds = ffprobe_data["duration_seconds"]
    elif xml_data and xml_data.get("duration_frames") and xml_data.get("capture_fps"):
        try:
            frames = float(xml_data["duration_frames"])
            fps = float(xml_data["capture_fps"])
            duration_seconds = frames / fps if fps else None
        except (ValueError, ZeroDivisionError):
            pass

    timecode_in = ""
    if ffprobe_data and ffprobe_data.get("timecode"):
        timecode_in = ffprobe_data["timecode"]
    elif xml_data and xml_data.get("timecode_start"):
        timecode_in = xml_data["timecode_start"]

    checksum = sha256_of_file(path) if compute_hash else None

    media_intel = {
        "scan_date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "shoot_date": shoot_date,
        "device_source": device_info["device_label"],
        "detection_method": device_info["detection_method"],
        "detection_confidence": device_info["confidence"],
        "camera_unit": device_info.get("unit"),
        "file": {
            "extension": path.suffix.lower(),
            "size_bytes": stat.st_size,
            "size_human": human_size(stat.st_size),
            "modified_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(
                timespec="seconds"
            ),
            "sha256": checksum,
        },
        "technical": {
            "duration_seconds": duration_seconds,
            "container_format": ffprobe_data.get("container_format") if ffprobe_data else None,
            "video_codec": ffprobe_data.get("video_codec") if ffprobe_data else (
                xml_data.get("video_codec") if xml_data else None
            ),
            "width": (ffprobe_data or {}).get("width") or (xml_data or {}).get("width"),
            "height": (ffprobe_data or {}).get("height") or (xml_data or {}).get("height"),
            "resolution_label": (ffprobe_data or {}).get("resolution_label"),
            "frame_rate": (ffprobe_data or {}).get("frame_rate") or (
                float(xml_data["capture_fps"]) if xml_data and xml_data.get("capture_fps") else None
            ),
            "bit_rate": (ffprobe_data or {}).get("bit_rate"),
            "pix_fmt": (ffprobe_data or {}).get("pix_fmt"),
            "audio_codec": (ffprobe_data or {}).get("audio_codec"),
            "audio_channels": (ffprobe_data or {}).get("audio_channels") or (
                xml_data.get("audio_channels") if xml_data else None
            ),
            "audio_sample_rate": (ffprobe_data or {}).get("audio_sample_rate") or (
                xml_data.get("audio_sample_rate") if xml_data else None
            ),
            "creation_time": (ffprobe_data or {}).get("creation_time") or (
                xml_data.get("creation_date") if xml_data else None
            ),
        },
        "device": {
            "make": device_info.get("make"),
            "model": device_info.get("model"),
            "serial_number": device_info.get("serial_number"),
        },
        "sidecars": {
            ext.lstrip("."): str(p.relative_to(root)) if p.is_relative_to(root) else str(p)
            for ext, p in dv.sidecars.items()
        }
        if hasattr(Path, "is_relative_to")
        else {ext.lstrip("."): str(p) for ext, p in dv.sidecars.items()},
        "xml_sidecar_parsed": xml_data,
        "tools_used": tool_versions,
    }

    return {
        "device_info": device_info,
        "shoot_date": shoot_date,
        "duration_seconds": duration_seconds,
        "timecode_in": timecode_in,
        "media_intel": media_intel,
    }


def make_asset_record(dv: DiscoveredVideo, root: Path, compute_hash: bool, tool_versions: dict) -> dict:
    path = dv.video_path
    built = build_media_intel(dv, root, compute_hash, tool_versions)

    try:
        rel_path = str(path.relative_to(root))
    except ValueError:
        rel_path = str(path)

    asset_id = build_asset_id(path, built["device_info"], built["shoot_date"])
    kind = "audio" if path.suffix.lower() in DEFAULT_AUDIO_EXTENSIONS else "video"

    record = {
        "asset_id": asset_id,
        "filename": path.name,
        "path": str(path),
        "kind": kind,
        "device": built["device_info"]["device_label"],
        "timecode_in": built["timecode_in"],
        "duration": seconds_to_timecode(built["duration_seconds"]),
    }
    for k, v in EDITORIAL_FIELDS.items():
        record[k] = v
    record["media_intel"] = built["media_intel"]
    record["_scan_key"] = str(path)  # internal, stripped before write if desired
    record["_rel_path"] = rel_path
    return record


# --------------------------------------------------------------------------
# DB load / merge / save
# --------------------------------------------------------------------------

def migrate_asset_schema(asset: dict) -> dict:
    """Upgrades one asset record in place to the current schema:
    'camera' (string) -> 'device', and backfills 'kind' -- needed for a DB
    written by a pre-device-rename version of this scanner (everything in
    it was video, since audio wasn't catalogued yet). Safe to call on an
    already-current record; it's a no-op then. This is a defensive second
    layer -- the primary migration for this project's real DB is a one-time
    pass documented alongside the orange-crush-media-intel-db.json rename,
    but keeping this here means an old DB file loaded directly by mistake
    still comes up clean instead of silently growing duplicate fields."""
    if "camera" in asset and "device" not in asset:
        asset["device"] = asset.pop("camera")
    asset.setdefault("kind", "video")
    mi = asset.get("media_intel")
    if isinstance(mi, dict) and "camera_source" in mi and "device_source" not in mi:
        mi["device_source"] = mi.pop("camera_source")
    return asset


def load_existing_db(db_path: Path, project: str) -> dict:
    if not db_path.exists():
        return {"project": project, "assets": []}
    try:
        with open(db_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("Could not read existing DB at %s (%s) -- starting fresh.", db_path, exc)
        return {"project": project, "assets": []}
    data.setdefault("project", project)
    data.setdefault("assets", [])
    data["assets"] = [migrate_asset_schema(a) for a in data["assets"]]
    return data


def unchanged_since_last_scan(existing: dict, path: Path) -> bool:
    intel = existing.get("media_intel") or {}
    file_info = intel.get("file") or {}
    if not file_info:
        return False
    try:
        stat = path.stat()
    except OSError:
        return False
    same_size = file_info.get("size_bytes") == stat.st_size
    existing_mtime = file_info.get("modified_at")
    if not same_size or not existing_mtime:
        return False
    try:
        existing_dt = datetime.fromisoformat(existing_mtime)
    except ValueError:
        return False
    current_dt = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
    return abs((current_dt - existing_dt).total_seconds()) < 1


def merge_assets(
    existing_assets: list[dict], fresh_by_path: dict[str, dict], on_disk_paths: set
) -> tuple[list[dict], dict]:
    """Upserts fresh records into existing_assets, keyed by absolute path.
    Preserves every editorial field on existing records. Returns
    (merged_list, stats).

    `on_disk_paths` is every path discover_videos() found this run --
    including files that were skipped from (re)probing because they were
    unchanged since the last scan. Only a path that is in NEITHER
    fresh_by_path NOR on_disk_paths is genuinely missing from disk; a
    path present in on_disk_paths but absent from fresh_by_path was just
    skipped as unchanged and must be left alone, not flagged missing.
    """
    stats = {"new": 0, "updated": 0, "unchanged": 0, "missing_on_disk": 0}
    seen_paths = set()

    merged: list[dict] = []
    for existing in existing_assets:
        p = existing.get("path")
        if p in fresh_by_path:
            fresh = fresh_by_path[p]
            merged_record = dict(existing)  # keep everything, including editorial fields
            for k in (
                "filename",
                "device",
                "kind",
                "timecode_in",
                "duration",
                "asset_id",
            ):
                if k == "asset_id" and existing.get("asset_id"):
                    continue  # never renumber an id a human/tool may already reference
                merged_record[k] = fresh[k]

            # media_intel: refresh only the keys this scanner owns:
            # preserve any other tool's additions (e.g. the schedule
            # enricher's 'production_day') instead of overwriting wholesale.
            existing_extra = {
                k: v
                for k, v in (existing.get("media_intel") or {}).items()
                if k not in SCANNER_OWNED_MEDIA_INTEL_KEYS
            }
            merged_record["media_intel"] = {**existing_extra, **fresh["media_intel"]}

            merged_record.pop("_scan_key", None)
            merged_record.pop("_rel_path", None)
            merged.append(merged_record)
            seen_paths.add(p)
            stats["updated"] += 1
        elif p in on_disk_paths:
            # Unchanged since last scan -- was intentionally not re-probed.
            # Leave the record exactly as it was, and clear any stale
            # missing_on_disk flag from a previous scan where it was absent.
            existing = dict(existing)
            if (existing.get("media_intel") or {}).get("missing_on_disk"):
                existing["media_intel"] = dict(existing["media_intel"])
                existing["media_intel"].pop("missing_on_disk", None)
            merged.append(existing)
            seen_paths.add(p)
            stats["unchanged"] += 1
        else:
            existing = dict(existing)
            existing["media_intel"] = dict(existing.get("media_intel") or {})
            existing["media_intel"]["missing_on_disk"] = True
            merged.append(existing)
            stats["missing_on_disk"] += 1

    for p, fresh in fresh_by_path.items():
        if p not in seen_paths:
            fresh = dict(fresh)
            fresh.pop("_scan_key", None)
            fresh.pop("_rel_path", None)
            merged.append(fresh)
            stats["new"] += 1

    return merged, stats


def write_db(db_path: Path, db: dict) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = db_path.with_suffix(db_path.suffix + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(db, f, indent=2, ensure_ascii=False)
        f.write("\n")
    tmp_path.replace(db_path)


# --------------------------------------------------------------------------
# Duplicate hinting (recovered/backup folders often duplicate originals)
# --------------------------------------------------------------------------

def report_possible_duplicates(assets: list[dict]) -> list[list[str]]:
    groups: dict[tuple, list[str]] = {}
    for a in assets:
        intel = a.get("media_intel") or {}
        size = (intel.get("file") or {}).get("size_bytes")
        duration = (intel.get("technical") or {}).get("duration_seconds")
        if not size or duration is None:
            continue
        key = (size, round(duration, 1))
        groups.setdefault(key, []).append(a.get("path"))
    return [paths for paths in groups.values() if len(paths) > 1]


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Scan camera-original footage and standalone audio recordings, and populate "
        "the Orange Crush media intelligence DB (JSON).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--root", required=True, help="Root folder to scan (e.g. the Original_Footage drive/folder -- "
                                                   "scanned recursively, so a single root above both the camera "
                                                   "folders and an Audio subfolder picks up everything in one pass).")
    p.add_argument("--db", required=True, help="Path to the JSON intel DB to update (created if missing).")
    p.add_argument("--project", default="Orange Crush", help="Project name written into the DB (default: 'Orange Crush').")
    p.add_argument(
        "--extensions",
        nargs="*",
        default=None,
        help=f"Extensions to scan (video and/or audio), e.g. --extensions .mp4 .mov .wav "
             f"(default: {sorted(DEFAULT_EXTENSIONS)}).",
    )
    p.add_argument(
        "--exclude",
        nargs="*",
        default=[],
        help="Glob pattern(s) matched against full paths to skip, e.g. "
        '--exclude "*DJIPocketRecovery*" "*RootBkup*"',
    )
    p.add_argument("--hash", action="store_true", help="Compute a sha256 checksum per file (slow on large files; off by default).")
    p.add_argument("--force-rescan", action="store_true", help="Re-probe every file even if size/mtime match the last scan.")
    p.add_argument("--workers", type=int, default=min(8, (os.cpu_count() or 4)), help="Parallel probing workers (default: min(8, cpu_count)).")
    p.add_argument("--dry-run", action="store_true", help="Scan and print a summary, but don't write the DB file.")
    p.add_argument("--inspect-xml", metavar="XML_PATH", help="Debug: parse one Sony sidecar XML and pretty-print the result, then exit.")
    p.add_argument("--verbose", "-v", action="store_true", help="Verbose logging.")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    if args.inspect_xml:
        result = parse_sony_nrt_xml(Path(args.inspect_xml))
        print(json.dumps(result, indent=2, default=str))
        return 0

    root = Path(args.root)
    if not root.exists():
        log.error("Root folder does not exist: %s", root)
        return 1

    extensions = {e.lower() if e.startswith(".") else f".{e.lower()}" for e in args.extensions} if args.extensions else DEFAULT_EXTENSIONS

    tool_versions = {
        "ffprobe": which("ffprobe") is not None,
        "exiftool": which("exiftool") is not None,
    }
    if not tool_versions["ffprobe"]:
        log.warning("ffprobe not found on PATH -- technical metadata (duration/resolution/fps/codec) will be limited to what the XML sidecar provides.")
    if not tool_versions["exiftool"]:
        log.warning("exiftool not found on PATH -- camera make/model detection will fall back to filename/folder heuristics.")

    log.info("Scanning %s ...", root)
    discovered = discover_videos(root, extensions, args.exclude)
    log.info("Found %d file(s) (video + audio).", len(discovered))

    db = load_existing_db(Path(args.db), args.project)
    existing_by_path = {a.get("path"): a for a in db["assets"] if a.get("path")}

    to_process: list[DiscoveredVideo] = []
    skipped_unchanged = 0
    for dv in discovered:
        key = str(dv.video_path)
        existing = existing_by_path.get(key)
        if existing and not args.force_rescan and unchanged_since_last_scan(existing, dv.video_path):
            skipped_unchanged += 1
            continue
        to_process.append(dv)

    log.info(
        "%d file(s) need (re)probing, %d unchanged since last scan.",
        len(to_process),
        skipped_unchanged,
    )

    fresh_by_path: dict[str, dict] = {}
    if to_process:
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {
                pool.submit(make_asset_record, dv, root, args.hash, tool_versions): dv
                for dv in to_process
            }
            done = 0
            for fut in concurrent.futures.as_completed(futures):
                dv = futures[fut]
                done += 1
                try:
                    record = fut.result()
                    fresh_by_path[str(dv.video_path)] = record
                except Exception as exc:  # noqa: BLE001 -- never let one bad file kill the scan
                    log.error("Failed to process %s: %s", dv.video_path, exc)
                if done % 50 == 0 or done == len(to_process):
                    log.info("Probed %d/%d", done, len(to_process))

    on_disk_paths = {str(dv.video_path) for dv in discovered}
    merged_assets, stats = merge_assets(db["assets"], fresh_by_path, on_disk_paths)
    db["assets"] = merged_assets
    db["project"] = args.project
    db["asset_kinds"] = sorted({a.get("kind") for a in merged_assets if a.get("kind")})
    db.pop("media_type", None)  # superseded by per-asset "kind" now that video + audio share one DB
    db["last_scanned"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    db["last_scan_root"] = str(root)

    dup_groups = report_possible_duplicates(merged_assets)

    print("\n--- Scan summary ---")
    print(f"  New assets:            {stats['new']}")
    print(f"  Updated (re-probed):   {stats['updated']}")
    print(f"  Unchanged (skipped):   {stats['unchanged']}")
    print(f"  In DB but not on disk: {stats['missing_on_disk']}")
    print(f"  Total assets in DB:    {len(merged_assets)}")
    if dup_groups:
        print(f"  Possible duplicate groups (same size+duration): {len(dup_groups)}")
        for grp in dup_groups[:10]:
            print("    -", " | ".join(grp))
        if len(dup_groups) > 10:
            print(f"    ... and {len(dup_groups) - 10} more")

    by_kind: dict[str, int] = {}
    for a in merged_assets:
        by_kind[a.get("kind") or "unknown"] = by_kind.get(a.get("kind") or "unknown", 0) + 1
    print("  By kind:", ", ".join(f"{k}: {v}" for k, v in sorted(by_kind.items())))

    by_device: dict[str, int] = {}
    for a in merged_assets:
        dev = a.get("device") or "Unknown"
        by_device[dev] = by_device.get(dev, 0) + 1
    print("  By device:")
    for dev, count in sorted(by_device.items(), key=lambda x: -x[1]):
        print(f"    {dev}: {count}")

    if args.dry_run:
        print("\n(--dry-run set: DB file was not written)")
        return 0

    write_db(Path(args.db), db)
    print(f"\nWrote {len(merged_assets)} asset record(s) to {args.db}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
