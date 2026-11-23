from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from pathlib import Path

from syrinx_backend.models import AnnotationDocument, AnnotationFilter, AnnotationRecord

# Required field names from syrinx_original_cpp/common/Annotation.h
FN_STARTTIME = "lefttimesec"
FN_ENDTIME = "righttimesec"
FN_DURATION = "durationsec"
FN_LOWFREQ = "bottomfreqhz"
FN_HIGHFREQ = "topfreqhz"
FN_CHANNEL = "channel"
FN_COMMENT = "comment"
FN_FILEPATH = "soundfile"
FN_NOTES = "notes"
FN_STARTTIME_ABS = "starttime"
FN_STARTDATE_ABS = "startdate"

REQUIRED_FIELD_ORDER = [
    FN_FILEPATH,
    FN_CHANNEL,
    FN_STARTTIME,
    FN_ENDTIME,
    FN_HIGHFREQ,
    FN_LOWFREQ,
    FN_COMMENT,
    FN_NOTES,
]

_KNOWN_REQUIRED = set(REQUIRED_FIELD_ORDER) | {FN_DURATION, FN_STARTTIME_ABS, FN_STARTDATE_ABS}


def _extract_filename(filepath: str) -> str:
    if not filepath:
        return ""
    # Handle both Windows and POSIX path separators cleanly across platforms
    normalized = filepath.replace("\\", "/")
    return Path(normalized).name


def decode_syrinx_notes(raw_notes: str) -> str:
    """Restore newlines from Syrinx's SOH (0x02) and '@' escape conventions (Annotation.cpp)."""
    return raw_notes.replace("\x02", "\n").replace("@", "\n")


def encode_syrinx_notes(notes: str) -> str:
    """Encode newlines for tab-delimited single-line Syrinx logs (Annotation.cpp::SetNotes)."""
    return notes.replace("\r", "").replace("\n", "@")


def _safe_float(val: str, default: float = 0.0) -> float:
    try:
        return float(val.strip())
    except (ValueError, AttributeError):
        return default


def _safe_int(val: str, default: int = 1) -> int:
    try:
        return int(float(val.strip()))
    except (ValueError, AttributeError):
        return default


def _parse_an_line(
    fields: list[str],
    key_columns: list[str],
    last_filepath: str,
) -> tuple[AnnotationRecord | None, str]:
    soundfile = ""
    channel = 1
    start_time = 0.0
    end_time = 0.0
    low_freq = 0.0
    high_freq = 0.0
    comment = ""
    notes = ""
    extra: dict[str, str | float | int] = {}

    for col_name, val in zip(key_columns, fields[1:]):
        col = col_name.strip()
        if not col:
            continue
        if col == FN_FILEPATH:
            soundfile = _extract_filename(val)
        elif col == FN_CHANNEL:
            channel = _safe_int(val, 1)
        elif col == FN_STARTTIME:
            start_time = _safe_float(val, 0.0)
        elif col == FN_ENDTIME:
            end_time = _safe_float(val, 0.0)
        elif col == FN_HIGHFREQ:
            high_freq = _safe_float(val, 0.0)
        elif col == FN_LOWFREQ:
            low_freq = _safe_float(val, 0.0)
        elif col == FN_COMMENT:
            comment = val
        elif col == FN_NOTES:
            notes = decode_syrinx_notes(val)
        elif col in (FN_STARTTIME_ABS, FN_STARTDATE_ABS, FN_DURATION):
            extra[col] = val
        else:
            extra[col] = val

    if end_time <= start_time:
        return None, last_filepath

    if soundfile:
        last_filepath = soundfile
    else:
        soundfile = last_filepath

    rec = AnnotationRecord(
        id=str(uuid.uuid4()),
        soundfile=soundfile,
        channel=channel,
        start_time_sec=start_time,
        end_time_sec=end_time,
        low_freq_hz=low_freq,
        high_freq_hz=high_freq,
        comment=comment,
        notes=notes,
        extra_fields=extra,
        modified=False,
    )
    return rec, last_filepath


