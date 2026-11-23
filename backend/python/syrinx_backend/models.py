from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


def _to_camel(value: str) -> str:
    parts = value.split("_")
    return parts[0] + "".join(p.capitalize() for p in parts[1:])


class CamelModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=_to_camel,
        populate_by_name=True,
    )


WindowType = Literal["blackman", "hamming", "hann", "rect"]
DspMode = Literal["modern_stft", "classic_syrinx"]
ColormapName = Literal[
    "grayscale_bow", "grayscale_wob", "viridis", "magma", "inferno", "turbo"
]
AnnotationFormat = Literal["syrinx_txt", "raven_txt", "csv", "json"]


class DataSourceInfo(CamelModel):
    source_id: str
    display_name: str
    kind: Literal["local_fs", "s3", "gcs", "http", "custom"]
    read_only: bool = False
    root_path: str | None = None


class BrowseEntry(CamelModel):
    name: str
    uri: str
    is_dir: bool
    size_bytes: int | None = None
    kind: Literal["audio", "annotation", "other"] | None = None


class AudioFileInfo(CamelModel):
    file_id: str
    source_id: str
    uri: str
    name: str
    sample_rate: int
    num_channels: int
    num_frames: int
    duration_sec: float
    format: str
    subtype: str
    start_date_time_iso: str | None = None


class OpenAudioRequest(CamelModel):
    source_id: str
    uri: str


class WaveformOverview(CamelModel):
    file_id: str
    channel: int
    num_bins: int
    start_sec: float
    end_sec: float
    min_values: list[float]
    max_values: list[float]


class ClipSaveRequest(CamelModel):
    file_id: str
    start_sec: float
    end_sec: float
    channels: list[int] | None = None
    target_source_id: str = "local"
    target_directory: str = ""
    name_template: str = "%f_%c_clip"
    comment: str = ""
    open_in_window: bool = False


class ClipSaveResponse(CamelModel):
    saved_uri: str
    saved_name: str
    file_info: AudioFileInfo | None = None


class AnnotationRecord(CamelModel):
    id: str
    soundfile: str
    channel: int = 1
    start_time_sec: float
    end_time_sec: float
    low_freq_hz: float
    high_freq_hz: float
    comment: str = ""
    notes: str = ""
    extra_fields: dict[str, str | float | int] = Field(default_factory=dict)
    modified: bool = False


class AnnotationDocument(CamelModel):
    annotations: list[AnnotationRecord] = Field(default_factory=list)
    non_annotation_lines: list[str] = Field(default_factory=list)
    source_uri: str | None = None
    add_time: bool = False
    add_date: bool = False


class AnnotationFilter(CamelModel):
    include_filter: bool = False
    include_text: str = ""
    exclude_filter: bool = False
    exclude_text: str = ""
    exclude_empty: bool = False
    channels: list[int] = Field(default_factory=list)


class ImportAnnotationsTextRequest(CamelModel):
    content: str
    format: AnnotationFormat = "syrinx_txt"


class ImportAnnotationsSourceRequest(CamelModel):
    source_id: str
    uri: str
    format: AnnotationFormat | None = None


class ExportAnnotationsTextRequest(CamelModel):
    annotations: list[AnnotationRecord]
    format: AnnotationFormat = "syrinx_txt"
    non_annotation_lines: list[str] = Field(default_factory=list)
    add_time: bool = False
    add_date: bool = False


class SaveAnnotationsSourceRequest(CamelModel):
    source_id: str
    uri: str
    annotations: list[AnnotationRecord]
    format: AnnotationFormat = "syrinx_txt"
    non_annotation_lines: list[str] = Field(default_factory=list)


class DetectSoundsRequest(CamelModel):
    channel: int = 0
    fft_size: int = 512
    hop_size: int = 128
    min_freq_hz: float = 1000.0
    max_freq_hz: float = 10000.0
    trigger_threshold_db: float = 12.0
    min_trigger_dur_sec: float = 0.015
    min_sound_dur_sec: float = 0.08
    max_silence_gap_sec: float = 0.25
    pre_buffer_sec: float = 0.03
    post_buffer_sec: float = 0.05
    pop_filter: bool = True
    start_sec: float | None = None
    end_sec: float | None = None
    comment: str = "Detected"
    save_clips: bool = False
    target_source_id: str = "local"
    target_directory: str = ""
    clip_name_template: str = "%f_det_%c"


class DetectSoundsResponse(CamelModel):
    detections: list[AnnotationRecord]
    noise_floor_db: float
    saved_clips: list[str] = Field(default_factory=list)
