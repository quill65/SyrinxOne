from __future__ import annotations

import html
import importlib
import re
import xml.etree.ElementTree as ET
from urllib.parse import quote, urlparse

import httpx

from syrinx_backend.models import BrowseEntry
from syrinx_backend.storage.base import AudioDataSource, SourceRegistry, classify_entry_kind


def _parse_content_range_total(header_val: str | None) -> int | None:
    if not header_val or "/" not in header_val:
        return None
    total_str = header_val.rsplit("/", 1)[-1].strip()
    return int(total_str) if total_str.isdigit() else None


def parse_google_drive_id(url_or_id: str) -> tuple[str, bool]:
    """Extract (drive_id, is_folder) from a Google Drive share URL or raw ID."""
    text = url_or_id.strip()
    folder_match = re.search(r"/folders/([a-zA-Z0-9_-]{10,})", text)
    if folder_match:
        return folder_match.group(1), True

    file_match = re.search(r"/file/d/([a-zA-Z0-9_-]{10,})", text)
    if file_match:
        return file_match.group(1), False

    id_param = re.search(r"[?&]id=([a-zA-Z0-9_-]{10,})", text)
    if id_param:
        is_folder = "folder" in text.lower()
        return id_param.group(1), is_folder

    return text, False


class S3AudioDataSource(AudioDataSource):
    """Amazon S3 / AWS Open Data / S3-compatible object storage adapter.

    When `anonymous=True` (default for public acoustic archives like MBARI Pacific Ocean Sound),
    uses standard S3 HTTPS REST APIs via `httpx` with zero AWS credentials or boto3 required.
    When `anonymous=False`, uses `boto3` for authenticated private buckets.
    """

    kind = "s3"

    def __init__(
        self,
        source_id: str,
        bucket: str,
        prefix: str = "",
        display_name: str | None = None,
        region: str | None = None,
        endpoint_url: str | None = None,
        read_only: bool = False,
        anonymous: bool = False,
        description: str | None = None,
        removable: bool = False,
    ) -> None:
        self.source_id = source_id
        self.bucket = bucket.strip()
        self.base_prefix = prefix.strip("/")
        self.display_name = display_name or f"S3: {self.bucket}"
        self.region = region.strip() if region else None
        self.endpoint_url = endpoint_url.rstrip("/") if endpoint_url else None
        self.read_only = read_only or anonymous
        self.anonymous = anonymous
        self.description = description
        self.removable = removable
        self.root_path_str = self.base_prefix

    def _public_base_url(self) -> str:
        if self.endpoint_url:
            return f"{self.endpoint_url}/{self.bucket}"
        if self.region:
            return f"https://{self.bucket}.s3.{self.region}.amazonaws.com"
        return f"https://{self.bucket}.s3.amazonaws.com"

    def _client(self):
        try:
            import boto3  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError(
                "Authenticated S3AudioDataSource requires the 'cloud' extra: `uv sync --extra cloud`"
            ) from exc
        return boto3.client("s3", region_name=self.region, endpoint_url=self.endpoint_url)

    def _key(self, uri: str) -> str:
        clean = uri.strip()
        if clean.startswith(f"s3://{self.bucket}/"):
            clean = clean[len(f"s3://{self.bucket}/") :]
        clean = clean.lstrip("/")
        if self.base_prefix and not clean.startswith(self.base_prefix):
            return f"{self.base_prefix}/{clean}".strip("/")
        return clean

    async def _list_public_http(self, full_prefix: str, limit: int) -> list[BrowseEntry]:
        params = {
            "list-type": "2",
            "delimiter": "/",
            "prefix": full_prefix,
            "max-keys": str(max(1, min(1000, limit))),
        }
        async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
            resp = await client.get(self._public_base_url(), params=params)
            resp.raise_for_status()
            xml_text = resp.text

        root = ET.fromstring(xml_text)
        ns_match = re.match(r"(\{[^}]+\})", root.tag)
        ns = ns_match.group(1) if ns_match else ""

        entries: list[BrowseEntry] = []
        if full_prefix:
            trimmed = full_prefix.rstrip("/")
            parent = trimmed.rsplit("/", 1)[0] + "/" if "/" in trimmed else ""
            if not self.base_prefix or parent.startswith(self.base_prefix):
                entries.append(BrowseEntry(name="..", uri=parent, is_dir=True, kind="other"))

        for cp in root.findall(f"{ns}CommonPrefixes"):
            pref_el = cp.find(f"{ns}Prefix")
            if pref_el is None or not pref_el.text:
                continue
            p = pref_el.text
            name = p.rstrip("/").split("/")[-1]
            entries.append(BrowseEntry(name=name, uri=p, is_dir=True, kind="other"))

        for content in root.findall(f"{ns}Contents"):
            key_el = content.find(f"{ns}Key")
            if key_el is None or not key_el.text:
                continue
            key = key_el.text
            if key == full_prefix or key.endswith("/"):
                continue
            size_el = content.find(f"{ns}Size")
            size_val = int(size_el.text) if (size_el is not None and size_el.text) else None
            name = key.split("/")[-1]
            entries.append(
                BrowseEntry(
                    name=name,
                    uri=key,
                    is_dir=False,
                    size_bytes=size_val,
                    kind=classify_entry_kind(name),
                )
            )
        return entries

    async def list_items(self, prefix: str = "", limit: int = 250) -> list[BrowseEntry]:
        full_prefix = self._key(prefix)
        if full_prefix and not full_prefix.endswith("/"):
            full_prefix += "/"

        if self.anonymous:
            return await self._list_public_http(full_prefix, limit)

        s3 = self._client()
        resp = s3.list_objects_v2(
            Bucket=self.bucket,
            Prefix=full_prefix,
            Delimiter="/",
            MaxKeys=max(1, min(1000, limit)),
        )
        entries: list[BrowseEntry] = []
        if full_prefix:
            trimmed = full_prefix.rstrip("/")
            parent = trimmed.rsplit("/", 1)[0] + "/" if "/" in trimmed else ""
            entries.append(BrowseEntry(name="..", uri=parent, is_dir=True, kind="other"))
        for cp in resp.get("CommonPrefixes", []):
            p = cp["Prefix"]
            name = p.rstrip("/").split("/")[-1]
            entries.append(BrowseEntry(name=name, uri=p, is_dir=True, kind="other"))
        for obj in resp.get("Contents", []):
            key = obj["Key"]
            if key == full_prefix or key.endswith("/"):
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
        key = self._key(uri)
        if self.anonymous:
            url = f"{self._public_base_url()}/{quote(key, safe='/')}"
            async with httpx.AsyncClient(follow_redirects=True, timeout=120.0) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                return resp.content

        s3 = self._client()
        obj = s3.get_object(Bucket=self.bucket, Key=key)
        return obj["Body"].read()

    async def read_byte_range(
        self, uri: str, start: int, end_inclusive: int
    ) -> tuple[bytes, int | None]:
        key = self._key(uri)
        if self.anonymous:
            url = f"{self._public_base_url()}/{quote(key, safe='/')}"
            headers = {"Range": f"bytes={start}-{end_inclusive}"}
            async with httpx.AsyncClient(follow_redirects=True, timeout=60.0) as client:
                resp = await client.get(url, headers=headers)
                resp.raise_for_status()
                total = _parse_content_range_total(resp.headers.get("Content-Range"))
                if total is None and resp.status_code == 200:
                    total = len(resp.content)
                    return resp.content[start : end_inclusive + 1], total
                return resp.content, total

        s3 = self._client()
        obj = s3.get_object(Bucket=self.bucket, Key=key, Range=f"bytes={start}-{end_inclusive}")
        total = _parse_content_range_total(obj.get("ContentRange"))
        return obj["Body"].read(), total

    async def write_bytes(self, uri: str, data: bytes) -> str:
        if self.read_only:
            raise PermissionError(f"Source '{self.source_id}' is read-only")
        s3 = self._client()
        key = self._key(uri)
        s3.put_object(Bucket=self.bucket, Key=key, Body=data)
        return key


