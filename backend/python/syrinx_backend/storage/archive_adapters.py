from __future__ import annotations

import asyncio
import hashlib
import io
import os
import re
import struct
import tarfile
import tempfile
import zipfile
import zlib
from pathlib import Path
from urllib.parse import quote, urlparse

import httpx

from syrinx_backend.models import BrowseEntry
from syrinx_backend.storage.base import AudioDataSource, classify_entry_kind

_BROWSER_UA = "SyrinxOne/0.1 (Bioacoustic Research Workstation; +https://github.com/quill65/SyrinxOne)"
_REMOTE_ZIP_INDEX_CACHE: dict[str, tuple[int, list[zipfile.ZipInfo]]] = {}
_MAX_TAR_CACHE_BYTES = 200 * 1024 * 1024  # 200 MB cap for remote .tar/.tar.gz archive caching


class _HttpRangeBuffer(io.RawIOBase):
    """Minimal synchronous seekable file backed by pre-fetched tail/central-directory bytes."""

    def __init__(self, total_size: int, chunks: list[tuple[int, bytes]]) -> None:
        self._size = total_size
        self._pos = 0
        self._chunks = chunks

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_SET:
            self._pos = max(0, offset)
        elif whence == io.SEEK_CUR:
            self._pos = max(0, self._pos + offset)
        elif whence == io.SEEK_END:
            self._pos = max(0, self._size + offset)
        return self._pos

    def tell(self) -> int:
        return self._pos

    def read(self, size: int = -1) -> bytes:
        if self._pos >= self._size:
            return b""
        want = self._size - self._pos if size is None or size < 0 else min(size, self._size - self._pos)
        for base, data in self._chunks:
            if base <= self._pos and self._pos + want <= base + len(data):
                rel = self._pos - base
                self._pos += want
                return data[rel : rel + want]
        return b""


async def _fetch_remote_zip_index(
    url: str,
    known_size: int | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, list[zipfile.ZipInfo]]:
    """Read only the tail/central-directory of a remote ZIP via HTTP Range (zero full download)."""
    if url in _REMOTE_ZIP_INDEX_CACHE:
        return _REMOTE_ZIP_INDEX_CACHE[url]

    req_headers = {"User-Agent": _BROWSER_UA, **(headers or {})}
    async with httpx.AsyncClient(follow_redirects=True, timeout=25.0) as client:
        total_size = known_size
        if not total_size or total_size <= 0:
            probe_resp = await client.get(url, headers={**req_headers, "Range": "bytes=0-0"})
            probe_resp.raise_for_status()
            cr = probe_resp.headers.get("content-range", "")
            if "/" in cr and cr.rsplit("/", 1)[-1].strip().isdigit():
                total_size = int(cr.rsplit("/", 1)[-1].strip())
            else:
                cl = probe_resp.headers.get("content-length", "")
                if cl.isdigit():
                    total_size = int(cl)
        if not total_size or total_size <= 22:
            raise ValueError("Remote ZIP size could not be determined for Range listing")

        tail_len = min(total_size, 65_536)
        tail_start = total_size - tail_len
        resp = await client.get(
            url,
            headers={**req_headers, "Range": f"bytes={tail_start}-{total_size - 1}"},
        )
        resp.raise_for_status()
        tail_bytes = resp.content
        chunks: list[tuple[int, bytes]] = [(tail_start, tail_bytes)]

        # Parse EOCD (or ZIP64 EOCD) to ensure the full central directory is covered
        eocd_idx = tail_bytes.rfind(b"PK\x05\x06")
        if eocd_idx < 0:
            raise ValueError("Remote file does not contain a valid ZIP central directory")

        cd_size, cd_offset = struct.unpack_from("<II", tail_bytes, eocd_idx + 12)
        if cd_offset == 0xFFFFFFFF or cd_size == 0xFFFFFFFF:
            # ZIP64 End of Central Directory Locator (PK\x06\x07) sits 20 bytes before EOCD
            loc_idx = tail_bytes.rfind(b"PK\x06\x07", 0, eocd_idx)
            if loc_idx >= 0 and loc_idx + 16 <= len(tail_bytes):
                z64_eocd_off = struct.unpack_from("<Q", tail_bytes, loc_idx + 8)[0]
                z64_rel = z64_eocd_off - tail_start
                if 0 <= z64_rel and z64_rel + 56 <= len(tail_bytes):
                    cd_size, cd_offset = struct.unpack_from("<QQ", tail_bytes, z64_rel + 40)

        if cd_offset < tail_start:
            cd_end = min(total_size - 1, cd_offset + cd_size + 65536)
            cd_resp = await client.get(
                url,
                headers={**req_headers, "Range": f"bytes={cd_offset}-{cd_end}"},
            )
            cd_resp.raise_for_status()
            chunks.append((cd_offset, cd_resp.content))

    buf = _HttpRangeBuffer(total_size, chunks)
    with zipfile.ZipFile(buf, "r") as zf:
        infos = list(zf.infolist())

    if len(_REMOTE_ZIP_INDEX_CACHE) > 32:
        _REMOTE_ZIP_INDEX_CACHE.pop(next(iter(_REMOTE_ZIP_INDEX_CACHE)))
    _REMOTE_ZIP_INDEX_CACHE[url] = (total_size, infos)
    return total_size, infos


