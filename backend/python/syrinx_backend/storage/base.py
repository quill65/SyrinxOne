from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Literal

from syrinx_backend.models import BrowseEntry, DataSourceInfo, DataSourceKind

AUDIO_EXTENSIONS = {
    ".wav",
    ".wave",
    ".flac",
    ".aif",
    ".aiff",
    ".aifc",
    ".ogg",
    ".opus",
    ".mp3",
    ".m4a",
    ".aac",
    ".webm",
}
ANNOTATION_EXTENSIONS = {".txt", ".log", ".csv", ".json", ".tsv"}


def classify_entry_kind(name: str) -> Literal["audio", "annotation", "other"]:
    ext = Path(name).suffix.lower()
    if ext in AUDIO_EXTENSIONS:
        return "audio"
    if ext in ANNOTATION_EXTENSIONS:
        return "annotation"
    return "other"


class AudioDataSource(ABC):
    """Abstract interface for any local, cloud bucket, or custom lab audio/annotation repository."""

    source_id: str
    display_name: str
    kind: DataSourceKind
    read_only: bool = False
    description: str | None = None
    anonymous: bool = False
    removable: bool = False

    def info(self) -> DataSourceInfo:
        return DataSourceInfo(
            source_id=self.source_id,
            display_name=self.display_name,
            kind=self.kind,
            read_only=self.read_only,
            root_path=getattr(self, "root_path_str", None),
            description=self.description,
            anonymous=self.anonymous,
            removable=self.removable,
        )

    @abstractmethod
    async def list_items(self, prefix: str = "", limit: int = 250) -> list[BrowseEntry]:
        """Browse files and virtual directories under prefix."""

    @abstractmethod
    async def read_bytes(self, uri: str) -> bytes:
        """Read full bytes of the referenced audio or annotation object."""

    async def read_byte_range(
        self, uri: str, start: int, end_inclusive: int
    ) -> tuple[bytes, int | None]:
        """Read a byte slice [start, end_inclusive] and return (data, total_size_or_None)."""
        full = await self.read_bytes(uri)
        return full[start : end_inclusive + 1], len(full)

    async def resolve_local_path(self, uri: str) -> Path | None:
        """Return direct filesystem Path when local (zero-copy optimization), else None."""
        return None

    @abstractmethod
    async def write_bytes(self, uri: str, data: bytes) -> str:
        """Persist bytes (clip WAV or annotation file) and return canonical saved URI."""


class SourceRegistry:
    """Registry holding all active storage adapters (local, S3, GCS, Drive, HTTP, custom)."""

    def __init__(self) -> None:
        self._sources: dict[str, AudioDataSource] = {}

    def register(self, source: AudioDataSource) -> None:
        self._sources[source.source_id] = source

    def unregister(self, source_id: str) -> None:
        if source_id not in self._sources:
            raise KeyError(f"Unknown data source: '{source_id}'")
        source = self._sources[source_id]
        if not source.removable:
            raise PermissionError(f"Built-in source '{source_id}' cannot be removed")
        del self._sources[source_id]

    def get(self, source_id: str) -> AudioDataSource:
        if source_id not in self._sources:
            raise KeyError(f"Unknown data source: '{source_id}'")
        return self._sources[source_id]

    def list_info(self) -> list[DataSourceInfo]:
        return [s.info() for s in self._sources.values()]