class GCSAudioDataSource(AudioDataSource):
    """Google Cloud Storage adapter supporting both anonymous public archives and private buckets.

    When `anonymous=True` (e.g. NOAA NCEI `gs://noaa-passive-bioacoustic`), uses Google's
    public GCS JSON REST API via `httpx` with zero GCP credentials required.
    """

    kind = "gcs"

    def __init__(
        self,
        source_id: str,
        bucket: str,
        prefix: str = "",
        display_name: str | None = None,
        read_only: bool = False,
        anonymous: bool = False,
        description: str | None = None,
        removable: bool = False,
    ) -> None:
        self.source_id = source_id
        self.bucket_name = bucket.strip()
        self.base_prefix = prefix.strip("/")
        self.display_name = display_name or f"GCS: {self.bucket_name}"
        self.read_only = read_only or anonymous
        self.anonymous = anonymous
        self.description = description
        self.removable = removable
        self.root_path_str = self.base_prefix

    def _bucket(self):
        try:
            from google.cloud import storage  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError(
                "Authenticated GCSAudioDataSource requires the 'cloud' extra: `uv sync --extra cloud`"
            ) from exc
        return storage.Client().bucket(self.bucket_name)

    def _key(self, uri: str) -> str:
        clean = uri.strip()
        if clean.startswith(f"gs://{self.bucket_name}/"):
            clean = clean[len(f"gs://{self.bucket_name}/") :]
        clean = clean.lstrip("/")
        if self.base_prefix and not clean.startswith(self.base_prefix):
            return f"{self.base_prefix}/{clean}".strip("/")
        return clean

    async def _list_public_http(self, full_prefix: str, limit: int) -> list[BrowseEntry]:
        url = f"https://storage.googleapis.com/storage/v1/b/{quote(self.bucket_name, safe='')}/o"
        params = {
            "delimiter": "/",
            "prefix": full_prefix,
            "maxResults": str(max(1, min(1000, limit))),
        }
        async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()

        entries: list[BrowseEntry] = []
        if full_prefix:
            trimmed = full_prefix.rstrip("/")
            parent = trimmed.rsplit("/", 1)[0] + "/" if "/" in trimmed else ""
            if not self.base_prefix or parent.startswith(self.base_prefix):
                entries.append(BrowseEntry(name="..", uri=parent, is_dir=True, kind="other"))

        for p in sorted(data.get("prefixes", [])):
            name = p.rstrip("/").split("/")[-1]
            entries.append(BrowseEntry(name=name, uri=p, is_dir=True, kind="other"))

        for item in data.get("items", []):
            key = str(item.get("name", ""))
            if not key or key == full_prefix or key.endswith("/"):
                continue
            size_raw = item.get("size")
            size_val = int(size_raw) if size_raw is not None else None
            name = key.split("/")[-1]
            entries.append(
                BrowseEntry(
                    name=name,
                    uri=key,
                    is_dir=False,
                    size_bytes=size_val,
                    kind=classify_entry_kind(name),
                )
            )
        return entries

    async def list_items(self, prefix: str = "", limit: int = 250) -> list[BrowseEntry]:
        full_prefix = self._key(prefix)
        if full_prefix and not full_prefix.endswith("/"):
            full_prefix += "/"

        if self.anonymous:
            return await self._list_public_http(full_prefix, limit)

        bucket = self._bucket()
        iterator = bucket.list_blobs(prefix=full_prefix, delimiter="/", max_results=limit)
        entries: list[BrowseEntry] = []
        if full_prefix:
            trimmed = full_prefix.rstrip("/")
            parent = trimmed.rsplit("/", 1)[0] + "/" if "/" in trimmed else ""
            entries.append(BrowseEntry(name="..", uri=parent, is_dir=True, kind="other"))
        for blob in iterator:
            if blob.name == full_prefix or blob.name.endswith("/"):
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

    def _public_media_url(self, key: str) -> str:
        b = quote(self.bucket_name, safe="")
        obj = quote(key, safe="")
        return f"https://storage.googleapis.com/storage/v1/b/{b}/o/{obj}?alt=media"

    async def read_bytes(self, uri: str) -> bytes:
        key = self._key(uri)
        if self.anonymous:
            async with httpx.AsyncClient(follow_redirects=True, timeout=120.0) as client:
                resp = await client.get(self._public_media_url(key))
                resp.raise_for_status()
                return resp.content
        return self._bucket().blob(key).download_as_bytes()

    async def read_byte_range(
        self, uri: str, start: int, end_inclusive: int
    ) -> tuple[bytes, int | None]:
        key = self._key(uri)
        if self.anonymous:
            headers = {"Range": f"bytes={start}-{end_inclusive}"}
            async with httpx.AsyncClient(follow_redirects=True, timeout=60.0) as client:
                resp = await client.get(self._public_media_url(key), headers=headers)
                resp.raise_for_status()
                total = _parse_content_range_total(resp.headers.get("Content-Range"))
                if total is None and resp.status_code == 200:
                    total = len(resp.content)
                    return resp.content[start : end_inclusive + 1], total
                return resp.content, total
        blob = self._bucket().blob(key)
        blob.reload()
        data = blob.download_as_bytes(start=start, end=end_inclusive)
        return data, blob.size

    async def write_bytes(self, uri: str, data: bytes) -> str:
        if self.read_only:
            raise PermissionError(f"Source '{self.source_id}' is read-only")
        key = self._key(uri)
        self._bucket().blob(key).upload_from_string(data)
        return key