def _list_zip_infos(
    infos: list[zipfile.ZipInfo],
    zip_uri_prefix: str,
    inner_prefix: str,
    parent_uri: str,
    limit: int = 250,
) -> list[BrowseEntry]:
    norm_inner = inner_prefix.strip().lstrip("/")
    entries: list[BrowseEntry] = [
        BrowseEntry(name="..", uri=parent_uri, is_dir=True, kind="other")
    ]
    seen_dirs: set[str] = set()
    dir_entries: list[BrowseEntry] = []
    file_entries: list[BrowseEntry] = []

    for zi in infos:
        fname = zi.filename.lstrip("/")
        if not fname or fname.startswith("__MACOSX/") or "/__MACOSX/" in fname:
            continue
        if Path(fname).name.startswith("._"):
            continue
        if norm_inner and not fname.startswith(norm_inner):
            continue
        rel = fname[len(norm_inner) :]
        if not rel or rel == "/":
            continue
        if "/" in rel:
            sub = rel.split("/", 1)[0]
            if not sub or sub in seen_dirs:
                continue
            seen_dirs.add(sub)
            dir_entries.append(
                BrowseEntry(
                    name=sub,
                    uri=f"{zip_uri_prefix}{norm_inner}{sub}/",
                    is_dir=True,
                    kind="other",
                )
            )
        elif not zi.is_dir():
            file_entries.append(
                BrowseEntry(
                    name=rel,
                    uri=f"{zip_uri_prefix}{fname}",
                    is_dir=False,
                    size_bytes=zi.file_size,
                    kind=classify_entry_kind(rel),
                )
            )

    dir_entries.sort(key=lambda e: e.name.lower())
    file_entries.sort(key=lambda e: e.name.lower())
    entries.extend(dir_entries)
    entries.extend(file_entries)
    return entries[:limit]


