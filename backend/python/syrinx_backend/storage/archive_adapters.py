from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import quote, urlparse

import httpx

from syrinx_backend.models import BrowseEntry
from syrinx_backend.storage.base import AudioDataSource, classify_entry_kind

_BROWSER_UA = "SyrinxOne/0.1 (Bioacoustic Research Workstation; +https://github.com/quill65/SyrinxOne)"


def _safe_filename(raw: str, fallback_ext: str = ".wav") -> str:
    cleaned = re.sub(r"[^\w.\-]+", "_", raw.strip()).strip("_.")
    if not cleaned:
        cleaned = f"recording{fallback_ext}"
    if "." not in cleaned:
        cleaned = f"{cleaned}{fallback_ext}"
    return cleaned[:120]


def _extract_extension_from_url(url: str, default_ext: str = ".wav") -> str:
    path = urlparse(url).path
    ext = Path(path).suffix.lower()
    if ext in {".wav", ".wave", ".flac", ".ogg", ".oga", ".mp3", ".m4a", ".aif", ".aiff", ".opus"}:
        return ext
    return default_ext


async def _http_read_bytes(url: str, headers: dict[str, str] | None = None) -> bytes:
    req_headers = {"User-Agent": _BROWSER_UA, **(headers or {})}
    async with httpx.AsyncClient(follow_redirects=True, timeout=60.0) as client:
        resp = await client.get(url, headers=req_headers)
        resp.raise_for_status()
        return resp.content


async def _http_read_range(
    url: str,
    start: int,
    end_inclusive: int,
    headers: dict[str, str] | None = None,
) -> tuple[bytes, int | None]:
    req_headers = {
        "User-Agent": _BROWSER_UA,
        "Range": f"bytes={max(0, start)}-{max(0, end_inclusive)}",
        **(headers or {}),
    }
    async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
        resp = await client.get(url, headers=req_headers)
        resp.raise_for_status()
        total: int | None = None
        cr = resp.headers.get("content-range")
        if cr and "/" in cr:
            tail = cr.rsplit("/", 1)[-1].strip()
            if tail.isdigit():
                total = int(tail)
        elif resp.status_code == 200:
            cl = resp.headers.get("content-length")
            if cl and cl.isdigit():
                total = int(cl)
            return resp.content[start : end_inclusive + 1], total
        return resp.content, total


class ZenodoAudioDataSource(AudioDataSource):
    """Browse and stream open-access audio datasets from Zenodo (zero API key required)."""

    kind = "zenodo"

    def __init__(
        self,
        source_id: str,
        record_id: str,
        display_name: str | None = None,
        description: str | None = None,
        removable: bool = True,
    ) -> None:
        self.source_id = source_id
        self.record_id = self._parse_record_id(record_id)
        self.display_name = display_name or f"Zenodo {self.record_id}"
        self.description = description or f"Zenodo open research dataset (record {self.record_id})"
        self.root_path_str = self.record_id
        self.read_only = True
        self.anonymous = True
        self.removable = removable
        self._url_map: dict[str, str] = {}

    @staticmethod
    def _parse_record_id(raw: str) -> str:
        s = raw.strip()
        m = re.search(r"zenodo\.(\d+)", s, re.IGNORECASE)
        if m:
            return m.group(1)
        m = re.search(r"/records?/(\d+)", s, re.IGNORECASE)
        if m:
            return m.group(1)
        m = re.search(r"(\d{5,})", s)
        return m.group(1) if m else s

    async def _ensure_loaded(self) -> list[dict]:
        url = f"https://zenodo.org/api/records/{quote(self.record_id, safe='')}"
        async with httpx.AsyncClient(follow_redirects=True, timeout=20.0) as client:
            resp = await client.get(url, headers={"User-Agent": _BROWSER_UA})
            resp.raise_for_status()
            data = resp.json()
        files = data.get("files") or []
        for f in files:
            key = str(f.get("key") or "")
            link = (f.get("links") or {}).get("self")
            if key and link:
                self._url_map[key] = str(link)
        return files

    async def list_items(self, prefix: str = "", limit: int = 250) -> list[BrowseEntry]:
        files = await self._ensure_loaded()
        norm_prefix = prefix.strip().lstrip("/")
        entries: list[BrowseEntry] = []
        seen_dirs: set[str] = set()

        if norm_prefix:
            parent = "/".join(norm_prefix.rstrip("/").split("/")[:-1])
            entries.append(
                BrowseEntry(name="..", uri=f"{parent}/" if parent else "", is_dir=True, kind="other")
            )

        for f in files:
            key = str(f.get("key") or "").lstrip("/")
            if not key or (norm_prefix and not key.startswith(norm_prefix)):
                continue
            rel = key[len(norm_prefix) :]
            if "/" in rel:
                sub = rel.split("/", 1)[0]
                dir_uri = f"{norm_prefix}{sub}/"
                if dir_uri not in seen_dirs:
                    seen_dirs.add(dir_uri)
                    entries.append(BrowseEntry(name=sub, uri=dir_uri, is_dir=True, kind="other"))
            else:
                size_raw = f.get("size")
                entries.append(
                    BrowseEntry(
                        name=rel,
                        uri=key,
                        is_dir=False,
                        size_bytes=int(size_raw) if isinstance(size_raw, int) else None,
                        kind=classify_entry_kind(rel),
                    )
                )
            if len(entries) >= limit:
                break
        return entries

    async def _resolve_content_url(self, uri: str) -> str:
        if uri.startswith(("http://", "https://")):
            return uri
        if uri not in self._url_map:
            await self._ensure_loaded()
        if uri in self._url_map:
            return self._url_map[uri]
        return f"https://zenodo.org/api/records/{self.record_id}/files/{quote(uri, safe='/')}/content"

    async def read_bytes(self, uri: str) -> bytes:
        return await _http_read_bytes(await self._resolve_content_url(uri))

    async def read_byte_range(
        self, uri: str, start: int, end_inclusive: int
    ) -> tuple[bytes, int | None]:
        return await _http_read_range(await self._resolve_content_url(uri), start, end_inclusive)

    async def write_bytes(self, uri: str, data: bytes) -> str:
        raise PermissionError("Zenodo source is read-only")


