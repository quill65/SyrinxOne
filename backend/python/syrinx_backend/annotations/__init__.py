"""Classic Syrinx annotation log I/O and modern interchange formats (Raven, CSV, JSON)."""

from .formats import (
    export_annotations,
    import_annotations,
    infer_annotation_format,
)
from .syrinx_io import (
    filter_annotations,
    find_nearest_annotation,
    find_next_annotation,
    find_prev_annotation,
    parse_syrinx_log,
    serialize_syrinx_log,
)

__all__ = [
    "parse_syrinx_log",
    "serialize_syrinx_log",
    "filter_annotations",
    "find_prev_annotation",
    "find_next_annotation",
    "find_nearest_annotation",
    "import_annotations",
    "export_annotations",
    "infer_annotation_format",
]
