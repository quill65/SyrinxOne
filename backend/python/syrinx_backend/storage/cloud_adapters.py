from __future__ import annotations

import importlib
from urllib.parse import urlparse

from syrinx_backend.models import BrowseEntry
from syrinx_backend.storage.base import AudioDataSource, SourceRegistry, classify_entry_kind


class S3AudioDataSource(AudioDataSource):
    """Amazon S3 / S3-compatible object storage adapter (requires optional `boto3`)."""

    kind = "s3"

    def __init__(
        self,
        source_id: str,
        bucket: str,
        prefix: str = "",
        display_name: str | None = None,
        endpoint_url: str | None = None,
        read_only: bool = False,
    ) -> None:
        self.source_id = source_id
        self.bucket = bucket
        self.base_prefix = prefix.strip("/")
        self.display_name = display_name or f"S3: {bucket}"
        self.endpoint_url = endpoint_url
        self.read_only = read_only
        self.root_path_str = f"s3://{bucket}/{self.base_prefix}".rstrip("/")

    def _client(self):
        try:
            import boto3  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError(
                "S3AudioDataSource requires the 'cloud' extra: `uv sync --extra cloud`"
            ) from exc
        return boto3.client("s3", endpoint_url=self.endpoint_url)

    def _key(self, uri: str) -> str:
        clean = uri.lstrip("/")
        if self.base_prefix and not clean.startswith(self.base_prefix):
            return f"{self.base_prefix}/{clean}".strip("/")
        return clean

    async def list_items(self, prefix: str = "") -> list[BrowseEntry]:
        s3 = self._client()
        full_prefix = self._key(prefix)
        if full_prefix and not full_prefix.endswith("/"):
            full_prefix += "/"
        resp = s3.list_objects_v2(Bucket=self.bucket, Prefix=full_prefix, Delimiter="/")
        entries: list[BrowseEntry] = []
        for cp in resp.get("CommonPrefixes", []):
            p = cp["Prefix"]
            name = p.rstrip("/").split("/")[-1]
            entries.append(BrowseEntry(name=name, uri=p, is_dir=True, kind="other"))
        for obj in resp.get("Contents", []):
            key = obj["Key"]
            if key == full_prefix:
                continue
            name = key.split("/")[-1]
            entries.append(
                BrowseEntry(
                    name=name,
                    uri=key,
                    is_dir=False,
                    size_bytes=int(obj.get("Size", 0)),
                    kind=classify_entry_kind(name),
                )
            )
        return entries

    async def read_bytes(self, uri: str) -> bytes:
        s3 = self._client()
        obj = s3.get_object(Bucket=self.bucket, Key=self._key(uri))
        return obj["Body"].read()

    async def write_bytes(self, uri: str, data: bytes) -> str:
        if self.read_only:
            raise PermissionError(f"Source '{self.source_id}' is read-only")
        s3 = self._client()
        key = self._key(uri)
        s3.put_object(Bucket=self.bucket, Key=key, Body=data)
        return key


class GCSAudioDataSource(AudioDataSource):
    """Google Cloud Storage adapter (requires optional `google-cloud-storage`)."""

    kind = "gcs"

    def __init__(
        self,
        source_id: str,
        bucket: str,
        prefix: str = "",
        display_name: str | None = None,
        read_only: bool = False,
    ) -> None:
        self.source_id = source_id
        self.bucket_name = bucket
        self.base_prefix = prefix.strip("/")
        self.display_name = display_name or f"GCS: {bucket}"
        self.read_only = read_only
        self.root_path_str = f"gs://{bucket}/{self.base_prefix}".rstrip("/")

    def _bucket(self):
        try:
            from google.cloud import storage  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError(
                "GCSAudioDataSource requires the 'cloud' extra: `uv sync --extra cloud`"
            ) from exc
        return storage.Client().bucket(self.bucket_name)

    def _key(self, uri: str) -> str:
        clean = uri.lstrip("/")
        if self.base_prefix and not clean.startswith(self.base_prefix):
            return f"{self.base_prefix}/{clean}".strip("/")
        return clean

    async def list_items(self, prefix: str = "") -> list[BrowseEntry]:
        bucket = self._bucket()
        full_prefix = self._key(prefix)
        if full_prefix and not full_prefix.endswith("/"):
            full_prefix += "/"
        iterator = bucket.list_blobs(prefix=full_prefix, delimiter="/")
        entries: list[BrowseEntry] = []
        for blob in iterator:
            if blob.name == full_prefix:
                continue
            name = blob.name.split("/")[-1]
            entries.append(
                BrowseEntry(
                    name=name,
                    uri=blob.name,
                    is_dir=False,
                    size_bytes=blob.size,
                    kind=classify_entry_kind(name),
                )
            )
        for p in sorted(iterator.prefixes):
            name = p.rstrip("/").split("/")[-1]
            entries.append(BrowseEntry(name=name, uri=p, is_dir=True, kind="other"))
        return entries

    async def read_bytes(self, uri: str) -> bytes:
        return self._bucket().blob(self._key(uri)).download_as_bytes()

    async def write_bytes(self, uri: str, data: bytes) -> str:
        if self.read_only:
            raise PermissionError(f"Source '{self.source_id}' is read-only")
        key = self._key(uri)
        self._bucket().blob(key).upload_from_string(data)
        return key


class HttpUrlAudioDataSource(AudioDataSource):
    """Read-only HTTP/HTTPS URL adapter for presigned URLs or public acoustic archives."""

    kind = "http"

    def __init__(
        self,
        source_id: str = "http",
        display_name: str = "HTTP / Presigned URLs",
        catalog_urls: list[str] | None = None,
    ) -> None:
        self.source_id = source_id
        self.display_name = display_name
        self.read_only = True
        self.catalog_urls = catalog_urls or []

    async def list_items(self, prefix: str = "") -> list[BrowseEntry]:
        entries: list[BrowseEntry] = []
        for url in self.catalog_urls:
            name = urlparse(url).path.rsplit("/", 1)[-1] or url
            if prefix and prefix.lower() not in name.lower():
                continue
            entries.append(
                BrowseEntry(
                    name=name,
                    uri=url,
                    is_dir=False,
                    kind=classify_entry_kind(name),
                )
            )
        return entries

    async def read_bytes(self, uri: str) -> bytes:
        try:
            import httpx
        except ImportError as exc:
            raise RuntimeError("HttpUrlAudioDataSource requires `httpx` installed") from exc
        async with httpx.AsyncClient(follow_redirects=True, timeout=60.0) as client:
            resp = await client.get(uri)
            resp.raise_for_status()
            return resp.content

    async def write_bytes(self, uri: str, data: bytes) -> str:
        raise PermissionError("HttpUrlAudioDataSource is read-only")


def load_custom_source_plugins(registry: SourceRegistry, plugin_specs: list[str]) -> None:
    """Load user-supplied Python data source factories (`module.path:register_sources`)."""
    for spec in plugin_specs:
        spec = spec.strip()
        if not spec or ":" not in spec:
            continue
        mod_name, fn_name = spec.split(":", 1)
        module = importlib.import_module(mod_name)
        factory = getattr(module, fn_name)
        factory(registry)