class HuggingFaceAudioDataSource(AudioDataSource):
    """Browse and stream public Hugging Face audio dataset repositories (zero API key required)."""

    kind = "huggingface"

    def __init__(
        self,
        source_id: str,
        repo_id: str,
        prefix: str = "",
        revision: str = "main",
        display_name: str | None = None,
        description: str | None = None,
        api_key: str | None = None,
        removable: bool = True,
    ) -> None:
        self.source_id = source_id
        self.repo_id = self._parse_repo_id(repo_id)
        self.prefix = prefix.strip("/")
        self.revision = (revision or "main").strip()
        self.api_key = api_key.strip() if api_key else (os.environ.get("HF_TOKEN") or "").strip() or None
        self.display_name = display_name or f"HF: {self.repo_id}"
        self.description = description or f"Hugging Face Dataset ({self.repo_id})"
        self.root_path_str = self.prefix
        self.read_only = True
        self.anonymous = not bool(self.api_key)
        self.removable = removable

    @staticmethod
    def _parse_repo_id(raw: str) -> str:
        s = raw.strip().rstrip("/")
        m = re.search(r"huggingface\.co/datasets/([^/\s]+/[^/\s]+)", s)
        if m:
            return m.group(1)
        return s.removeprefix("datasets/").removeprefix("hf://datasets/")

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    def _resolve_url(self, rel_path: str) -> str:
        if rel_path.startswith(("http://", "https://")):
            return rel_path
        clean = rel_path.lstrip("/")
        return (
            f"https://huggingface.co/datasets/{self.repo_id}/resolve/"
            f"{quote(self.revision, safe='')}/{quote(clean, safe='/')}"
        )

    async def list_items(self, prefix: str = "", limit: int = 250) -> list[BrowseEntry]:
        sub = prefix.strip("/") or self.prefix
        tree_url = (
            f"https://huggingface.co/api/datasets/{self.repo_id}/tree/"
            f"{quote(self.revision, safe='')}"
        )
        if sub:
            tree_url = f"{tree_url}/{quote(sub, safe='/')}"

        async with httpx.AsyncClient(follow_redirects=True, timeout=25.0) as client:
            resp = await client.get(
                tree_url,
                headers={"User-Agent": _BROWSER_UA, **self._headers()},
            )
            resp.raise_for_status()
            items = resp.json()

        entries: list[BrowseEntry] = []
        if sub and sub != self.prefix:
            parent = "/".join(sub.split("/")[:-1])
            entries.append(BrowseEntry(name="..", uri=parent, is_dir=True, kind="other"))

        if isinstance(items, list):
            # Folders first, then files
            dirs = [i for i in items if i.get("type") == "directory"]
            files = [i for i in items if i.get("type") == "file"]
            for d in dirs:
                p = str(d.get("path") or "")
                if not p:
                    continue
                entries.append(
                    BrowseEntry(name=Path(p).name or p, uri=p, is_dir=True, kind="other")
                )
                if len(entries) >= limit:
                    return entries
            for f in files:
                p = str(f.get("path") or "")
                if not p:
                    continue
                name = Path(p).name or p
                size_val = f.get("size")
                entries.append(
                    BrowseEntry(
                        name=name,
                        uri=p,
                        is_dir=False,
                        size_bytes=int(size_val) if isinstance(size_val, int) else None,
                        kind=classify_entry_kind(name),
                    )
                )
                if len(entries) >= limit:
                    break
        return entries

    async def read_bytes(self, uri: str) -> bytes:
        return await _http_read_bytes(self._resolve_url(uri), headers=self._headers())

    async def read_byte_range(
        self, uri: str, start: int, end_inclusive: int
    ) -> tuple[bytes, int | None]:
        return await _http_read_range(
            self._resolve_url(uri), start, end_inclusive, headers=self._headers()
        )

    async def write_bytes(self, uri: str, data: bytes) -> str:
        raise PermissionError("Hugging Face dataset source is read-only")