def _parse_legacy_pair_line(
    fields: list[str],
    last_filepath: str,
) -> tuple[AnnotationRecord | None, str]:
    """Parse older Syrinx key:/value tab-paired lines (AnnotationList.cpp::AddEntry)."""
    soundfile = ""
    channel = 1
    start_time = 0.0
    end_time = 0.0
    low_freq = 0.0
    high_freq = 0.0
    comment = ""
    notes = ""
    extra: dict[str, str | float | int] = {}

    i = 0
    while i < len(fields):
        key = fields[i].strip()
        val = fields[i + 1] if (i + 1) < len(fields) else ""
        if key in ("selected:", "soundfile:"):
            soundfile = _extract_filename(val)
            if soundfile:
                last_filepath = soundfile
            i += 2
        elif key == "comment:":
            comment = val
            i += 2
        elif key == "notes:":
            notes = decode_syrinx_notes(val)
            i += 2
        elif key == "channel:":
            channel = _safe_int(val, 1)
            i += 2
        elif key in ("lefttime_abs:", "lefttimesec:"):
            start_time = _safe_float(val, 0.0)
            i += 2
        elif key in ("righttime_abs:", "righttimesec:"):
            end_time = _safe_float(val, 0.0)
            i += 2
        elif key in ("topfreq:", "topfreqhz:"):
            high_freq = _safe_float(val, 0.0)
            i += 2
        elif key in ("bottomfreq:", "bottomfreqhz:"):
            low_freq = _safe_float(val, 0.0)
            i += 2
        elif key.endswith(":"):
            extra[key[:-1]] = val
            i += 2
        else:
            i += 1

    if end_time <= start_time:
        return None, last_filepath

    if not soundfile:
        soundfile = last_filepath

    rec = AnnotationRecord(
        id=str(uuid.uuid4()),
        soundfile=soundfile,
        channel=channel,
        start_time_sec=start_time,
        end_time_sec=end_time,
        low_freq_hz=low_freq,
        high_freq_hz=high_freq,
        comment=comment,
        notes=notes,
        extra_fields=extra,
        modified=False,
    )
    return rec, last_filepath


def parse_syrinx_log(content: str) -> AnnotationDocument:
    """Parse classic Syrinx tab-delimited annotation logs (.txt/.log) faithfully."""
    raw_lines = content.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    key_columns: list[str] = list(REQUIRED_FIELD_ORDER)
    add_time = False
    add_date = False

    # First pass: locate fieldkey: line anywhere in the file (matches ImportTextLogfileFieldKey)
    for line in raw_lines:
        if not line:
            continue
        parts = line.split("\t")
        if parts and parts[0].strip().lower() == "fieldkey:":
            key_columns = [p.strip() for p in parts[1:]]
            add_time = FN_STARTTIME_ABS in key_columns
            add_date = FN_STARTDATE_ABS in key_columns
            break

    annotations: list[AnnotationRecord] = []
    non_annotation_lines: list[str] = []
    last_filepath = ""

    for line in raw_lines:
        if not line:
            continue
        parts = line.split("\t")
        first = parts[0].strip()
        if first.lower() == "fieldkey:":
            key_columns = [p.strip() for p in parts[1:]]
            continue
        if first == "an:":
            rec, last_filepath = _parse_an_line(parts, key_columns, last_filepath)
            if rec is not None:
                annotations.append(rec)
            else:
                non_annotation_lines.append(line)
            continue

        # Attempt legacy key:/value pair line
        rec, last_filepath = _parse_legacy_pair_line(parts, last_filepath)
        if rec is not None:
            annotations.append(rec)
        else:
            non_annotation_lines.append(line)

    return AnnotationDocument(
        annotations=annotations,
        non_annotation_lines=non_annotation_lines,
        add_time=add_time,
        add_date=add_date,
    )


