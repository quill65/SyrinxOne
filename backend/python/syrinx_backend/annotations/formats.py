from __future__ import annotations

import csv
import io
import json
import uuid
from pathlib import Path

from syrinx_backend.annotations.syrinx_io import parse_syrinx_log, serialize_syrinx_log
from syrinx_backend.models import AnnotationDocument, AnnotationFormat, AnnotationRecord


def infer_annotation_format(uri: str, content: str = "") -> AnnotationFormat:
    ext = Path(uri).suffix.lower()
    if ext == ".json":
        return "json"
    if ext == ".csv":
        return "csv"
    first_line = content.splitlines()[0] if content.splitlines() else ""
    if "Begin Time (s)" in first_line and "End Time (s)" in first_line:
        return "raven_txt"
    return "syrinx_txt"


def parse_raven_table(content: str, default_soundfile: str = "") -> AnnotationDocument:
    """Import Cornell Raven selection table (.txt TSV)."""
    reader = csv.DictReader(io.StringIO(content), delimiter="\t")
    annotations: list[AnnotationRecord] = []
    reserved = {
        "Selection",
        "View",
        "Channel",
        "Begin Time (s)",
        "End Time (s)",
        "Low Freq (Hz)",
        "High Freq (Hz)",
        "Begin File",
        "Soundfile",
        "Annotation",
        "Comment",
        "Notes",
    }
    for row in reader:
        if not row:
            continue
        try:
            start_t = float(row.get("Begin Time (s)") or 0.0)
            end_t = float(row.get("End Time (s)") or 0.0)
        except ValueError:
            continue
        if end_t <= start_t:
            continue
        low_f = float(row.get("Low Freq (Hz)") or 0.0)
        high_f = float(row.get("High Freq (Hz)") or 0.0)
        channel = int(float(row.get("Channel") or 1))
        soundfile = (
            row.get("Begin File")
            or row.get("Soundfile")
            or default_soundfile
            or ""
        )
        comment = row.get("Annotation") or row.get("Comment") or ""
        notes = row.get("Notes") or ""
        extra = {
            k: v
            for k, v in row.items()
            if k and k not in reserved and v is not None and v != ""
        }
        annotations.append(
            AnnotationRecord(
                id=str(uuid.uuid4()),
                soundfile=soundfile,
                channel=channel,
                start_time_sec=start_t,
                end_time_sec=end_t,
                low_freq_hz=low_f,
                high_freq_hz=high_f,
                comment=comment,
                notes=notes,
                extra_fields=extra,
            )
        )
    return AnnotationDocument(annotations=annotations)


def serialize_raven_table(annotations: list[AnnotationRecord]) -> str:
    extra_keys: list[str] = []
    seen: set[str] = set()
    for a in annotations:
        for k in a.extra_fields:
            if k not in seen:
                seen.add(k)
                extra_keys.append(k)

    fieldnames = [
        "Selection",
        "View",
        "Channel",
        "Begin Time (s)",
        "End Time (s)",
        "Low Freq (Hz)",
        "High Freq (Hz)",
        "Begin File",
        "Annotation",
        "Notes",
        *extra_keys,
    ]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, delimiter="\t", lineterminator="\n")
    writer.writeheader()
    for idx, ann in enumerate(annotations, start=1):
        row: dict[str, object] = {
            "Selection": idx,
            "View": "Spectrogram 1",
            "Channel": ann.channel,
            "Begin Time (s)": f"{ann.start_time_sec:.6f}",
            "End Time (s)": f"{ann.end_time_sec:.6f}",
            "Low Freq (Hz)": f"{ann.low_freq_hz:.2f}",
            "High Freq (Hz)": f"{ann.high_freq_hz:.2f}",
            "Begin File": ann.soundfile,
            "Annotation": ann.comment,
            "Notes": ann.notes.replace("\n", " "),
        }
        for k in extra_keys:
            row[k] = ann.extra_fields.get(k, "")
        writer.writerow(row)
    return buf.getvalue()


