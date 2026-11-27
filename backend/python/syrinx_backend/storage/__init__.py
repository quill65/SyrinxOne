"""Pluggable data source adapters for local disk and heterogeneous cloud backends."""

from .archive_adapters import (
    FigshareAudioDataSource,
    FreesoundAudioDataSource,
    HuggingFaceAudioDataSource,
    INaturalistAudioDataSource,
    RemoteArchiveAudioDataSource,
    XenoCantoAudioDataSource,
    ZenodoAudioDataSource,
)
from .base import AudioDataSource, SourceRegistry, classify_entry_kind
from .cloud_adapters import (
    GCSAudioDataSource,
    HttpUrlAudioDataSource,
    S3AudioDataSource,
)
from .discovery import (
    DiscoveryProvider,
    DiscoveryRegistry,
    build_default_discovery_registry,
    build_source_from_request,
    discover_online_sources,
    verify_audio_source,
)
from .local_fs import LocalFilesystemSource

__all__ = [
    "AudioDataSource",
    "SourceRegistry",
    "LocalFilesystemSource",
    "S3AudioDataSource",
    "GCSAudioDataSource",
    "HttpUrlAudioDataSource",
    "ZenodoAudioDataSource",
    "FigshareAudioDataSource",
    "HuggingFaceAudioDataSource",
    "RemoteArchiveAudioDataSource",
    "INaturalistAudioDataSource",
    "XenoCantoAudioDataSource",
    "FreesoundAudioDataSource",
    "DiscoveryProvider",
    "DiscoveryRegistry",
    "build_default_discovery_registry",
    "build_source_from_request",
    "discover_online_sources",
    "verify_audio_source",
    "classify_entry_kind",
]