async def _read_remote_zip_entry(
    url: str,
    member_name: str,
    *,
    start: int | None = None,
    end_inclusive: int | None = None,
    known_size: int | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[bytes, int]:
    """Fetch and extract a single file (or prefix range) inside a remote ZIP via HTTP Range."""
    _total, infos = await _fetch_remote_zip_index(url, known_size=known_size, headers=headers)
    norm_target = member_name.lstrip("/")
    info = next((zi for zi in infos if zi.filename.lstrip("/") == norm_target), None)
    if info is None:
        raise FileNotFoundError(f"ZIP member not found: {member_name}")

    # Read 30-byte Local File Header to find exact payload offset
    lfh, _ = await _http_read_range(url, info.header_offset, info.header_offset + 29, headers=headers)
    if len(lfh) < 30 or lfh[:4] != b"PK\x03\x04":
        raise ValueError(f"Invalid ZIP local file header for {member_name}")
    fname_len, extra_len = struct.unpack_from("<HH", lfh, 26)
    data_start = info.header_offset + 30 + fname_len + extra_len

    # Direct byte-range read for uncompressed (ZIP_STORED) entries
    if info.compress_type == zipfile.ZIP_STORED:
        s = max(0, start or 0)
        e = min(info.file_size - 1, end_inclusive if end_inclusive is not None else info.file_size - 1)
        if e < s:
            return b"", info.file_size
        chunk, _ = await _http_read_range(url, data_start + s, data_start + e, headers=headers)
        return chunk, info.file_size

    if info.compress_type != zipfile.ZIP_DEFLATED:
        raise ValueError(f"Unsupported ZIP compression method ({info.compress_type}) for {member_name}")

    # Fast partial prefix decompression for WAV header probes (start == 0 and small end)
    if start == 0 and end_inclusive is not None and end_inclusive + 1 < info.file_size:
        want_out = end_inclusive + 1
        fetch_comp = min(info.compress_size, max(131_072, want_out * 2))
        comp_bytes, _ = await _http_read_range(
            url, data_start, data_start + max(0, fetch_comp - 1), headers=headers
        )
        decomp = zlib.decompressobj(-zlib.MAX_WBITS)
        out = decomp.decompress(comp_bytes, want_out)
        if len(out) >= min(want_out, info.file_size) or fetch_comp >= info.compress_size:
            return out[:want_out], info.file_size

    # Full entry decompression (still fetches ONLY this single member via HTTP Range!)
    comp_all, _ = await _http_read_range(
        url, data_start, data_start + max(0, info.compress_size - 1), headers=headers
    )
    full = zlib.decompress(comp_all, -zlib.MAX_WBITS)
    if start is not None and end_inclusive is not None:
        return full[start : end_inclusive + 1], len(full)
    return full, len(full)


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
        self._files_cache: list[dict] | None = None
        self._url_map: dict[str, str] = {}
        self._size_map: dict[str, int] = {}

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
        if self._files_cache is not None:
            return self._files_cache
        url = f"https://zenodo.org/api/records/{quote(self.record_id, safe='')}"
        async with httpx.AsyncClient(follow_redirects=True, timeout=20.0) as client:
            resp = await client.get(url, headers={"User-Agent": _BROWSER_UA})
            resp.raise_for_status()
            data = resp.json()
        files = data.get("files") or []
        for f in files:
            key = str(f.get("key") or "").lstrip("/")
            link = (f.get("links") or {}).get("self")
            size_raw = f.get("size")
            if key and link:
                self._url_map[key] = str(link)
                if isinstance(size_raw, int):
                    self._size_map[key] = size_raw
        self._files_cache = files
        return files

    async def list_items(self, prefix: str = "", limit: int = 250) -> list[BrowseEntry]:
        files = await self._ensure_loaded()
        norm_prefix = prefix.strip().lstrip("/")

        # Remote ZIP virtual directory browsing: e.g. "NFIELD_16k.zip::" or "NFIELD_16k.zip::NFIELD/"
        if "::" in norm_prefix:
            zip_key, inner_prefix = norm_prefix.split("::", 1)
            zip_url = await self._resolve_content_url(zip_key)
            _total, infos = await _fetch_remote_zip_index(
                zip_url, known_size=self._size_map.get(zip_key)
            )
            if inner_prefix.strip("/"):
                inner_parent = "/".join(inner_prefix.rstrip("/").split("/")[:-1])
                parent_uri = f"{zip_key}::{inner_parent}/" if inner_parent else f"{zip_key}::"
            else:
                parent_dir = "/".join(zip_key.split("/")[:-1])
                parent_uri = f"{parent_dir}/" if parent_dir else ""
            return _list_zip_infos(infos, f"{zip_key}::", inner_prefix, parent_uri, limit=limit)

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
                is_zip = rel.lower().endswith(".zip")
                entries.append(
                    BrowseEntry(
                        name=rel,
                        uri=f"{key}::" if is_zip else key,
                        is_dir=is_zip,
                        size_bytes=int(size_raw) if isinstance(size_raw, int) else None,
                        kind="other" if is_zip else classify_entry_kind(rel),
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
        if "::" in uri:
            zip_key, member = uri.split("::", 1)
            zip_url = await self._resolve_content_url(zip_key)
            data, _ = await _read_remote_zip_entry(
                zip_url, member, known_size=self._size_map.get(zip_key)
            )
            return data
        return await _http_read_bytes(await self._resolve_content_url(uri))

    async def read_byte_range(
        self, uri: str, start: int, end_inclusive: int
    ) -> tuple[bytes, int | None]:
        if "::" in uri:
            zip_key, member = uri.split("::", 1)
            zip_url = await self._resolve_content_url(zip_key)
            data, total = await _read_remote_zip_entry(
                zip_url,
                member,
                start=start,
                end_inclusive=end_inclusive,
                known_size=self._size_map.get(zip_key),
            )
            return data, total
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
        self._row_url_cache: dict[str, str] = {}

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

    async def _list_datasets_server_splits(self) -> list[BrowseEntry]:
        async with httpx.AsyncClient(follow_redirects=True, timeout=15.0) as client:
            resp = await client.get(
                "https://datasets-server.huggingface.co/splits",
                params={"dataset": self.repo_id},
                headers={"User-Agent": _BROWSER_UA, **self._headers()},
            )
            if resp.status_code != 200:
                return []
            splits = resp.json().get("splits") or []
        entries: list[BrowseEntry] = []
        for sp in splits:
            cfg = str(sp.get("config") or "default")
            split = str(sp.get("split") or "")
            if not split:
                continue
            label = f"{cfg}/{split}" if cfg != "default" else split
            entries.append(
                BrowseEntry(
                    name=label,
                    uri=f"__hf_split__/{cfg}/{split}/0",
                    is_dir=True,
                    kind="other",
                )
            )
        return entries

    async def _list_datasets_server_rows(self, split_uri: str, limit: int) -> list[BrowseEntry]:
        parts = split_uri.split("/")
        # __hf_split__/{config}/{split}/{offset}
        if len(parts) < 3:
            return []
        cfg = parts[1]
        split = parts[2]
        offset = int(parts[3]) if len(parts) >= 4 and parts[3].isdigit() else 0
        page_len = min(100, max(10, limit))
        async with httpx.AsyncClient(follow_redirects=True, timeout=20.0) as client:
            resp = await client.get(
                "https://datasets-server.huggingface.co/rows",
                params={
                    "dataset": self.repo_id,
                    "config": cfg,
                    "split": split,
                    "offset": str(offset),
                    "length": str(page_len),
                },
                headers={"User-Agent": _BROWSER_UA, **self._headers()},
            )
            resp.raise_for_status()
            data = resp.json()

        entries: list[BrowseEntry] = [BrowseEntry(name="..", uri="", is_dir=True, kind="other")]
        rows = data.get("rows") or []
        for item in rows:
            row_idx = int(item.get("row_idx", 0))
            row = item.get("row") or {}
            audio_src: str | None = None
            for val in row.values():
                if isinstance(val, list) and val and isinstance(val[0], dict) and val[0].get("src"):
                    audio_src = str(val[0]["src"])
                    break
                if isinstance(val, dict) and val.get("src"):
                    audio_src = str(val["src"])
                    break
            if not audio_src:
                continue
            raw_label = str(
                row.get("name")
                or row.get("file")
                or row.get("path")
                or row.get("id")
                or f"{split}_{row_idx:04d}"
            )
            clean_label = raw_label.replace("/", "_")
            ext = _extract_extension_from_url(audio_src, ".wav")
            stem = Path(clean_label).stem
            fname = _safe_filename(f"{stem}{ext}", ext)
            row_uri = f"__hf_row__/{cfg}/{split}/{row_idx}"
            self._row_url_cache[row_uri] = audio_src
            entries.append(BrowseEntry(name=fname, uri=row_uri, is_dir=False, kind="audio"))

        total_rows = int(data.get("num_rows_total") or 0)
        next_offset = offset + len(rows)
        if total_rows > next_offset:
            entries.append(
                BrowseEntry(
                    name=f"Next ({next_offset}–{min(total_rows, next_offset + page_len)} of {total_rows})",
                    uri=f"__hf_split__/{cfg}/{split}/{next_offset}",
                    is_dir=True,
                    kind="other",
                )
            )
        return entries

    async def _resolve_hf_row_url(self, row_uri: str) -> str:
        if row_uri in self._row_url_cache:
            return self._row_url_cache[row_uri]
        parts = row_uri.split("/")
        if len(parts) < 4:
            raise FileNotFoundError(f"Invalid HF row URI: {row_uri}")
        cfg, split, row_idx = parts[1], parts[2], parts[3]
        async with httpx.AsyncClient(follow_redirects=True, timeout=20.0) as client:
            resp = await client.get(
                "https://datasets-server.huggingface.co/rows",
                params={
                    "dataset": self.repo_id,
                    "config": cfg,
                    "split": split,
                    "offset": row_idx,
                    "length": "1",
                },
                headers={"User-Agent": _BROWSER_UA, **self._headers()},
            )
            resp.raise_for_status()
            rows = resp.json().get("rows") or []
        if not rows:
            raise FileNotFoundError(f"HF row {row_idx} not found in {cfg}/{split}")
        row = rows[0].get("row") or {}
        for val in row.values():
            if isinstance(val, list) and val and isinstance(val[0], dict) and val[0].get("src"):
                url = str(val[0]["src"])
                self._row_url_cache[row_uri] = url
                return url
            if isinstance(val, dict) and val.get("src"):
                url = str(val["src"])
                self._row_url_cache[row_uri] = url
                return url
        raise FileNotFoundError(f"No audio column found in HF row {row_uri}")

    async def list_items(self, prefix: str = "", limit: int = 250) -> list[BrowseEntry]:
        if prefix.startswith("__hf_split__/"):
            return await self._list_datasets_server_rows(prefix, limit=limit)

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
            dirs = [i for i in items if i.get("type") == "directory"]
            files = [i for i in items if i.get("type") == "file"]

            # Detect Parquet-only Hugging Face datasets (e.g. philgzl/demand) at root or data/
            parquet_files = [f for f in files if str(f.get("path") or "").lower().endswith(".parquet")]
            audio_files = [
                f for f in files if classify_entry_kind(Path(str(f.get("path") or "")).name) == "audio"
            ]
            only_data_dir_at_root = (
                not sub
                and not audio_files
                and len(dirs) == 1
                and str(dirs[0].get("path") or "") == "data"
            )
            if (parquet_files and not audio_files) or only_data_dir_at_root:
                ds_splits = await self._list_datasets_server_splits()
                if ds_splits:
                    # Keep README/docs alongside streaming splits
                    doc_entries = [
                        BrowseEntry(
                            name=Path(str(f.get("path") or "")).name,
                            uri=str(f.get("path") or ""),
                            is_dir=False,
                            size_bytes=int(f["size"]) if isinstance(f.get("size"), int) else None,
                            kind=classify_entry_kind(Path(str(f.get("path") or "")).name),
                        )
                        for f in files
                        if Path(str(f.get("path") or "")).name.lower().startswith("readme")
                    ]
                    return [*ds_splits, *doc_entries][:limit]

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
        if uri.startswith("__hf_row__/"):
            url = await self._resolve_hf_row_url(uri)
            return await _http_read_bytes(url)
        return await _http_read_bytes(self._resolve_url(uri), headers=self._headers())

    async def read_byte_range(
        self, uri: str, start: int, end_inclusive: int
    ) -> tuple[bytes, int | None]:
        if uri.startswith("__hf_row__/"):
            url = await self._resolve_hf_row_url(uri)
            return await _http_read_range(url, start, end_inclusive)
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


class FigshareAudioDataSource(AudioDataSource):
    """Browse and stream open-access scientific audio datasets from Figshare API v2."""

    kind = "figshare"

    def __init__(
        self,
        source_id: str,
        article_id: str,
        display_name: str | None = None,
        description: str | None = None,
        removable: bool = True,
    ) -> None:
        self.source_id = source_id
        self.article_id = self._parse_article_id(article_id)
        self.display_name = display_name or f"Figshare {self.article_id}"
        self.description = description or f"Figshare open dataset (article {self.article_id})"
        self.root_path_str = self.article_id
        self.read_only = True
        self.anonymous = True
        self.removable = removable
        self._files_cache: list[dict] | None = None
        self._url_map: dict[str, str] = {}
        self._size_map: dict[str, int] = {}

    @staticmethod
    def _parse_article_id(raw: str) -> str:
        s = raw.strip()
        m = re.search(r"/(\d{5,})(?:/\d+)?(?:[?#]|$)", s)
        if m:
            return m.group(1)
        m = re.search(r"(\d{5,})", s)
        return m.group(1) if m else s

    async def _ensure_loaded(self) -> list[dict]:
        if self._files_cache is not None:
            return self._files_cache
        url = f"https://api.figshare.com/v2/articles/{quote(self.article_id, safe='')}"
        async with httpx.AsyncClient(follow_redirects=True, timeout=20.0) as client:
            resp = await client.get(url, headers={"User-Agent": _BROWSER_UA})
            resp.raise_for_status()
            data = resp.json()
        files = data.get("files") or []
        for f in files:
            name = str(f.get("name") or "").lstrip("/")
            dl = f.get("download_url")
            sz = f.get("size")
            if name and dl:
                self._url_map[name] = str(dl)
                if isinstance(sz, int):
                    self._size_map[name] = sz
        self._files_cache = files
        return files

    async def _resolve_url(self, name: str) -> str:
        if name.startswith(("http://", "https://")):
            return name
        if name not in self._url_map:
            await self._ensure_loaded()
        if name not in self._url_map:
            raise FileNotFoundError(f"Figshare file not found: {name}")
        return self._url_map[name]

    async def list_items(self, prefix: str = "", limit: int = 250) -> list[BrowseEntry]:
        files = await self._ensure_loaded()
        norm_prefix = prefix.strip().lstrip("/")
        if "::" in norm_prefix:
            zip_key, inner_prefix = norm_prefix.split("::", 1)
            zip_url = await self._resolve_url(zip_key)
            _total, infos = await _fetch_remote_zip_index(
                zip_url, known_size=self._size_map.get(zip_key)
            )
            if inner_prefix.strip("/"):
                inner_parent = "/".join(inner_prefix.rstrip("/").split("/")[:-1])
                parent_uri = f"{zip_key}::{inner_parent}/" if inner_parent else f"{zip_key}::"
            else:
                parent_uri = ""
            return _list_zip_infos(infos, f"{zip_key}::", inner_prefix, parent_uri, limit=limit)

        entries: list[BrowseEntry] = []
        for f in files:
            name = str(f.get("name") or "").lstrip("/")
            if not name:
                continue
            sz = f.get("size")
            is_zip = name.lower().endswith(".zip")
            entries.append(
                BrowseEntry(
                    name=name,
                    uri=f"{name}::" if is_zip else name,
                    is_dir=is_zip,
                    size_bytes=int(sz) if isinstance(sz, int) else None,
                    kind="other" if is_zip else classify_entry_kind(name),
                )
            )
            if len(entries) >= limit:
                break
        return entries

    async def read_bytes(self, uri: str) -> bytes:
        if "::" in uri:
            zip_key, member = uri.split("::", 1)
            zip_url = await self._resolve_url(zip_key)
            data, _ = await _read_remote_zip_entry(
                zip_url, member, known_size=self._size_map.get(zip_key)
            )
            return data
        return await _http_read_bytes(await self._resolve_url(uri))

    async def read_byte_range(
        self, uri: str, start: int, end_inclusive: int
    ) -> tuple[bytes, int | None]:
        if "::" in uri:
            zip_key, member = uri.split("::", 1)
            zip_url = await self._resolve_url(zip_key)
            data, total = await _read_remote_zip_entry(
                zip_url,
                member,
                start=start,
                end_inclusive=end_inclusive,
                known_size=self._size_map.get(zip_key),
            )
            return data, total
        return await _http_read_range(await self._resolve_url(uri), start, end_inclusive)

    async def write_bytes(self, uri: str, data: bytes) -> str:
        raise PermissionError("Figshare source is read-only")


class RemoteArchiveAudioDataSource(AudioDataSource):
    """Browse and stream audio files inside a remote .zip (via HTTP Range) or .tar/.tar.gz archive URL."""

    kind = "archive"

    def __init__(
        self,
        source_id: str,
        archive_url: str,
        archive_format: str = "auto",
        display_name: str | None = None,
        description: str | None = None,
        removable: bool = True,
    ) -> None:
        self.source_id = source_id
        self.archive_url = archive_url.strip()
        self.archive_format = archive_format.strip().lower() or "auto"
        self.display_name = display_name or Path(urlparse(self.archive_url).path).name or "Remote Archive"
        self.description = description or f"Remote acoustic archive ({self.archive_url})"
        self.root_path_str = ""
        self.read_only = True
        self.anonymous = True
        self.removable = removable
        self._tar_cache_path: Path | None = None
        self._tar_members: list[tuple[str, int]] | None = None

    def _is_zip(self) -> bool:
        if self.archive_format == "zip":
            return True
        if self.archive_format in ("tar", "tgz", "tar.gz"):
            return False
        return urlparse(self.archive_url).path.lower().endswith(".zip")

    async def _ensure_tar_cached(self) -> tuple[Path, list[tuple[str, int]]]:
        if self._tar_cache_path and self._tar_cache_path.is_file() and self._tar_members is not None:
            return self._tar_cache_path, self._tar_members

        cache_dir = Path(tempfile.gettempdir()) / "syrinx_remote_archives"
        cache_dir.mkdir(parents=True, exist_ok=True)
        key = hashlib.sha256(self.archive_url.encode("utf-8")).hexdigest()[:16]
        target = cache_dir / f"{key}.tar"

        if not target.is_file() or target.stat().st_size == 0:
            async with httpx.AsyncClient(follow_redirects=True, timeout=120.0) as client:
                async with client.stream(
                    "GET", self.archive_url, headers={"User-Agent": _BROWSER_UA}
                ) as resp:
                    resp.raise_for_status()
                    cl = resp.headers.get("content-length")
                    if cl and cl.isdigit() and int(cl) > _MAX_TAR_CACHE_BYTES:
                        raise ValueError(
                            f"Remote TAR archive exceeds {_MAX_TAR_CACHE_BYTES // (1024 * 1024)} MB streaming cache cap"
                        )
                    total = 0
                    tmp_path = target.with_suffix(".part")
                    with tmp_path.open("wb") as out:
                        async for chunk in resp.aiter_bytes(262_144):
                            total += len(chunk)
                            if total > _MAX_TAR_CACHE_BYTES:
                                out.close()
                                tmp_path.unlink(missing_ok=True)
                                raise ValueError("Remote TAR archive exceeded size cap during download")
                            out.write(chunk)
                    tmp_path.replace(target)

        def _index_tar(p: Path) -> list[tuple[str, int]]:
            out_list: list[tuple[str, int]] = []
            with tarfile.open(p, "r:*") as tf:
                for m in tf.getmembers():
                    if not m.isfile():
                        continue
                    clean = m.name.removeprefix("./").lstrip("/")
                    if (
                        not clean
                        or clean.startswith("__MACOSX/")
                        or "/__MACOSX/" in clean
                        or Path(clean).name.startswith("._")
                    ):
                        continue
                    out_list.append((clean, m.size))
            return out_list

        members = await asyncio.to_thread(_index_tar, target)
        self._tar_cache_path = target
        self._tar_members = members
        return target, members

    async def list_items(self, prefix: str = "", limit: int = 250) -> list[BrowseEntry]:
        norm_prefix = prefix.strip().lstrip("/")
        if self._is_zip():
            _total, infos = await _fetch_remote_zip_index(self.archive_url)
            parent = "/".join(norm_prefix.rstrip("/").split("/")[:-1])
            parent_uri = f"{parent}/" if parent else ""
            entries = _list_zip_infos(infos, "", norm_prefix, parent_uri, limit=limit)
            return entries if norm_prefix else [e for e in entries if e.name != ".."]

        _path, members = await self._ensure_tar_cached()
        entries: list[BrowseEntry] = []
        if norm_prefix:
            parent = "/".join(norm_prefix.rstrip("/").split("/")[:-1])
            entries.append(
                BrowseEntry(name="..", uri=f"{parent}/" if parent else "", is_dir=True, kind="other")
            )
        seen_dirs: set[str] = set()
        dir_entries: list[BrowseEntry] = []
        file_entries: list[BrowseEntry] = []
        for fname, size in members:
            if norm_prefix and not fname.startswith(norm_prefix):
                continue
            rel = fname[len(norm_prefix) :]
            if "/" in rel:
                sub = rel.split("/", 1)[0]
                if sub and sub not in seen_dirs:
                    seen_dirs.add(sub)
                    dir_entries.append(
                        BrowseEntry(name=sub, uri=f"{norm_prefix}{sub}/", is_dir=True, kind="other")
                    )
            else:
                file_entries.append(
                    BrowseEntry(
                        name=rel,
                        uri=fname,
                        is_dir=False,
                        size_bytes=size,
                        kind=classify_entry_kind(rel),
                    )
                )
        dir_entries.sort(key=lambda e: e.name.lower())
        file_entries.sort(key=lambda e: e.name.lower())
        entries.extend(dir_entries)
        entries.extend(file_entries)
        return entries[:limit]

    async def read_bytes(self, uri: str) -> bytes:
        if self._is_zip():
            data, _ = await _read_remote_zip_entry(self.archive_url, uri)
            return data
        tar_path, _ = await self._ensure_tar_cached()

        def _extract_member(p: Path, target_name: str) -> bytes:
            want = target_name.removeprefix("./").lstrip("/")
            with tarfile.open(p, "r:*") as tf:
                for m in tf.getmembers():
                    if m.isfile() and m.name.removeprefix("./").lstrip("/") == want:
                        fh = tf.extractfile(m)
                        if fh is not None:
                            return fh.read()
            raise FileNotFoundError(f"Archive member not found: {target_name}")

        return await asyncio.to_thread(_extract_member, tar_path, uri)

    async def read_byte_range(
        self, uri: str, start: int, end_inclusive: int
    ) -> tuple[bytes, int | None]:
        if self._is_zip():
            data, total = await _read_remote_zip_entry(
                self.archive_url, uri, start=start, end_inclusive=end_inclusive
            )
            return data, total
        full = await self.read_bytes(uri)
        return full[start : end_inclusive + 1], len(full)

    async def write_bytes(self, uri: str, data: bytes) -> str:
        raise PermissionError("Remote archive source is read-only")