class GoogleDriveAudioDataSource(AudioDataSource):
    """Google Drive adapter supporting public/shared links & folders plus optional OAuth/API key."""

    kind = "gdrive"

    def __init__(
        self,
        source_id: str,
        folder_or_url: str,
        display_name: str | None = None,
        api_key: str | None = None,
        oauth_token: str | None = None,
        description: str | None = None,
        removable: bool = True,
    ) -> None:
        drive_id, is_folder = parse_google_drive_id(folder_or_url)
        self.source_id = source_id
        self.root_id = drive_id
        self.is_folder = is_folder
        self.raw_input = folder_or_url
        self.api_key = api_key.strip() if api_key else None
        self.oauth_token = oauth_token.strip() if oauth_token else None
        self.display_name = display_name or (
            f"Google Drive Folder ({drive_id[:8]}…)"
            if is_folder
            else f"Google Drive File ({drive_id[:8]}…)"
        )
        self.read_only = True
        self.anonymous = not bool(self.oauth_token)
        self.description = description or (
            "Google Drive shared folder" if is_folder else "Google Drive shared audio file"
        )
        self.removable = removable
        self.root_path_str = drive_id if is_folder else ""

    def _auth_headers(self) -> dict[str, str]:
        if self.oauth_token:
            return {"Authorization": f"Bearer {self.oauth_token}"}
        return {}

    async def _list_via_drive_api(self, folder_id: str, limit: int) -> list[BrowseEntry]:
        params: dict[str, str] = {
            "q": f"'{folder_id}' in parents and trashed = false",
            "fields": "files(id,name,mimeType,size)",
            "pageSize": str(max(1, min(1000, limit))),
            "orderBy": "folder,name",
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
        }
        if self.api_key and not self.oauth_token:
            params["key"] = self.api_key
        async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
            resp = await client.get(
                "https://www.googleapis.com/drive/v3/files",
                params=params,
                headers=self._auth_headers(),
            )
            resp.raise_for_status()
            data = resp.json()

        entries: list[BrowseEntry] = []
        for item in data.get("files", []):
            fid = str(item.get("id", ""))
            name = str(item.get("name", fid))
            mime = str(item.get("mimeType", ""))
            is_dir = mime == "application/vnd.google-apps.folder"
            size_raw = item.get("size")
            size_val = int(size_raw) if (size_raw is not None and not is_dir) else None
            entries.append(
                BrowseEntry(
                    name=name,
                    uri=fid if is_dir else f"gdrive://{fid}/{name}",
                    is_dir=is_dir,
                    size_bytes=size_val,
                    kind="other" if is_dir else classify_entry_kind(name),
                )
            )
        return entries

    async def _list_public_embedded_folder(self, folder_id: str, limit: int) -> list[BrowseEntry]:
        """Browse a public 'Anyone with the link' Google Drive folder without API keys."""
        url = f"https://drive.google.com/embeddedfolderview?id={quote(folder_id, safe='')}#list"
        async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            body = resp.text

        entries: list[BrowseEntry] = []
        pattern = re.compile(
            r'href="https://drive\.google\.com/(file/d|drive/folders)/([a-zA-Z0-9_-]{10,})[^"]*"'
            r'.*?class="flip-entry-title">([^<]+)<',
            re.DOTALL,
        )
        seen: set[str] = set()
        for kind_path, fid, raw_title in pattern.findall(body):
            if fid in seen:
                continue
            seen.add(fid)
            name = html.unescape(raw_title).strip()
            is_dir = kind_path == "drive/folders"
            entries.append(
                BrowseEntry(
                    name=name,
                    uri=fid if is_dir else f"gdrive://{fid}/{name}",
                    is_dir=is_dir,
                    kind="other" if is_dir else classify_entry_kind(name),
                )
            )
            if len(entries) >= limit:
                break
        return entries

    async def list_items(self, prefix: str = "", limit: int = 250) -> list[BrowseEntry]:
        if not self.is_folder:
            name = f"{self.root_id}.wav"
            return [
                BrowseEntry(
                    name=name,
                    uri=f"gdrive://{self.root_id}/{name}",
                    is_dir=False,
                    kind="audio",
                )
            ]

        folder_id = prefix.strip() or self.root_id
        if self.oauth_token or self.api_key:
            return await self._list_via_drive_api(folder_id, limit)
        return await self._list_public_embedded_folder(folder_id, limit)

    def _extract_file_id(self, uri: str) -> str:
        if uri.startswith("gdrive://"):
            return uri[len("gdrive://") :].split("/", 1)[0]
        fid, _ = parse_google_drive_id(uri)
        return fid

    def _download_url(self, file_id: str) -> tuple[str, dict[str, str]]:
        if self.oauth_token:
            return (
                f"https://www.googleapis.com/drive/v3/files/{quote(file_id, safe='')}?alt=media",
                self._auth_headers(),
            )
        if self.api_key:
            return (
                f"https://www.googleapis.com/drive/v3/files/{quote(file_id, safe='')}"
                f"?alt=media&key={quote(self.api_key, safe='')}",
                {},
            )
        return (
            f"https://drive.usercontent.google.com/download?id={quote(file_id, safe='')}"
            "&export=download&confirm=t",
            {},
        )

    async def read_bytes(self, uri: str) -> bytes:
        file_id = self._extract_file_id(uri)
        url, headers = self._download_url(file_id)
        async with httpx.AsyncClient(follow_redirects=True, timeout=120.0) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            if resp.headers.get("content-type", "").startswith("text/html"):
                raise PermissionError(
                    "Google Drive returned an HTML page instead of audio. Make sure the file "
                    "is shared ('Anyone with the link') or provide a Google Drive OAuth token."
                )
            return resp.content

    async def read_byte_range(
        self, uri: str, start: int, end_inclusive: int
    ) -> tuple[bytes, int | None]:
        file_id = self._extract_file_id(uri)
        url, base_headers = self._download_url(file_id)
        headers = {**base_headers, "Range": f"bytes={start}-{end_inclusive}"}
        async with httpx.AsyncClient(follow_redirects=True, timeout=60.0) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            if resp.headers.get("content-type", "").startswith("text/html"):
                raise PermissionError(
                    "Google Drive returned an HTML page instead of audio. Ensure sharing is "
                    "enabled or provide an OAuth token."
                )
            total = _parse_content_range_total(resp.headers.get("Content-Range"))
            if total is None and resp.status_code == 200:
                total = len(resp.content)
                return resp.content[start : end_inclusive + 1], total
            return resp.content, total

    async def write_bytes(self, uri: str, data: bytes) -> str:
        raise PermissionError("GoogleDriveAudioDataSource is read-only")


