"""Pluggable data source adapters for local disk and heterogeneous cloud backends."""

from .base import AudioDataSource, SourceRegistry, classify_entry_kind
from .cloud_adapters import (
    GCSAudioDataSource,
    HttpUrlAudioDataSource,
    S3AudioDataSource,
)
from .local_fs import LocalFilesystemSource

__all__ = [
    "AudioDataSource",
    "SourceRegistry",
    "LocalFilesystemSource",
    "S3AudioDataSource",
    "GCSAudioDataSource",
    "HttpUrlAudioDataSource",
    "classify_entry_kind",
]