class INaturalistAudioDataSource(AudioDataSource):
    """Search and stream research-grade wildlife audio clips from iNaturalist (zero API key)."""

    kind = "inaturalist"

    def __init__(
        self,
        source_id: str,
        query: str = "",
        iconic_taxa: str | None = None,
        taxon_id: str | None = None,
        display_name: str | None = None,
        description: str | None = None,
        removable: bool = True,
    ) -> None:
        self.source_id = source_id
        self.query = query.strip()
        self.iconic_taxa = iconic_taxa.strip() if iconic_taxa else None
        self.taxon_id = taxon_id.strip() if taxon_id else None
        self.display_name = display_name or (
            f"iNaturalist: {self.query}" if self.query else "iNaturalist Wildlife Sounds"
        )
        self.description = description or "iNaturalist citizen-science wildlife audio clips"
        self.root_path_str = self.query
        self.read_only = True
        self.anonymous = True
        self.removable = removable
        self._url_map: dict[str, str] = {}

    async def list_items(self, prefix: str = "", limit: int = 60) -> list[BrowseEntry]:
        params: dict[str, str] = {
            "sounds": "true",
            "per_page": str(max(1, min(60, limit))),
            "order_by": "votes",
        }
        q_effective = prefix.strip() or self.query
        if q_effective:
            params["q"] = q_effective
        if self.iconic_taxa:
            params["iconic_taxa"] = self.iconic_taxa
        if self.taxon_id:
            params["taxon_id"] = self.taxon_id

        async with httpx.AsyncClient(follow_redirects=True, timeout=20.0) as client:
            resp = await client.get(
                "https://api.inaturalist.org/v1/observations",
                params=params,
                headers={"User-Agent": _BROWSER_UA},
            )
            resp.raise_for_status()
            data = resp.json()

        entries: list[BrowseEntry] = []
        seen: set[str] = set()
        for obs in data.get("results") or []:
            obs_id = obs.get("id")
            taxon = obs.get("taxon") or {}
            species = (
                taxon.get("preferred_common_name")
                or taxon.get("name")
                or obs.get("species_guess")
                or f"obs_{obs_id}"
            )
            for idx, sound in enumerate(obs.get("sounds") or []):
                file_url = str(sound.get("file_url") or "").strip()
                if not file_url or file_url in seen:
                    continue
                seen.add(file_url)
                ext = _extract_extension_from_url(file_url, ".wav")
                suffix = f"_{idx + 1}" if idx > 0 else ""
                fname = _safe_filename(f"{species}_iNat{obs_id}{suffix}{ext}", ext)
                self._url_map[fname] = file_url
                entries.append(
                    BrowseEntry(
                        name=fname,
                        uri=file_url,
                        is_dir=False,
                        kind="audio",
                    )
                )
                if len(entries) >= limit:
                    return entries
        return entries

    async def read_bytes(self, uri: str) -> bytes:
        url = self._url_map.get(uri, uri)
        return await _http_read_bytes(url)

    async def read_byte_range(
        self, uri: str, start: int, end_inclusive: int
    ) -> tuple[bytes, int | None]:
        url = self._url_map.get(uri, uri)
        return await _http_read_range(url, start, end_inclusive)

    async def write_bytes(self, uri: str, data: bytes) -> str:
        raise PermissionError("iNaturalist source is read-only")