def parse_csv_annotations(content: str) -> AnnotationDocument:
    reader = csv.DictReader(io.StringIO(content))
    standard = {
        "id",
        "soundfile",
        "channel",
        "start_time_sec",
        "end_time_sec",
        "low_freq_hz",
        "high_freq_hz",
        "comment",
        "notes",
    }
    annotations: list[AnnotationRecord] = []
    for row in reader:
        start_t = float(row.get("start_time_sec") or 0.0)
        end_t = float(row.get("end_time_sec") or 0.0)
        if end_t <= start_t:
            continue
        extra = {k: v for k, v in row.items() if k and k not in standard and v != ""}
        annotations.append(
            AnnotationRecord(
                id=row.get("id") or str(uuid.uuid4()),
                soundfile=row.get("soundfile") or "",
                channel=int(float(row.get("channel") or 1)),
                start_time_sec=start_t,
                end_time_sec=end_t,
                low_freq_hz=float(row.get("low_freq_hz") or 0.0),
                high_freq_hz=float(row.get("high_freq_hz") or 0.0),
                comment=row.get("comment") or "",
                notes=row.get("notes") or "",
                extra_fields=extra,
            )
        )
    return AnnotationDocument(annotations=annotations)


def serialize_csv_annotations(annotations: list[AnnotationRecord]) -> str:
    extra_keys: list[str] = []
    seen: set[str] = set()
    for a in annotations:
        for k in a.extra_fields:
            if k not in seen:
                seen.add(k)
                extra_keys.append(k)
    fieldnames = [
        "id",
        "soundfile",
        "channel",
        "start_time_sec",
        "end_time_sec",
        "low_freq_hz",
        "high_freq_hz",
        "comment",
        "notes",
        *extra_keys,
    ]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    for ann in annotations:
        row: dict[str, object] = {
            "id": ann.id,
            "soundfile": ann.soundfile,
            "channel": ann.channel,
            "start_time_sec": f"{ann.start_time_sec:.6f}",
            "end_time_sec": f"{ann.end_time_sec:.6f}",
            "low_freq_hz": f"{ann.low_freq_hz:.2f}",
            "high_freq_hz": f"{ann.high_freq_hz:.2f}",
            "comment": ann.comment,
            "notes": ann.notes,
        }
        for k in extra_keys:
            row[k] = ann.extra_fields.get(k, "")
        writer.writerow(row)
    return buf.getvalue()


def import_annotations(content: str, fmt: AnnotationFormat) -> AnnotationDocument:
    if fmt == "syrinx_txt":
        return parse_syrinx_log(content)
    if fmt == "raven_txt":
        return parse_raven_table(content)
    if fmt == "csv":
        return parse_csv_annotations(content)
    if fmt == "json":
        payload = json.loads(content)
        if isinstance(payload, list):
            return AnnotationDocument(
                annotations=[AnnotationRecord.model_validate(item) for item in payload]
            )
        return AnnotationDocument.model_validate(payload)
    raise ValueError(f"Unsupported annotation format: {fmt}")


def export_annotations(
    annotations: list[AnnotationRecord],
    fmt: AnnotationFormat,
    non_annotation_lines: list[str] | None = None,
    add_time: bool = False,
    add_date: bool = False,
) -> str:
    if fmt == "syrinx_txt":
        return serialize_syrinx_log(
            annotations,
            non_annotation_lines=non_annotation_lines,
            add_time=add_time,
            add_date=add_date,
        )
    if fmt == "raven_txt":
        return serialize_raven_table(annotations)
    if fmt == "csv":
        return serialize_csv_annotations(annotations)
    if fmt == "json":
        doc = AnnotationDocument(
            annotations=annotations,
            non_annotation_lines=non_annotation_lines or [],
            add_time=add_time,
            add_date=add_date,
        )
        return doc.model_dump_json(indent=2, by_alias=True)
    raise ValueError(f"Unsupported annotation format: {fmt}")
