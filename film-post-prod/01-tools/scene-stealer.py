#!/usr/bin/env python3
"""
scene-stealer.py -- local, offline audio transcription for Orange Crush footage.

Runs faster-whisper (CTranslate2) over camera-original clips -- either
every asset listed in video-media-intel-db.json, or every file under a
folder you point it at -- and writes a pipe-delimited transcript file:

    asset_id|filepath|filename|transcription|duration_sec|language|
    language_probability|avg_logprob|slate_text|model|device|compute_type|
    transcribed_at_utc|status|error

`filepath`, `filename`, and `transcription` are the three required
columns; everything else is bonus context that comes along for free from
the same transcription pass. `slate_text` is just the first few seconds
of transcript (see whisper.slate_window_seconds in the yaml) -- this
script does not parse it into a scene/take number itself, that's a
separate follow-up tool. Nothing in this script writes back into
video-media-intel-db.json; it only reads clip paths from it.

`status` is one of: "ok" (transcribed, possibly with an empty
`transcription` if VAD found no speech -- normal for silent B-roll),
"error" (a real decode/extraction problem -- see `error` for detail, and
retry with --retry-errors after fixing the cause), or "no_audio" (ffprobe
confirmed the clip has no audio stream at all, e.g. mic-off drone
footage -- nothing to transcribe, not a failure, and not retried).

Every tunable parameter lives in scene-stealer.yaml (next to this script
by default). See that file for what each one does -- this script embeds
the same values as built-in defaults, so it still runs sensibly even if
the yaml is missing or only partially filled in.

Prerequisites (see chat history / README for the Blackwell-GPU notes):
    pip install faster-whisper
    pip install --upgrade "ctranslate2>=4.8.2"
    pip install nvidia-cublas-cu12 nvidia-cudnn-cu12   # GPU only
    pip install pyyaml

Usage:
    python scene-stealer.py                       # uses scene-stealer.yaml next to this script
    python scene-stealer.py --config other.yaml
    python scene-stealer.py --dry-run              # list what would be processed, load nothing, transcribe nothing
    python scene-stealer.py --retry-errors         # re-attempt only the rows currently marked status=error
"""

import argparse
import csv
import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

try:
    import yaml
except ImportError:
    print("Missing dependency: PyYAML. Install it with:\n    pip install pyyaml", file=sys.stderr)
    sys.exit(1)

DEFAULT_CONFIG_PATH = Path(__file__).with_name("scene-stealer.yaml")

log = logging.getLogger("scene_stealer")


# ---------------------------------------------------------------------------
# Config: built-in defaults + yaml overrides, deep-merged so a yaml file
# only needs to mention the keys it wants to change.
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "source": {
        "mode": "intel_db",
        "intel_db_path": "../20-video/orange-crush-media-intel-db.json",
        "root_folder": None,
        "extensions": [".mp4", ".mov", ".mxf", ".avi", ".m4v"],
    },
    "filters": {
        "skip_missing_on_disk": True,
        "skip_cr_floor": True,
        "only_devices": [],
        "only_dates": [],
        "limit": None,
    },
    "whisper": {
        "model_size": "large-v3-turbo",
        "device": "cuda",
        "compute_type": "float16",
        "device_index": 0,
        "cpu_threads": 0,
        "num_workers": 1,
        "auto_fallback_cpu": True,
        "beam_size": 5,
        "vad_filter": True,
        "vad_min_silence_duration_ms": 500,
        "language": None,
        "initial_prompt": None,
        "condition_on_previous_text": True,
        "temperature": 0.0,
        "word_timestamps": False,
        "slate_window_seconds": 8,
    },
    "audio": {
        "ffmpeg_fallback": True,
        "ffmpeg_path": "ffmpeg",
        "ffprobe_path": "ffprobe",   # used only to pre-check whether a clip has an audio stream at all
        "skip_no_audio_check": False,  # true disables the ffprobe pre-check below (falls back to old behavior)
        "sample_rate": 16000,
        "channels": 1,
        "keep_temp_wav": False,
    },
    "output": {
        "path": "../20-video/scene-stealer-transcripts.dsv",
        "delimiter": "|",
        "mode": "overwrite",
        "write_json_sidecar": True,
        "temp_audio_dir": "../20-video/_scene_stealer_tmp",
    },
    "logging": {
        "level": "INFO",
        "progress_every": 10,
    },
}