def serialize_syrinx_log(
    annotations: list[AnnotationRecord],
    non_annotation_lines: list[str] | None = None,
    add_time: bool = False,
    add_date: bool = False,
    start_datetime: datetime | None = None,
) -> str:
    """Serialize annotations into classic Syrinx fieldkey:/an: format (AnnotationList::ExportTextLogfile)."""
    lines: list[str] = []
    for header_line in non_annotation_lines or []:
        if header_line.strip():
            lines.append(header_line)

    # Collect union of extra field names in insertion order across annotations
    extra_keys: list[str] = []
    seen_extra: set[str] = set()
    for ann in annotations:
        for k in ann.extra_fields:
            if k not in _KNOWN_REQUIRED and k not in seen_extra:
                seen_extra.add(k)
                extra_keys.append(k)

    key_parts = [
        "fieldkey:",
        FN_FILEPATH,
        FN_CHANNEL,
        FN_STARTTIME,
        FN_ENDTIME,
        FN_HIGHFREQ,
        FN_LOWFREQ,
    ]
    if add_time:
        key_parts.append(FN_STARTTIME_ABS)
    if add_date:
        key_parts.append(FN_STARTDATE_ABS)
    key_parts.extend([FN_COMMENT, FN_NOTES])
    key_parts.extend(extra_keys)
    lines.append("\t".join(key_parts))

    base_dt = start_datetime or datetime(2000, 1, 1, 0, 0, 0)
    for ann in annotations:
        rel_dt = base_dt + timedelta(seconds=int(ann.start_time_sec))
        row = [
            "an:",
            _extract_filename(ann.soundfile),
            f"{int(ann.channel)}",
            f"{ann.start_time_sec:.6f}",
            f"{ann.end_time_sec:.6f}",
            f"{ann.high_freq_hz:.6f}",
            f"{ann.low_freq_hz:.6f}",
        ]
        if add_time:
            row.append(rel_dt.strftime("%H:%M:%S"))
        if add_date:
            row.append(rel_dt.strftime("%Y-%m-%d"))
        row.append(ann.comment.replace("\t", " "))
        row.append(encode_syrinx_notes(ann.notes).replace("\t", " "))
        for k in extra_keys:
            val = ann.extra_fields.get(k, "")
            row.append(str(val).replace("\t", " "))
        lines.append("\t".join(row))

    return "\n".join(lines) + "\n"


def filter_annotations(
    annotations: list[AnnotationRecord],
    flt: AnnotationFilter | None = None,
    filename: str | None = None,
) -> list[AnnotationRecord]:
    """Filter annotations matching classic CAnnotationList::FilterOK semantics."""
    target_name = _extract_filename(filename).lower() if filename else None
    result: list[AnnotationRecord] = []
    for ann in annotations:
        if target_name and _extract_filename(ann.soundfile).lower() != target_name:
            continue
        if flt is not None:
            if flt.channels and ann.channel not in flt.channels:
                continue
            if flt.exclude_empty and not ann.comment.strip():
                continue
            if flt.include_filter and flt.include_text not in ann.comment:
                continue
            if flt.exclude_filter and flt.exclude_text and flt.exclude_text in ann.comment:
                continue
        result.append(ann)
    return result


def find_next_annotation(
    annotations: list[AnnotationRecord],
    current_time_sec: float,
) -> AnnotationRecord | None:
    candidates = [a for a in annotations if a.start_time_sec > current_time_sec]
    return min(candidates, key=lambda a: a.start_time_sec) if candidates else None


def find_prev_annotation(
    annotations: list[AnnotationRecord],
    current_time_sec: float,
) -> AnnotationRecord | None:
    candidates = [a for a in annotations if a.start_time_sec < current_time_sec]
    return max(candidates, key=lambda a: a.start_time_sec) if candidates else None


def find_nearest_annotation(
    annotations: list[AnnotationRecord],
    current_time_sec: float,
) -> AnnotationRecord | None:
    if not annotations:
        return None
    return min(annotations, key=lambda a: abs(a.start_time_sec - current_time_sec))