class XenoCantoAudioDataSource(AudioDataSource):
    """Search and stream global bird, bat, frog, and insect clips via Xeno-canto API v3."""

    kind = "xenocanto"

    def __init__(
        self,
        source_id: str,
        query: str,
        api_key: str | None = None,
        display_name: str | None = None,
        description: str | None = None,
        removable: bool = True,
    ) -> None:
        self.source_id = source_id
        self.query = query.strip() or "q:A"
        self.api_key = (
            api_key.strip() if api_key else (os.environ.get("XENOCANTO_API_KEY") or "").strip()
        )
        self.display_name = display_name or f"Xeno-canto: {self.query}"
        self.description = description or "Xeno-canto wildlife sound recordings (API v3)"
        self.root_path_str = self.query
        self.read_only = True
        self.anonymous = False
        self.removable = removable

    async def list_items(self, prefix: str = "", limit: int = 60) -> list[BrowseEntry]:
        if not self.api_key:
            raise PermissionError(
                "Xeno-canto API v3 requires a free personal API key (xeno-canto.org/explore/api)."
            )
        q_str = prefix.strip() or self.query
        async with httpx.AsyncClient(follow_redirects=True, timeout=20.0) as client:
            resp = await client.get(
                "https://xeno-canto.org/api/3/recordings",
                params={"query": q_str, "key": self.api_key, "per_page": str(min(100, limit))},
                headers={"User-Agent": _BROWSER_UA},
            )
            resp.raise_for_status()
            data = resp.json()

        entries: list[BrowseEntry] = []
        for rec in data.get("recordings") or []:
            xc_id = str(rec.get("id") or "")
            file_url = str(rec.get("file") or "").strip()
            if file_url.startswith("//"):
                file_url = f"https:{file_url}"
            if not file_url:
                continue
            sp = str(rec.get("en") or rec.get("sp") or "recording")
            ext = _extract_extension_from_url(str(rec.get("file-name") or file_url), ".mp3")
            fname = _safe_filename(f"XC{xc_id}_{sp}{ext}", ext)
            entries.append(BrowseEntry(name=fname, uri=file_url, is_dir=False, kind="audio"))
            if len(entries) >= limit:
                break
        return entries

    async def read_bytes(self, uri: str) -> bytes:
        return await _http_read_bytes(uri)

    async def read_byte_range(
        self, uri: str, start: int, end_inclusive: int
    ) -> tuple[bytes, int | None]:
        return await _http_read_range(uri, start, end_inclusive)

    async def write_bytes(self, uri: str, data: bytes) -> str:
        raise PermissionError("Xeno-canto source is read-only")


class FreesoundAudioDataSource(AudioDataSource):
    """Search and stream Creative Commons audio clips via Freesound API v2."""

    kind = "freesound"

    def __init__(
        self,
        source_id: str,
        query: str,
        api_key: str | None = None,
        display_name: str | None = None,
        description: str | None = None,
        removable: bool = True,
    ) -> None:
        self.source_id = source_id
        self.query = query.strip() or "birdsong"
        self.api_key = (
            api_key.strip() if api_key else (os.environ.get("FREESOUND_API_KEY") or "").strip()
        )
        self.display_name = display_name or f"Freesound: {self.query}"
        self.description = description or "Freesound Creative Commons sound clip library"
        self.root_path_str = self.query
        self.read_only = True
        self.anonymous = False
        self.removable = removable

    async def list_items(self, prefix: str = "", limit: int = 50) -> list[BrowseEntry]:
        if not self.api_key:
            raise PermissionError(
                "Freesound API requires a free developer API key (freesound.org/apiv2/apply)."
            )
        q_str = prefix.strip() or self.query
        async with httpx.AsyncClient(follow_redirects=True, timeout=20.0) as client:
            resp = await client.get(
                "https://freesound.org/apiv2/search/text/",
                params={
                    "query": q_str,
                    "fields": "id,name,previews,type,filesize,samplerate,channels",
                    "page_size": str(min(100, limit)),
                    "token": self.api_key,
                },
                headers={"User-Agent": _BROWSER_UA},
            )
            resp.raise_for_status()
            data = resp.json()

        entries: list[BrowseEntry] = []
        for snd in data.get("results") or []:
            previews = snd.get("previews") or {}
            audio_url = (
                previews.get("preview-hq-ogg")
                or previews.get("preview-hq-mp3")
                or previews.get("preview-lq-ogg")
                or previews.get("preview-lq-mp3")
            )
            if not audio_url:
                continue
            ext = _extract_extension_from_url(str(audio_url), ".ogg")
            raw_name = str(snd.get("name") or f"freesound_{snd.get('id')}")
            fname = _safe_filename(f"FS{snd.get('id')}_{Path(raw_name).stem}{ext}", ext)
            entries.append(BrowseEntry(name=fname, uri=str(audio_url), is_dir=False, kind="audio"))
            if len(entries) >= limit:
                break
        return entries

    async def read_bytes(self, uri: str) -> bytes:
        return await _http_read_bytes(uri)

    async def read_byte_range(
        self, uri: str, start: int, end_inclusive: int
    ) -> tuple[bytes, int | None]:
        return await _http_read_range(uri, start, end_inclusive)

    async def write_bytes(self, uri: str, data: bytes) -> str:
        raise PermissionError("Freesound source is read-only")