OUTPUT_COLUMNS = [
    "asset_id", "filepath", "filename", "transcription", "duration_sec",
    "language", "language_probability", "avg_logprob", "slate_text",
    "model", "device", "compute_type", "transcribed_at_utc", "status", "error",
]


def deep_merge(base: dict, override: dict) -> dict:
    result = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(result.get(k), dict):
            result[k] = deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def resolve_path(cfg_dir: Path, value):
    if not value:
        return value
    p = Path(value)
    return str(p if p.is_absolute() else (cfg_dir / p).resolve())


def load_config(config_path: Path) -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # cheap deep copy
    if config_path.exists():
        with open(config_path, "r", encoding="utf-8") as f:
            user_cfg = yaml.safe_load(f) or {}
        cfg = deep_merge(cfg, user_cfg)
    else:
        print(f"NOTE: config file {config_path} not found -- using built-in defaults.", file=sys.stderr)

    cfg_dir = config_path.resolve().parent
    if cfg["source"].get("intel_db_path"):
        cfg["source"]["intel_db_path"] = resolve_path(cfg_dir, cfg["source"]["intel_db_path"])
    if cfg["source"].get("root_folder"):
        cfg["source"]["root_folder"] = resolve_path(cfg_dir, cfg["source"]["root_folder"])
    cfg["output"]["path"] = resolve_path(cfg_dir, cfg["output"]["path"])
    cfg["output"]["temp_audio_dir"] = resolve_path(cfg_dir, cfg["output"]["temp_audio_dir"])
    return cfg


def setup_logging(cfg: dict):
    level_name = str(cfg["logging"].get("level", "INFO")).upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")


# ---------------------------------------------------------------------------
# Clip discovery
# ---------------------------------------------------------------------------

@dataclass
class ClipTask:
    asset_id: str
    path: str
    filename: str
    device: str = ""
    shoot_date: str = ""


def discover_from_intel_db(cfg: dict) -> list:
    db_path = Path(cfg["source"]["intel_db_path"])
    if not db_path.exists():
        raise FileNotFoundError(f"intel_db_path does not exist: {db_path}")
    with open(db_path, "r", encoding="utf-8") as f:
        db = json.load(f)

    f_cfg = cfg["filters"]
    only_devices = set(f_cfg.get("only_devices") or [])
    only_dates = set(f_cfg.get("only_dates") or [])

    tasks = []
    for a in db.get("assets", []):
        mi = a.get("media_intel") or {}
        if f_cfg.get("skip_missing_on_disk", True) and mi.get("missing_on_disk"):
            continue
        if f_cfg.get("skip_cr_floor", True) and a.get("cr_floor"):
            continue
        if only_devices and a.get("device") not in only_devices:
            continue
        if only_dates and mi.get("shoot_date") not in only_dates:
            continue
        path = a.get("path") or ""
        tasks.append(ClipTask(
            asset_id=a.get("asset_id", ""),
            path=path,
            filename=a.get("filename") or Path(path).name,
            device=a.get("device", ""),
            shoot_date=mi.get("shoot_date", ""),
        ))

    limit = f_cfg.get("limit")
    if limit:
        tasks = tasks[:limit]
    return tasks


def discover_from_folder(cfg: dict) -> list:
    root_folder = cfg["source"].get("root_folder")
    if not root_folder:
        raise ValueError("source.mode is 'folder' but source.root_folder is not set")
    root = Path(root_folder)
    if not root.exists():
        raise FileNotFoundError(f"root_folder does not exist: {root}")
    exts = {e.lower() for e in cfg["source"].get("extensions", [])}

    tasks = []
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.suffix.lower() in exts:
            tasks.append(ClipTask(asset_id="", path=str(p), filename=p.name))

    limit = cfg["filters"].get("limit")
    if limit:
        tasks = tasks[:limit]
    return tasks


def discover_clips(cfg: dict) -> list:
    mode = cfg["source"]["mode"]
    if mode == "intel_db":
        return discover_from_intel_db(cfg)
    if mode == "folder":
        return discover_from_folder(cfg)
    raise ValueError(f"Unknown source.mode: {mode!r} (expected 'intel_db' or 'folder')")


# ---------------------------------------------------------------------------
# Resume support: read filepaths already present in an existing output file.
# ---------------------------------------------------------------------------

