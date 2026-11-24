from __future__ import annotations

import os
from pathlib import Path

from syrinx_backend.models import BrowseEntry
from syrinx_backend.storage.base import AudioDataSource, classify_entry_kind


class LocalFilesystemSource(AudioDataSource):
    """Direct local filesystem adapter for Desktop sidecar or self-hosted server directories."""

    kind = "local_fs"

    def __init__(
        self,
        source_id: str = "local",
        display_name: str = "Local Filesystem",
        root_dir: str | Path | None = None,
        restrict_to_root: bool = False,
        read_only: bool = False,
    ) -> None:
        self.source_id = source_id
        self.display_name = display_name
        self.root_dir = Path(root_dir).resolve() if root_dir else None
        self.restrict_to_root = restrict_to_root
        self.read_only = read_only
        self.root_path_str = str(self.root_dir) if self.root_dir else None

    def _resolve(self, uri: str) -> Path:
        raw = Path(uri)
        if self.root_dir and not raw.is_absolute():
            candidate = (self.root_dir / raw).resolve()
        else:
            candidate = raw.resolve()

        if self.restrict_to_root and self.root_dir is not None:
            try:
                candidate.relative_to(self.root_dir)
            except ValueError as exc:
                raise PermissionError(
                    f"Path '{candidate}' escapes restricted root '{self.root_dir}'"
                ) from exc
        return candidate

    async def list_items(self, prefix: str = "", limit: int = 250) -> list[BrowseEntry]:
        if prefix:
            target = self._resolve(prefix)
        elif self.root_dir is not None:
            target = self.root_dir
        else:
            target = Path.home()

        if not target.exists() or not target.is_dir():
            return []

        entries: list[BrowseEntry] = []
        if target.parent != target:
            allow_parent = not (
                self.restrict_to_root and self.root_dir and target == self.root_dir
            )
            if allow_parent:
                entries.append(
                    BrowseEntry(
                        name="..",
                        uri=str(target.parent),
                        is_dir=True,
                        kind="other",
                    )
                )

        try:
            children = sorted(
                target.iterdir(),
                key=lambda p: (not p.is_dir(), p.name.lower()),
            )
        except PermissionError:
            return entries

        for child in children:
            if len(entries) >= max(1, limit):
                break
            if child.name.startswith("."):
                continue
            is_dir = child.is_dir()
            size = None
            if not is_dir:
                try:
                    size = child.stat().st_size
                except OSError:
                    size = None
            entries.append(
                BrowseEntry(
                    name=child.name,
                    uri=str(child),
                    is_dir=is_dir,
                    size_bytes=size,
                    kind="other" if is_dir else classify_entry_kind(child.name),
                )
            )
        return entries

    async def resolve_local_path(self, uri: str) -> Path | None:
        return self._resolve(uri)

    async def read_bytes(self, uri: str) -> bytes:
        path = self._resolve(uri)
        if not path.is_file():
            raise FileNotFoundError(f"File not found: {path}")
        return path.read_bytes()

    async def write_bytes(self, uri: str, data: bytes) -> str:
        if self.read_only:
            raise PermissionError(f"Source '{self.source_id}' is read-only")
        path = self._resolve(uri)
        os.makedirs(path.parent, exist_ok=True)
        path.write_bytes(data)
        return str(path)
