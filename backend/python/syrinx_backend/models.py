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


DataSourceKind = Literal["local_fs", "s3", "gcs", "gdrive", "http", "custom"]


class DataSourceInfo(CamelModel):
    source_id: str
    display_name: str
    kind: DataSourceKind
    read_only: bool = False
    root_path: str | None = None
    description: str | None = None
    anonymous: bool = False
    removable: bool = False


class CreateSourceRequest(CamelModel):
    kind: Literal["s3", "gcs", "gdrive", "http"]
    source_id: str | None = None
    display_name: str | None = None
    bucket: str | None = None
    prefix: str = ""
    region: str | None = None
    endpoint_url: str | None = None
    anonymous: bool = True
    folder_or_url: str | None = None
    api_key: str | None = None
    oauth_token: str | None = None
    read_only: bool = True


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
    window_start_sec: float = 0.0
    total_duration_sec: float | None = None
    is_partial_window: bool = False


class OpenAudioRequest(CamelModel):
    source_id: str
    uri: str
    start_sec: float = 0.0
    max_duration_sec: float | None = None


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


class MeasureSelectionRequest(CamelModel):
    channel: int = 0
    start_sec: float
    end_sec: float
    low_freq_hz: float
    high_freq_hz: float
    fft_size: int = 512
    hop_size: int = 128


class SelectionMeasurements(CamelModel):
    duration_sec: float
    low_freq_hz: float
    high_freq_hz: float
    bandwidth_hz: float
    peak_freq_hz: float
    centroid_freq_hz: float
    q5_freq_hz: float
    q50_freq_hz: float
    q95_freq_hz: float
    bw90_hz: float
    time_of_peak_sec: float
    rms_db: float
    peak_db: float
    noise_floor_db: float
    snr_db: float


class AiChatMessage(CamelModel):
    role: Literal["user", "assistant"]
    content: str


class AiSelectionRegion(CamelModel):
    start_sec: float
    end_sec: float
    low_freq_hz: float
    high_freq_hz: float


class AiViewRange(CamelModel):
    start_sec: float
    end_sec: float
    min_freq_hz: float
    max_freq_hz: float


class AiSpecParams(CamelModel):
    channel: int = 0
    fft_size: int = 512
    hop_size: int = 128
    window_type: WindowType = "hann"
    dsp_mode: DspMode = "modern_stft"
    pop_filter: bool = False
    classic_gain: float = 1.0
    classic_white_thresh: float = 0.0


class AiDisplaySettings(CamelModel):
    colormap: ColormapName = "grayscale_bow"
    gain_db: float = 0.0
    threshold_db: float = -80.0
    dynamic_range_db: float = 75.0


class SuggestedDetectParams(CamelModel):
    min_freq_hz: float
    max_freq_hz: float
    trigger_threshold_db: float
    min_sound_dur_sec: float
    max_silence_gap_sec: float
    comment: str = "AI-Suggested"


class AiAnalyzeRequest(CamelModel):
    file_id: str
    channel: int = 0
    prompt: str
    history: list[AiChatMessage] = Field(default_factory=list)
    selection: AiSelectionRegion | None = None
    view_range: AiViewRange | None = None
    spec_params: AiSpecParams | None = None
    display_settings: AiDisplaySettings | None = None
    annotations: list[AnnotationRecord] = Field(default_factory=list)
    include_spectrogram_image: bool = True
    api_key: str | None = None
    model: str = "gemini-2.5-flash"


class AiAnalyzeResponse(CamelModel):
    answer: str
    model_used: str
    measurements: SelectionMeasurements | None = None
    suggested_detect_params: SuggestedDetectParams | None = None
    suggested_labels: list[str] = Field(default_factory=list)