def split_out_error_rows(output_path: Path, delimiter: str):
    """Read an existing output file and split it into the rows worth
    keeping (anything not status=error) and the set of filepaths whose row
    *was* an error, so those clips can be retried. Returns
    (header, kept_rows, retry_filepaths). Used by --retry-errors."""
    if not output_path.exists() or output_path.stat().st_size == 0:
        return list(OUTPUT_COLUMNS), [], set()
    with open(output_path, "r", encoding="utf-8", newline="") as f:
        reader = csv.reader(f, delimiter=delimiter)
        header = next(reader, None) or list(OUTPUT_COLUMNS)
        try:
            fp_idx = header.index("filepath")
        except ValueError:
            fp_idx = 1
        try:
            status_idx = header.index("status")
        except ValueError:
            status_idx = None
        kept_rows, retry_filepaths = [], set()
        for row in reader:
            is_error = status_idx is not None and len(row) > status_idx and row[status_idx] == "error"
            if is_error and len(row) > fp_idx:
                retry_filepaths.add(row[fp_idx])
            else:
                kept_rows.append(row)
    return header, kept_rows, retry_filepaths


def rewrite_output_file(output_path: Path, delimiter: str, header, rows):
    with open(output_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, delimiter=delimiter, lineterminator="\n")
        writer.writerow(header)
        for row in rows:
            writer.writerow(row)


def load_done_paths(output_path: Path, delimiter: str) -> set:
    if not output_path.exists() or output_path.stat().st_size == 0:
        return set()
    done = set()
    with open(output_path, "r", encoding="utf-8", newline="") as f:
        reader = csv.reader(f, delimiter=delimiter)
        header = next(reader, None)
        if not header or "filepath" not in header:
            return set()
        idx = header.index("filepath")
        for row in reader:
            if len(row) > idx:
                done.add(row[idx])
    return done


# ---------------------------------------------------------------------------
# ffmpeg fallback extraction (only used if faster-whisper's own PyAV-based
# decode fails on a particular file's container/codec).
# ---------------------------------------------------------------------------

def probe_stream_types(src_path: str, cfg: dict):
    """Best-effort: ask ffprobe what kind of streams src_path actually
    contains, so a clip that genuinely has no audio track (seen on this
    project: DJI drone footage with only a video stream plus a
    proprietary "DJI.Subtitle" telemetry/metadata track, no microphone
    audio at all) can be labeled accurately instead of retried forever as
    a decode "error". Returns a set like {"video", "audio"} or {"video"},
    or None if ffprobe couldn't tell at all (missing binary, or the file
    is too broken to probe) -- callers should treat None as inconclusive
    and fall through to the normal decode attempts, not as "no audio"."""
    a_cfg = cfg["audio"]
    if a_cfg.get("skip_no_audio_check"):
        return None
    cmd = [a_cfg.get("ffprobe_path", "ffprobe"), "-v", "error",
           "-show_entries", "stream=codec_type", "-of", "csv=p=0", src_path]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    out = (proc.stdout or b"").decode("utf-8", errors="replace")
    types = {line.strip() for line in out.splitlines() if line.strip()}
    return types or None


def sniff_container_hint(path: str) -> str:
    """Peek at a file's actual leading bytes to catch cases where the real
    container doesn't match its extension. Seen on this project: DJI
    drone-footage recovery output (the DJIFIX/ folder, and a batch of
    "Orange Crush Fantasy Scene" drone clips) saved with a .MP4/.MOV
    extension whose bytes are actually a WAV file -- presumably a
    recovery tool that could only save the audio track after an
    interrupted/corrupted recording. Returns a bare format name (e.g.
    "wav") that ffmpeg's -f flag understands when a mismatch like that is
    detected, else "" -- never raises, so a read error just means no hint."""
    try:
        with open(path, "rb") as f:
            head = f.read(12)
    except OSError:
        return ""
    if len(head) >= 12 and head[0:4] == b"RIFF" and head[8:12] == b"WAVE":
        if Path(path).suffix.lower() != ".wav":
            return "wav"
    return ""