class HttpUrlAudioDataSource(AudioDataSource):
    """Read-only HTTP/HTTPS URL adapter for presigned URLs, public archives, or shared links."""

    kind = "http"

    def __init__(
        self,
        source_id: str = "http",
        display_name: str = "Direct Audio URLs",
        catalog_urls: list[str] | None = None,
        description: str | None = None,
        removable: bool = False,
    ) -> None:
        self.source_id = source_id
        self.display_name = display_name
        self.read_only = True
        self.anonymous = True
        self.description = description or "Open any direct HTTPS audio URL or shared Drive file link"
        self.removable = removable
        self.catalog_urls = catalog_urls or []

    def _normalize_url(self, uri: str) -> str:
        clean = uri.strip()
        if "drive.google.com" in clean:
            fid, is_folder = parse_google_drive_id(clean)
            if not is_folder and fid:
                return (
                    f"https://drive.usercontent.google.com/download?id={quote(fid, safe='')}"
                    "&export=download&confirm=t"
                )
        return clean

    async def list_items(self, prefix: str = "", limit: int = 250) -> list[BrowseEntry]:
        entries: list[BrowseEntry] = []
        for url in self.catalog_urls:
            name = urlparse(url).path.rsplit("/", 1)[-1] or url
            if prefix and prefix.lower() not in name.lower() and prefix.lower() not in url.lower():
                continue
            entries.append(
                BrowseEntry(
                    name=name,
                    uri=url,
                    is_dir=False,
                    kind=classify_entry_kind(name),
                )
            )
            if len(entries) >= limit:
                break
        return entries

    async def read_bytes(self, uri: str) -> bytes:
        url = self._normalize_url(uri)
        async with httpx.AsyncClient(follow_redirects=True, timeout=120.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.content

    async def read_byte_range(
        self, uri: str, start: int, end_inclusive: int
    ) -> tuple[bytes, int | None]:
        url = self._normalize_url(uri)
        headers = {"Range": f"bytes={start}-{end_inclusive}"}
        async with httpx.AsyncClient(follow_redirects=True, timeout=60.0) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            total = _parse_content_range_total(resp.headers.get("Content-Range"))
            if total is None and resp.status_code == 200:
                total = len(resp.content)
                return resp.content[start : end_inclusive + 1], total
            return resp.content, total

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