def extract_audio_wav(src_path: str, cfg: dict, tmp_dir: Path, input_format: str = "") -> Path:
    a_cfg = cfg["audio"]
    out_path = tmp_dir / (Path(src_path).stem + ".wav")
    cmd = [a_cfg["ffmpeg_path"]]
    if input_format:
        # Force the real input format instead of letting ffmpeg guess from
        # the (misleading) extension -- see sniff_container_hint().
        cmd += ["-f", input_format]
    cmd += [
        "-y", "-i", src_path,
        "-vn", "-ac", str(a_cfg["channels"]), "-ar", str(a_cfg["sample_rate"]),
        "-f", "wav", str(out_path),
    ]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        # Don't use check=True -- CalledProcessError's default str() is just
        # "Command '[...]' returned non-zero exit status N", which throws
        # away the one thing that actually explains the failure: ffmpeg's
        # own stderr. Surface the last few lines of it instead (the top of
        # ffmpeg's stderr is just its build/banner info; the real reason is
        # almost always in the last line or two, e.g. "Output file does not
        # contain any stream" for a video-only source with no audio track).
        stderr_text = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
        tail = "\n".join(stderr_text.splitlines()[-6:]) if stderr_text else "(ffmpeg wrote nothing to stderr)"
        raise RuntimeError(f"ffmpeg exit code {proc.returncode}: {tail}")
    return out_path


# ---------------------------------------------------------------------------
# Model loading, with an optional automatic GPU -> CPU fallback (useful
# while GPU library versions for very new cards -- e.g. Blackwell -- are
# still settling down).
# ---------------------------------------------------------------------------

def _register_windows_cuda_dll_dirs():
    """On Windows, the nvidia-cublas-cu12 / nvidia-cudnn-cu12 pip wheels drop
    their DLLs inside site-packages\\nvidia\\<pkg>\\bin rather than anywhere
    Windows' DLL loader checks by default. That surfaces as e.g. "Library
    cublas64_12.dll is not found or cannot be loaded" even though the
    packages are installed and `pip show` finds them -- Windows just never
    looked in that folder. Registering it explicitly (Python 3.8+'s
    os.add_dll_directory) fixes that for this process without needing to
    edit PATH and restart every terminal."""
    if sys.platform != "win32":
        return
    for pkg_name in ("nvidia.cublas", "nvidia.cudnn"):
        try:
            pkg = __import__(pkg_name, fromlist=["_"])
        except ImportError:
            continue
        # These pip wheels are commonly installed as PEP 420 namespace
        # packages (no __init__.py), which means __file__ is None -- use
        # __path__ instead, which is populated for both regular and
        # namespace packages.
        search_dirs = [str(p) for p in (getattr(pkg, "__path__", None) or [])]
        if not search_dirs and getattr(pkg, "__file__", None):
            search_dirs = [str(Path(pkg.__file__).parent)]
        for d in search_dirs:
            bin_dir = Path(d) / "bin"
            if bin_dir.is_dir():
                try:
                    os.add_dll_directory(str(bin_dir))
                    log.debug("Registered DLL directory: %s", bin_dir)
                except (OSError, AttributeError):
                    pass


def load_model(cfg: dict):
    _register_windows_cuda_dll_dirs()
    from faster_whisper import WhisperModel

    w = cfg["whisper"]
    try:
        model = WhisperModel(
            w["model_size"], device=w["device"], device_index=w["device_index"],
            compute_type=w["compute_type"], cpu_threads=w["cpu_threads"], num_workers=w["num_workers"],
        )
        log.info("Loaded model %s on %s (%s)", w["model_size"], w["device"], w["compute_type"])
        return model, w["device"], w["compute_type"]
    except Exception as exc:
        if w["device"] != "cpu" and w.get("auto_fallback_cpu", True):
            log.warning("GPU model load failed (%s) -- falling back to CPU/int8.", exc)
            model = WhisperModel(
                w["model_size"], device="cpu", compute_type="int8",
                cpu_threads=w["cpu_threads"], num_workers=w["num_workers"],
            )
            return model, "cpu", "int8"
        raise


def reload_model_on_cpu(cfg: dict):
    from faster_whisper import WhisperModel
    w = cfg["whisper"]
    model = WhisperModel(
        w["model_size"], device="cpu", compute_type="int8",
        cpu_threads=w["cpu_threads"], num_workers=w["num_workers"],
    )
    return model, "cpu", "int8"


# ---------------------------------------------------------------------------
# Transcription of a single clip
# ---------------------------------------------------------------------------

GPU_LIBRARY_ERROR_MARKERS = ("cublas", "cudnn", "cuda", ".dll", "libcu", "library is not found")


def _is_gpu_library_error(exc) -> bool:
    """True for a missing/unloadable CUDA library (cuBLAS/cuDNN DLL or .so),
    as opposed to a decode/container problem. ffmpeg-extracting audio can't
    fix a missing GPU library, so it's not worth trying that fallback for
    this class of error -- and the caller can use this to trigger a switch
    to CPU for the rest of the run instead of failing clip after clip."""
    msg = str(exc).lower()
    return any(marker in msg for marker in GPU_LIBRARY_ERROR_MARKERS)


def sanitize_field(text, delimiter: str) -> str:
    if text is None:
        return ""
    t = str(text).replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    t = t.replace(delimiter, "¦")  # broken-bar stand-in so a stray '|' can't split the row
    return " ".join(t.split())


def _run_transcribe(model, source, w: dict):
    segments, info = model.transcribe(
        source,
        beam_size=w["beam_size"],
        language=w["language"],
        vad_filter=w["vad_filter"],
        vad_parameters=dict(min_silence_duration_ms=w["vad_min_silence_duration_ms"]),
        initial_prompt=w["initial_prompt"],
        condition_on_previous_text=w["condition_on_previous_text"],
        temperature=w["temperature"],
        word_timestamps=w["word_timestamps"],
    )
    return list(segments), info  # materialize the generator so errors surface here, not later


def transcribe_clip(model, task: ClipTask, cfg: dict, tmp_dir: Path) -> dict:
    w = cfg["whisper"]
    delimiter = cfg["output"]["delimiter"]
    result = {
        "asset_id": task.asset_id, "filepath": task.path, "filename": task.filename,
        "transcription": "", "duration_sec": "", "language": "", "language_probability": "",
        "avg_logprob": "", "slate_text": "", "status": "ok", "error": "", "_segments": None,
        "gpu_library_error": False,
    }

    stream_types = probe_stream_types(task.path, cfg)
    if stream_types is not None and "audio" not in stream_types:
        # Confirmed by ffprobe, not guessed: this clip has no audio stream
        # at all (just video, and on DJI footage often a proprietary
        # "DJI.Subtitle" telemetry track) -- there is nothing to
        # transcribe, so don't waste a decode attempt or count it as a
        # failure. If ffprobe itself couldn't read the file (returns
        # None, e.g. a genuinely corrupted/truncated recording), fall
        # through to the normal attempts below so that real problem still
        # gets a proper error message.
        result.update(status="no_audio",
                       error="source clip has no audio stream (video-only recording, e.g. mic-off drone footage) "
                             "-- nothing to transcribe")
        return result

    tmp_wav = None
    attempt_notes = []  # accumulated so a final failure's error message shows every attempt, not just the last

    def _discard_tmp_wav():
        nonlocal tmp_wav
        if tmp_wav is not None and not cfg["audio"].get("keep_temp_wav", False):
            try:
                tmp_wav.unlink(missing_ok=True)
            except OSError:
                pass
        tmp_wav = None

    container_hint = sniff_container_hint(task.path)
    segments = info = None

    if container_hint:
        # This file's real content doesn't match its extension (see
        # sniff_container_hint) -- try decoding it with the true format
        # forced before falling back to the normal (extension-trusting)
        # attempts below, since those are the ones known to fail on files
        # like this.
        try:
            tmp_wav = extract_audio_wav(task.path, cfg, tmp_dir, input_format=container_hint)
            segments, info = _run_transcribe(model, str(tmp_wav), w)
            log.info("%s: extension suggested video but content is %s audio -- decoded correctly with the format forced.",
                      task.filename, container_hint)
        except Exception as exc_hint:
            if _is_gpu_library_error(exc_hint):
                _discard_tmp_wav()
                result.update(status="error", error=f"GPU library error: {exc_hint}", gpu_library_error=True)
                return result
            attempt_notes.append(f"forced-format ({container_hint}) read failed: {exc_hint}")
            log.warning("%s: forced-format (%s) read failed (%s) -- trying the normal decode path.",
                        task.filename, container_hint, exc_hint)
            _discard_tmp_wav()

    if segments is None:
        try:
            segments, info = _run_transcribe(model, task.path, w)
        except Exception as exc1:
            if _is_gpu_library_error(exc1):
                result.update(status="error", error=f"GPU library error: {exc1}", gpu_library_error=True)
                return result
            if not cfg["audio"].get("ffmpeg_fallback", True):
                attempt_notes.append(f"decode failed: {exc1}")
                result.update(status="error", error="; ".join(attempt_notes))
                return result
            log.warning("Direct decode failed for %s (%s) -- trying ffmpeg extraction fallback.", task.filename, exc1)
            try:
                tmp_wav = extract_audio_wav(task.path, cfg, tmp_dir)
                segments, info = _run_transcribe(model, str(tmp_wav), w)
            except Exception as exc2:
                if _is_gpu_library_error(exc2):
                    _discard_tmp_wav()
                    result.update(status="error", error=f"GPU library error (after ffmpeg fallback): {exc2}", gpu_library_error=True)
                    return result
                # Keep the original direct-decode error too -- it's often the
                # more informative one (e.g. PyAV's message about a missing
                # audio stream), and previously got silently discarded once
                # the ffmpeg fallback also failed.
                attempt_notes.append(f"direct decode failed: {exc1}")
                attempt_notes.append(f"ffmpeg fallback also failed: {exc2}")
                _discard_tmp_wav()
                result.update(status="error", error="; ".join(attempt_notes))
                return result

    _discard_tmp_wav()

    full_text = " ".join(seg.text.strip() for seg in segments).strip()
    slate_cutoff = w.get("slate_window_seconds", 8)
    slate_text = " ".join(seg.text.strip() for seg in segments if seg.start < slate_cutoff).strip()
    logprobs = [seg.avg_logprob for seg in segments if getattr(seg, "avg_logprob", None) is not None]

    result.update(
        transcription=sanitize_field(full_text, delimiter),
        slate_text=sanitize_field(slate_text, delimiter),
        duration_sec=f"{info.duration:.2f}" if getattr(info, "duration", None) else "",
        language=info.language or "",
        language_probability=(f"{info.language_probability:.3f}"
                               if getattr(info, "language_probability", None) is not None else ""),
        avg_logprob=f"{(sum(logprobs) / len(logprobs)):.3f}" if logprobs else "",
        _segments=[{"start": s.start, "end": s.end, "text": s.text.strip()} for s in segments],
    )
    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Local offline transcription for Orange Crush footage (faster-whisper).")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help="Path to scene-stealer.yaml")
    parser.add_argument("--dry-run", action="store_true",
                         help="List clips that would be processed and exit -- loads no model, transcribes nothing.")
    parser.add_argument("--retry-errors", action="store_true",
                         help="Re-run only the clips whose existing row in the output file has status=error "
                              "(e.g. after a fix), leaving every other already-processed row untouched. "
                              "Ignored (falls through to the configured output.mode) if the output file "
                              "doesn't exist yet or has no error rows.")
    args = parser.parse_args()

    cfg = load_config(args.config)
    setup_logging(cfg)

    try:
        clips = discover_clips(cfg)
    except (FileNotFoundError, ValueError) as exc:
        log.error("%s", exc)
        sys.exit(1)
    log.info("Discovered %d clip(s) matching the configured filters.", len(clips))

    out_path = Path(cfg["output"]["path"])
    delimiter = cfg["output"]["delimiter"]
    out_mode = cfg["output"].get("mode", "overwrite")

    done_paths = set()
    if args.retry_errors and out_path.exists() and out_path.stat().st_size > 0:
        header, kept_rows, retry_filepaths = split_out_error_rows(out_path, delimiter)
        rewrite_output_file(out_path, delimiter, header, kept_rows)
        out_mode = "resume"  # append fresh results for the retried (and any brand-new) clips below
        try:
            fp_idx = header.index("filepath")
            done_paths = {row[fp_idx] for row in kept_rows if len(row) > fp_idx}
        except ValueError:
            done_paths = set()
        if retry_filepaths:
            log.info("Retry mode: %d error row(s) removed from %s and queued for retry; %d other row(s) kept as-is.",
                      len(retry_filepaths), out_path, len(kept_rows))
        else:
            log.info("Retry mode: no error rows found in %s -- nothing to retry.", out_path)
    elif out_mode == "resume":
        done_paths = load_done_paths(out_path, delimiter)
        if done_paths:
            log.info("Resume mode: %d clip(s) already in %s will be skipped.", len(done_paths), out_path)

    pending = [c for c in clips if c.path not in done_paths]
    log.info("%d clip(s) pending.", len(pending))

    if args.dry_run:
        for c in pending[:50]:
            print(f"{c.asset_id or '-'}\t{c.path}")
        if len(pending) > 50:
            print(f"... and {len(pending) - 50} more")
        print(f"\nDRY RUN -- {len(pending)} clip(s) would be transcribed. No model loaded, nothing written.")
        return

    if not pending:
        log.info("Nothing to do.")
        return

    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_dir = Path(cfg["output"]["temp_audio_dir"])
    tmp_dir.mkdir(parents=True, exist_ok=True)

    model, device_used, compute_type_used = load_model(cfg)

    file_exists_with_content = out_path.exists() and out_path.stat().st_size > 0
    file_mode = "a" if (out_mode == "resume" and file_exists_with_content) else "w"
    write_header = not (out_mode == "resume" and file_exists_with_content)

    sidecar_path = out_path.with_suffix(out_path.suffix + ".json") if cfg["output"].get("write_json_sidecar") else None
    sidecar_data = {}
    if sidecar_path and out_mode == "resume" and sidecar_path.exists():
        try:
            sidecar_data = json.loads(sidecar_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            sidecar_data = {}

    processed = 0
    errors = 0
    no_audio = 0
    cpu_fallback_triggered = False
    t_start = time.time()
    progress_every = cfg["logging"].get("progress_every", 10)

    with open(out_path, file_mode, encoding="utf-8", newline="") as f:
        writer = csv.writer(f, delimiter=delimiter, lineterminator="\n")
        if write_header:
            writer.writerow(OUTPUT_COLUMNS)
            f.flush()

        for i, task in enumerate(pending, start=1):
            log.debug("Transcribing (%d/%d): %s", i, len(pending), task.filename)

            if not Path(task.path).exists():
                row_result = {
                    "asset_id": task.asset_id, "filepath": task.path, "filename": task.filename,
                    "transcription": "", "duration_sec": "", "language": "", "language_probability": "",
                    "avg_logprob": "", "slate_text": "", "status": "error",
                    "error": "file not found on disk", "_segments": None,
                }
            else:
                row_result = transcribe_clip(model, task, cfg, tmp_dir)

                if (row_result.get("gpu_library_error") and device_used != "cpu"
                        and not cpu_fallback_triggered and cfg["whisper"].get("auto_fallback_cpu", True)):
                    log.warning(
                        "GPU library error on %s (%s) -- reloading the model on CPU/int8 and "
                        "retrying this clip and the rest of the run there.",
                        task.filename, row_result.get("error"),
                    )
                    model, device_used, compute_type_used = reload_model_on_cpu(cfg)
                    cpu_fallback_triggered = True
                    row_result = transcribe_clip(model, task, cfg, tmp_dir)

            if row_result["status"] == "error":
                errors += 1
                log.warning("FAILED %s: %s", task.filename, row_result["error"])
            elif row_result["status"] == "no_audio":
                no_audio += 1
                log.info("No audio track, skipped: %s", task.filename)

            segments = row_result.pop("_segments", None)
            if sidecar_path is not None:
                sidecar_data[task.path] = {
                    "asset_id": task.asset_id,
                    "segments": segments or [],
                    "language": row_result.get("language"),
                    "language_probability": row_result.get("language_probability"),
                }

            writer.writerow([
                row_result.get("asset_id", ""), row_result.get("filepath", ""), row_result.get("filename", ""),
                row_result.get("transcription", ""), row_result.get("duration_sec", ""), row_result.get("language", ""),
                row_result.get("language_probability", ""), row_result.get("avg_logprob", ""), row_result.get("slate_text", ""),
                cfg["whisper"]["model_size"], device_used, compute_type_used,
                datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                row_result.get("status", "ok"), row_result.get("error", ""),
            ])
            f.flush()  # each clip's row is durable immediately -- a crash mid-run loses at most the in-flight clip
            processed += 1

            if progress_every and processed % progress_every == 0:
                elapsed = time.time() - t_start
                rate = elapsed / processed
                remaining = (len(pending) - processed) * rate
                log.info("Progress: %d/%d done (%d error(s), %d no-audio) -- ~%.0fs/clip, ~%.0fmin remaining",
                          processed, len(pending), errors, no_audio, rate, remaining / 60)

    if sidecar_path is not None:
        sidecar_path.write_text(json.dumps(sidecar_data, indent=2, ensure_ascii=False), encoding="utf-8")
        log.info("Segment-level detail written to %s", sidecar_path)

    log.info("Done. %d clip(s) processed, %d error(s), %d no-audio (skipped, not an error). Output: %s",
              processed, errors, no_audio, out_path)


if __name__ == "__main__":
    main()
