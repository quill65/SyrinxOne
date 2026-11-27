from __future__ import annotations

import asyncio
import json
import os
import re
from abc import ABC, abstractmethod
from pathlib import Path
from urllib.parse import quote

import httpx

from syrinx_backend.models import (
    CreateSourceRequest,
    DiscoveredSourceCandidate,
    DiscoverSourcesRequest,
    DiscoverSourcesResponse,
    DiscoveryProviderInfo,
    SourceVerificationResult,
)
from syrinx_backend.storage.archive_adapters import (
    FreesoundAudioDataSource,
    HuggingFaceAudioDataSource,
    INaturalistAudioDataSource,
    XenoCantoAudioDataSource,
    ZenodoAudioDataSource,
)
from syrinx_backend.storage.base import AudioDataSource
from syrinx_backend.storage.cloud_adapters import (
    GCSAudioDataSource,
    GoogleDriveAudioDataSource,
    HttpUrlAudioDataSource,
    S3AudioDataSource,
)

_BROWSER_UA = "SyrinxOne/0.1 (Bioacoustic Research Workstation; +https://github.com/quill65/SyrinxOne)"
_STOP_WORDS = {
    "a", "an", "and", "archive", "archives", "audio", "clips", "data", "dataset", "datasets",
    "files", "find", "for", "from", "i", "in", "libraries", "library", "looking", "me", "of",
    "on", "online", "open", "or", "public", "recording", "recordings", "search", "show", "some",
    "sound", "sounds", "source", "sources", "that", "the", "to", "want", "wav", "with",
}


def slugify_source_id(raw: str, prefix: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", raw.lower()).strip("_")[:36]
    return f"{prefix}_{slug}" if slug else f"{prefix}_source"


def build_source_from_request(req: CreateSourceRequest) -> AudioDataSource:
    """Generic source factory supporting all built-in cloud and open-archive adapters."""
    kind = req.kind.strip().lower()
    params = req.params or {}

    if kind == "s3":
        if not req.bucket or not req.bucket.strip():
            raise ValueError("S3 source requires a bucket name")
        bucket = req.bucket.strip().removeprefix("s3://").split("/", 1)[0]
        sid = req.source_id or slugify_source_id(bucket, "s3")
        return S3AudioDataSource(
            source_id=sid,
            bucket=bucket,
            prefix=req.prefix,
            display_name=req.display_name or f"S3: {bucket}",
            region=req.region,
            endpoint_url=req.endpoint_url,
            read_only=req.read_only or req.anonymous,
            anonymous=req.anonymous,
            description=f"s3://{bucket}/{req.prefix}".rstrip("/"),
            removable=True,
        )

    if kind == "gcs":
        if not req.bucket or not req.bucket.strip():
            raise ValueError("GCS source requires a bucket name")
        bucket = req.bucket.strip().removeprefix("gs://").split("/", 1)[0]
        sid = req.source_id or slugify_source_id(bucket, "gcs")
        return GCSAudioDataSource(
            source_id=sid,
            bucket=bucket,
            prefix=req.prefix,
            display_name=req.display_name or f"GCS: {bucket}",
            read_only=req.read_only or req.anonymous,
            anonymous=req.anonymous,
            description=f"gs://{bucket}/{req.prefix}".rstrip("/"),
            removable=True,
        )

    if kind == "gdrive":
        if not req.folder_or_url or not req.folder_or_url.strip():
            raise ValueError("Google Drive source requires a shared folder/file URL or Drive ID")
        sid = req.source_id or slugify_source_id(req.display_name or req.folder_or_url[:18], "gdrive")
        return GoogleDriveAudioDataSource(
            source_id=sid,
            folder_or_url=req.folder_or_url,
            display_name=req.display_name,
            api_key=req.api_key,
            oauth_token=req.oauth_token,
            removable=True,
        )

    if kind == "http":
        raw_urls = req.folder_or_url or ""
        urls = [u.strip() for u in re.split(r"[\n,]+", raw_urls) if u.strip()]
        sid = req.source_id or slugify_source_id(req.display_name or "urls", "http")
        return HttpUrlAudioDataSource(
            source_id=sid,
            display_name=req.display_name or "HTTP Audio Catalog",
            catalog_urls=urls,
            removable=True,
        )

    if kind == "zenodo":
        record_ref = str(req.folder_or_url or req.bucket or params.get("recordId") or "").strip()
        if not record_ref:
            raise ValueError("Zenodo source requires a record ID, DOI, or Zenodo URL")
        sid = req.source_id or slugify_source_id(req.display_name or record_ref, "zenodo")
        return ZenodoAudioDataSource(
            source_id=sid,
            record_id=record_ref,
            display_name=req.display_name,
            removable=True,
        )

    if kind == "huggingface":
        repo_ref = str(req.folder_or_url or req.bucket or params.get("repoId") or "").strip()
        if not repo_ref:
            raise ValueError("Hugging Face source requires a dataset repo ID (e.g. org/dataset)")
        sid = req.source_id or slugify_source_id(req.display_name or repo_ref, "hf")
        return HuggingFaceAudioDataSource(
            source_id=sid,
            repo_id=repo_ref,
            prefix=req.prefix,
            revision=str(params.get("revision") or "main"),
            display_name=req.display_name,
            api_key=req.api_key,
            removable=True,
        )

    if kind == "inaturalist":
        q = str(req.query or req.folder_or_url or params.get("query") or "").strip()
        iconic = str(params.get("iconicTaxa") or "").strip() or None
        taxon_id = str(params.get("taxonId") or "").strip() or None
        sid = req.source_id or slugify_source_id(req.display_name or q or iconic or "sounds", "inat")
        return INaturalistAudioDataSource(
            source_id=sid,
            query=q,
            iconic_taxa=iconic,
            taxon_id=taxon_id,
            display_name=req.display_name,
            removable=True,
        )

    if kind == "xenocanto":
        q = str(req.query or req.folder_or_url or params.get("query") or "q:A").strip()
        sid = req.source_id or slugify_source_id(req.display_name or q, "xc")
        return XenoCantoAudioDataSource(
            source_id=sid,
            query=q,
            api_key=req.api_key,
            display_name=req.display_name,
            removable=True,
        )

    if kind == "freesound":
        q = str(req.query or req.folder_or_url or params.get("query") or "birdsong").strip()
        sid = req.source_id or slugify_source_id(req.display_name or q, "fs")
        return FreesoundAudioDataSource(
            source_id=sid,
            query=q,
            api_key=req.api_key,
            display_name=req.display_name,
            removable=True,
        )

    raise ValueError(f"Unsupported source kind: {req.kind}")


async def verify_audio_source(
    source: AudioDataSource,
    timeout_sec: float = 8.0,
) -> SourceVerificationResult:
    """Lightweight live compatibility probe: checks listing, streamable audio, and WAV header."""
    try:
        entries = await asyncio.wait_for(source.list_items("", limit=60), timeout=timeout_sec)
    except PermissionError as exc:
        return SourceVerificationResult(
            status="auth_required",
            streamable=False,
            message=str(exc),
        )
    except TimeoutError:
        return SourceVerificationResult(
            status="error",
            streamable=False,
            message=f"Timed out after {timeout_sec:.0f}s while listing archive.",
        )
    except Exception as exc:
        detail = str(exc).strip() or type(exc).__name__
        return SourceVerificationResult(
            status="error",
            streamable=False,
            message=f"Could not list archive: {detail}",
        )

    audio_entries = [e for e in entries if not e.is_dir and e.kind == "audio"]
    zip_entries = [
        e
        for e in entries
        if not e.is_dir
        and Path(e.name).suffix.lower() in {".zip", ".tar", ".gz", ".tgz", ".7z", ".parquet"}
    ]
    dir_entries = [e for e in entries if e.is_dir and e.name != ".."]

    # If top level only has folders (e.g. congo8khz-pnnn/recordings/wav/ or HF nested trees), probe up to 7 levels deep
    depth = 0
    probe_dirs = dir_entries[:3]
    while not audio_entries and probe_dirs and depth < 7:
        depth += 1
        next_dirs = []
        for d in probe_dirs:
            try:
                sub = await asyncio.wait_for(source.list_items(d.uri, limit=40), timeout=timeout_sec)
            except Exception:
                continue
            audio_entries.extend([e for e in sub if not e.is_dir and e.kind == "audio"])
            zip_entries.extend(
                [
                    e
                    for e in sub
                    if not e.is_dir
                    and Path(e.name).suffix.lower() in {".zip", ".tar", ".gz", ".tgz", ".7z", ".parquet"}
                ]
            )
            next_dirs.extend([e for e in sub if e.is_dir and e.name != ".."])
            if audio_entries:
                break
        probe_dirs = next_dirs[:3]

    if not audio_entries:
        if zip_entries:
            exts = sorted({Path(e.name).suffix.lower().lstrip(".") for e in zip_entries})
            return SourceVerificationResult(
                status="zipped_only",
                streamable=False,
                audio_file_count=0,
                formats=exts,
                message=f"Contains packaged archives ({', '.join(exts)}) — download & extract to open locally.",
            )
        if probe_dirs:
            return SourceVerificationResult(
                status="untested",
                streamable=False,
                audio_file_count=0,
                message="Deep folder hierarchy (>7 levels) — connect to browse subdirectories.",
            )
        return SourceVerificationResult(
            status="incompatible",
            streamable=False,
            audio_file_count=0,
            message="No streamable audio files (.wav/.flac/.ogg/.mp3) found in archive.",
        )

    formats = sorted(
        {Path(e.name).suffix.lower().lstrip(".") or "audio" for e in audio_entries if e.name}
    )
    sample = next(
        (e for e in audio_entries if e.name.lower().endswith((".wav", ".wave"))),
        audio_entries[0],
    )
    sample_rate_hz: int | None = None
    num_channels: int | None = None
    supports_range = False

    try:
        chunk, _total = await asyncio.wait_for(
            source.read_byte_range(sample.uri, 0, 65535), timeout=timeout_sec
        )
        if chunk:
            from syrinx_backend.audio.loader import _parse_wav_header

            supports_range = True
            hdr = _parse_wav_header(chunk)
            if hdr is not None:
                sample_rate_hz = hdr.sample_rate
                num_channels = hdr.num_channels
    except Exception:
        supports_range = False

    parts = [f"{len(audio_entries)}+ audio files ({', '.join(formats[:3])})"]
    if sample_rate_hz:
        sr_label = (
            f"{sample_rate_hz / 1000:.1f} kHz" if sample_rate_hz >= 1000 else f"{sample_rate_hz} Hz"
        )
        parts.append(sr_label)
    if num_channels:
        parts.append(f"{num_channels} ch" if num_channels > 1 else "Mono")
    if supports_range:
        parts.append("Range streaming")

    return SourceVerificationResult(
        status="verified",
        streamable=True,
        audio_file_count=len(audio_entries),
        sample_rate_hz=sample_rate_hz,
        num_channels=num_channels,
        formats=formats,
        supports_range=supports_range,
        sample_file_name=sample.name,
        message=" · ".join(parts),
    )


def _extract_terms_offline(prompt: str) -> list[str]:
    raw_words = re.findall(r"[a-zA-Z0-9][a-zA-Z0-9\-']*", prompt.lower())
    terms = [w for w in raw_words if len(w) >= 2 and w not in _STOP_WORDS]
    dedup: list[str] = []
    for t in terms:
        if t not in dedup:
            dedup.append(t)
    return dedup[:8] or [prompt.strip()]


async def _expand_prompt_terms(prompt: str, gemini_api_key: str | None) -> list[str]:
    base_terms = _extract_terms_offline(prompt)
    key = (gemini_api_key or os.environ.get("GEMINI_API_KEY") or "").strip()
    if not key:
        return base_terms
    try:
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"gemini-2.5-flash:generateContent?key={quote(key, safe='')}"
        )
        body = {
            "contents": [
                {
                    "parts": [
                        {
                            "text": (
                                "Extract 3 to 6 concise bioacoustic search keywords (including any "
                                "relevant taxonomic/scientific names or habitat terms) for searching "
                                "public audio archives like Zenodo, Hugging Face, iNaturalist, and AWS Open Data. "
                                f"Return ONLY a JSON array of short strings.\nPrompt: {prompt}"
                            )
                        }
                    ]
                }
            ],
            "generationConfig": {"temperature": 0.1, "maxOutputTokens": 128},
        }
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.post(url, json=body)
            if resp.status_code == 200:
                text = (
                    resp.json()
                    .get("candidates", [{}])[0]
                    .get("content", {})
                    .get("parts", [{}])[0]
                    .get("text", "")
                )
                m = re.search(r"\[[\s\S]*?\]", text)
                if m:
                    parsed = json.loads(m.group(0))
                    if isinstance(parsed, list):
                        expanded = [str(x).strip() for x in parsed if str(x).strip()]
                        merged = list(dict.fromkeys([*base_terms, *expanded]))
                        return merged[:8]
    except Exception:
        pass
    return base_terms


class DiscoveryProvider(ABC):
    """Pluggable provider interface for prompt-based online acoustic archive discovery."""

    provider_id: str
    display_name: str
    description: str
    auth_required: bool = False
    auth_kind: str = "none"
    env_var: str | None = None
    auth_hint: str | None = None
    auth_docs_url: str | None = None

    def server_key(self) -> str | None:
        if not self.env_var:
            return None
        val = (os.environ.get(self.env_var) or "").strip()
        return val or None

    def resolve_key(self, provider_keys: dict[str, str]) -> str | None:
        user_key = (provider_keys.get(self.provider_id) or "").strip()
        return user_key or self.server_key()

    def info(self) -> DiscoveryProviderInfo:
        return DiscoveryProviderInfo(
            provider_id=self.provider_id,
            display_name=self.display_name,
            description=self.description,
            auth_required=self.auth_required,
            auth_kind=self.auth_kind,  # type: ignore[arg-type]
            auth_configured_on_server=bool(self.server_key()),
            auth_hint=self.auth_hint,
            auth_docs_url=self.auth_docs_url,
        )

    @abstractmethod
    async def search(
        self,
        prompt: str,
        terms: list[str],
        api_key: str | None,
        limit: int,
    ) -> list[DiscoveredSourceCandidate]:
        raise NotImplementedError


class CuratedCloudCatalogProvider(DiscoveryProvider):
    """Verified public bioacoustic S3, GCS, Zenodo, and Hugging Face archives."""

    provider_id = "open_catalog"
    display_name = "Open Bioacoustic Catalog"
    description = "Verified public AWS Open Data, GCS, terrestrial PAM, and multi-channel array archives"
    auth_required = False
    auth_kind = "none"

    _CATALOG: list[dict] = [
        {
            "id": "congo_rainforest_8khz",
            "title": "Cornell Central African Rainforest (Elephant Listening Project)",
            "description": "Continuous 8 kHz terrestrial rainforest WAV soundscapes from Nouabalé-Ndoki National Park, Congo (forest elephants, rainforest birds, primates, insects).",
            "homepageUrl": "https://registry.opendata.aws/congo8khz-pnnn/",
            "license": "CC-BY 4.0",
            "tags": ["terrestrial", "rainforest", "elephant", "africa", "birds", "primates", "soundscape", "pam", "wav", "s3"],
            "req": {
                "kind": "s3",
                "sourceId": "congo_rainforest",
                "displayName": "Cornell Congo Rainforest (8 kHz)",
                "bucket": "congo8khz-pnnn",
                "prefix": "recordings/",
                "region": "us-west-2",
                "anonymous": True,
                "readOnly": True,
            },
        },
        {
            "id": "hf_thewilds_pam",
            "title": "The Wilds Terrestrial Bioacoustic Monitors (Ohio PAM)",
            "description": "Continuous terrestrial passive acoustic WAV recordings from 4 Song Meter deployments across grassland/forest sites (birds, frogs, dawn chorus, mammals).",
            "homepageUrl": "https://huggingface.co/datasets/imageomics/thewilds_bioacousticmonitors",
            "license": "CC-BY 4.0",
            "tags": ["terrestrial", "bird", "birds", "birdsong", "frog", "dawn", "chorus", "soundscape", "pam", "ohio", "wav"],
            "req": {
                "kind": "huggingface",
                "sourceId": "hf_thewilds",
                "displayName": "The Wilds Terrestrial PAM",
                "folderOrUrl": "imageomics/thewilds_bioacousticmonitors",
                "anonymous": True,
                "readOnly": True,
            },
        },
        {
            "id": "zenodo_carsington_stereo",
            "title": "Carsington Water Bioacoustics (Stereo 2-ch Terrestrial SM2)",
            "description": "Stereo 2-channel terrestrial Wildlife Acoustics SM2 WAV recordings across Reedbed, Woodland, Scrub, and Plantation habitats in the UK (birdsong & wetland soundscapes).",
            "homepageUrl": "https://zenodo.org/records/2575467",
            "license": "CC-BY 4.0",
            "tags": ["terrestrial", "stereo", "multi-channel", "multichannel", "2-channel", "bird", "birdsong", "woodland", "wetland", "wav"],
            "req": {
                "kind": "zenodo",
                "sourceId": "zenodo_carsington",
                "displayName": "Carsington Water (2-ch Stereo)",
                "folderOrUrl": "2575467",
                "anonymous": True,
                "readOnly": True,
            },
        },
        {
            "id": "hf_chime3_6ch",
            "title": "CHiME-3 Multi-Channel Array Recordings (6-Mic Array WAV)",
            "description": "Uncompressed 16 kHz environmental WAV recordings from a 6-microphone tablet array (6 synchronized CH1–CH6 tracks per session) ideal for multi-channel spatial inspection.",
            "homepageUrl": "https://huggingface.co/datasets/chimechallenge/chime3-background-noise",
            "license": "CC-BY 4.0",
            "tags": ["multi-channel", "multichannel", "array", "6-channel", "microphone", "spatial", "environmental", "wav"],
            "req": {
                "kind": "huggingface",
                "sourceId": "hf_chime3_6ch",
                "displayName": "CHiME-3 6-Mic Array",
                "folderOrUrl": "chimechallenge/chime3-background-noise",
                "prefix": "CHiME3/data/audio/16kHz/backgrounds",
                "anonymous": True,
                "readOnly": True,
            },
        },
        {
            "id": "orcasound_clips",
            "title": "Orcasound Curated Vocalization Clips & Highlight Hours (AWS)",
            "description": "Curated WAV highlight recordings and labeled clips of Southern Resident killer whale pods, sperm whales, and Puget Sound marine soundscapes.",
            "homepageUrl": "https://registry.opendata.aws/orcasound/",
            "license": "CC-BY-SA 4.0",
            "tags": ["orca", "whale", "marine", "clips", "labeled", "ocean", "hydrophone", "wav", "s3"],
            "req": {
                "kind": "s3",
                "sourceId": "orcasound_clips",
                "displayName": "Orcasound Curated Clips (AWS)",
                "bucket": "acoustic-sandbox",
                "region": "us-west-2",
                "anonymous": True,
                "readOnly": True,
            },
        },
        {
            "id": "orcasound_archive",
            "title": "Orcasound Port Townsend Lossless Archive (FLAC, AWS)",
            "description": "Continuous lossless FLAC underwater hydrophone recordings from Port Townsend, Washington.",
            "homepageUrl": "https://registry.opendata.aws/orcasound/",
            "license": "CC-BY-SA 4.0",
            "tags": ["orca", "whale", "marine", "ocean", "flac", "hydrophone", "s3"],
            "req": {
                "kind": "s3",
                "sourceId": "orcasound_archive",
                "displayName": "Orcasound Port Townsend (FLAC)",
                "bucket": "archive-orcasound-net",
                "region": "us-west-2",
                "anonymous": True,
                "readOnly": True,
            },
        },
        {
            "id": "mbari_256khz_2023",
            "title": "MBARI Pacific Ocean Sound 256 kHz Ultrasonic Archive (2023)",
            "description": "Full-bandwidth 256 kHz deep-sea hydrophone WAV recordings capturing dolphin/beaked-whale echolocation clicks up to 128 kHz.",
            "homepageUrl": "https://docs.mbari.org/pacific-sound/",
            "license": "CC-BY 4.0",
            "tags": ["ultrasonic", "dolphin", "whale", "echolocation", "clicks", "marine", "ocean", "256khz", "s3"],
            "req": {
                "kind": "s3",
                "sourceId": "mbari_256khz_2023",
                "displayName": "MBARI Pacific 256 kHz (2023)",
                "bucket": "pacific-sound-256khz-2023",
                "region": "us-west-2",
                "anonymous": True,
                "readOnly": True,
            },
        },
        {
            "id": "noaa_sanctsound",
            "title": "NOAA NCEI SanctSound Sanctuary Soundscapes (GCS)",
            "description": "Multi-site U.S. National Marine Sanctuary passive acoustic recordings (whales, fish choruses, snapping shrimp, ocean weather).",
            "homepageUrl": "https://sanctsound.ioos.us/",
            "license": "Public Domain",
            "tags": ["marine", "ocean", "whale", "fish", "sanctuary", "noaa", "soundscape", "gcs"],
            "req": {
                "kind": "gcs",
                "sourceId": "noaa_sanctsound",
                "displayName": "NOAA SanctSound (GCS)",
                "bucket": "noaa-passive-bioacoustic",
                "prefix": "sanctsound/",
                "anonymous": True,
                "readOnly": True,
            },
        },
    ]

    async def search(
        self,
        prompt: str,
        terms: list[str],
        api_key: str | None,
        limit: int,
    ) -> list[DiscoveredSourceCandidate]:
        lower_prompt = prompt.lower()
        scored: list[tuple[int, dict]] = []
        for item in self._CATALOG:
            haystack = " ".join([item["title"], item["description"], *item["tags"]]).lower()
            score = 0
            for t in terms:
                if t in item["tags"]:
                    score += 4
                elif t in haystack:
                    score += 2
            if any(w in lower_prompt for w in ("multi-channel", "multichannel", "stereo", "array", "channel")):
                if any(tag in item["tags"] for tag in ("multi-channel", "stereo", "array")):
                    score += 5
            if any(w in lower_prompt for w in ("terrestrial", "bird", "forest", "land", "frog")):
                if "terrestrial" in item["tags"] or "bird" in item["tags"]:
                    score += 3
            if score > 0:
                scored.append((score, item))

        scored.sort(key=lambda x: x[0], reverse=True)
        out: list[DiscoveredSourceCandidate] = []
        for _score, item in scored[:limit]:
            out.append(
                DiscoveredSourceCandidate(
                    candidate_id=f"catalog:{item['id']}",
                    provider_id=self.provider_id,
                    provider_label=self.display_name,
                    title=item["title"],
                    description=item["description"],
                    homepage_url=item.get("homepageUrl"),
                    license=item.get("license"),
                    tags=item.get("tags", [])[:6],
                    auth_required=False,
                    auth_kind="none",
                    connect_request=CreateSourceRequest(**item["req"]),
                )
            )
        return out


class ZenodoDiscoveryProvider(DiscoveryProvider):
    """Live search across Zenodo open-access scientific audio datasets (zero API key)."""

    provider_id = "zenodo"
    display_name = "Zenodo Research Datasets"
    description = "Open-access scientific WAV/FLAC bioacoustic & multi-channel array datasets"
    auth_required = False
    auth_kind = "none"

    async def search(
        self,
        prompt: str,
        terms: list[str],
        api_key: str | None,
        limit: int,
    ) -> list[DiscoveredSourceCandidate]:
        query_terms = " ".join(terms[:5])
        if not any(w in query_terms.lower() for w in ("wav", "flac", "audio", "acoustic", "bioacoustic", "sound")):
            query_terms = f"{query_terms} audio wav"

        async with httpx.AsyncClient(follow_redirects=True, timeout=12.0) as client:
            resp = await client.get(
                "https://zenodo.org/api/records",
                params={
                    "q": query_terms,
                    "type": "dataset",
                    "access_right": "open",
                    "size": str(min(12, max(6, limit * 2))),
                },
                headers={"User-Agent": _BROWSER_UA},
            )
            resp.raise_for_status()
            hits = (resp.json().get("hits") or {}).get("hits") or []

        candidates: list[DiscoveredSourceCandidate] = []
        for hit in hits:
            rec_id = str(hit.get("id") or "")
            if not rec_id:
                continue
            meta = hit.get("metadata") or {}
            files = hit.get("files") or []
            audio_files = [
                f for f in files
                if Path(str(f.get("key") or "")).suffix.lower()
                in {".wav", ".wave", ".flac", ".ogg", ".mp3", ".aif", ".aiff"}
            ]
            zip_files = [
                f for f in files
                if Path(str(f.get("key") or "")).suffix.lower() in {".zip", ".tar", ".gz", ".tgz", ".7z"}
            ]
            if not audio_files and not zip_files:
                continue

            title = str(meta.get("title") or f"Zenodo {rec_id}").strip()
            raw_desc = re.sub(r"<[^>]+>", " ", str(meta.get("description") or ""))
            desc = re.sub(r"\s+", " ", raw_desc).strip()[:240]
            lic = (meta.get("license") or {}).get("id")
            homepage = (hit.get("links") or {}).get("self_html") or f"https://zenodo.org/records/{rec_id}"

            if audio_files:
                fmts = sorted(
                    {Path(str(f.get("key") or "")).suffix.lower().lstrip(".") for f in audio_files}
                )
                pre_ver = SourceVerificationResult(
                    status="untested",
                    streamable=True,
                    audio_file_count=len(audio_files),
                    formats=fmts,
                    sample_file_name=str(audio_files[0].get("key") or ""),
                    message=f"{len(audio_files)} streamable files ({', '.join(fmts[:3])})",
                )
            else:
                pre_ver = SourceVerificationResult(
                    status="zipped_only",
                    streamable=False,
                    audio_file_count=0,
                    formats=["zip"],
                    message="Packaged as downloadable archive (.zip/.tar) — open link to download & extract.",
                )

            candidates.append(
                DiscoveredSourceCandidate(
                    candidate_id=f"zenodo:{rec_id}",
                    provider_id=self.provider_id,
                    provider_label=self.display_name,
                    title=title,
                    description=desc or "Open-access scientific dataset on Zenodo.",
                    homepage_url=homepage,
                    license=str(lic) if lic else None,
                    tags=[*(meta.get("keywords") or [])[:4], "zenodo"],
                    auth_required=False,
                    auth_kind="none",
                    connect_request=CreateSourceRequest(
                        kind="zenodo",
                        source_id=slugify_source_id(title[:28], "zenodo"),
                        display_name=title[:48],
                        folder_or_url=rec_id,
                        anonymous=True,
                        read_only=True,
                    ),
                    verification=pre_ver,
                )
            )

        # Prefer datasets with loose streamable audio files over zipped-only
        candidates.sort(key=lambda c: (0 if c.verification.streamable else 1))
        return candidates[:limit]


class HuggingFaceDiscoveryProvider(DiscoveryProvider):
    """Live search across public Hugging Face audio datasets (zero API key)."""

    provider_id = "huggingface"
    display_name = "Hugging Face Datasets"
    description = "Public browsable audio & bioacoustic dataset repositories"
    auth_required = False
    auth_kind = "none"

    async def search(
        self,
        prompt: str,
        terms: list[str],
        api_key: str | None,
        limit: int,
    ) -> list[DiscoveredSourceCandidate]:
        search_q = " ".join(terms[:3])
        async with httpx.AsyncClient(follow_redirects=True, timeout=12.0) as client:
            resp = await client.get(
                "https://huggingface.co/api/datasets",
                params={
                    "search": search_q,
                    "sort": "downloads",
                    "direction": "-1",
                    "limit": str(min(10, max(4, limit * 2))),
                },
                headers={"User-Agent": _BROWSER_UA},
            )
            resp.raise_for_status()
            repos = resp.json()

        candidates: list[DiscoveredSourceCandidate] = []
        if not isinstance(repos, list):
            return candidates
        for repo in repos:
            repo_id = str(repo.get("id") or "")
            if not repo_id or repo.get("gated") or repo.get("private"):
                continue
            tags = [str(t) for t in (repo.get("tags") or []) if isinstance(t, str)]
            tag_blob = " ".join(tags).lower()
            desc = str(repo.get("description") or "").strip()
            # Keep audio-relevant repos
            if not any(
                k in f"{repo_id} {desc} {tag_blob}".lower()
                for k in ("audio", "sound", "acoustic", "bird", "whale", "speech", "bioacoustic", "wav", "noise", "frog", "bat")
            ):
                continue
            short_tags = [t.split(":", 1)[-1] for t in tags if not t.startswith(("region:", "size_categories:"))][:5]
            candidates.append(
                DiscoveredSourceCandidate(
                    candidate_id=f"hf:{repo_id}",
                    provider_id=self.provider_id,
                    provider_label=self.display_name,
                    title=repo_id,
                    description=desc[:220] or f"Public Hugging Face audio dataset ({repo.get('downloads', 0):,} downloads).",
                    homepage_url=f"https://huggingface.co/datasets/{repo_id}",
                    tags=short_tags,
                    auth_required=False,
                    auth_kind="none",
                    connect_request=CreateSourceRequest(
                        kind="huggingface",
                        source_id=slugify_source_id(repo_id, "hf"),
                        display_name=f"HF: {repo_id.split('/')[-1][:36]}",
                        folder_or_url=repo_id,
                        anonymous=True,
                        read_only=True,
                    ),
                )
            )
            if len(candidates) >= limit:
                break
        return candidates


class INaturalistDiscoveryProvider(DiscoveryProvider):
    """Live wildlife sound clip search via iNaturalist API (1.46M+ clips, zero API key)."""

    provider_id = "inaturalist"
    display_name = "iNaturalist Wildlife Sounds"
    description = "1.46M+ individual bird, frog, insect, and mammal audio clips worldwide"
    auth_required = False
    auth_kind = "none"

    @staticmethod
    def _infer_iconic_taxa(prompt: str) -> str | None:
        p = prompt.lower()
        if any(w in p for w in ("bird", "song", "sparrow", "warbler", "owl", "hawk", "finch", "thrush", "wren", "aves")):
            return "Aves"
        if any(w in p for w in ("frog", "toad", "amphibian", "anura")):
            return "Amphibia"
        if any(w in p for w in ("insect", "cricket", "cicada", "katydid", "grasshopper")):
            return "Insecta"
        if any(w in p for w in ("mammal", "whale", "wolf", "coyote", "bat", "elk", "primate", "monkey")):
            return "Mammalia"
        return None

    async def search(
        self,
        prompt: str,
        terms: list[str],
        api_key: str | None,
        limit: int,
    ) -> list[DiscoveredSourceCandidate]:
        iconic = self._infer_iconic_taxa(prompt)
        # Filter out generic acoustic/hardware words so iNaturalist matches species/habitat cleanly
        generic_skip = {
            "terrestrial", "multi-channel", "multichannel", "stereo", "array", "microphone",
            "individual", "soundscape", "soundscapes", "pam", "clips", "s3", "gcs", "flac",
            "bird", "birds", "birdsong", "frog", "frogs", "insect", "insects", "mammal", "mammals",
        }
        specific_terms = [t for t in terms if t not in generic_skip]
        q_str = " ".join(specific_terms[:3])

        params: dict[str, str] = {"sounds": "true", "per_page": "5", "order_by": "votes"}
        if q_str:
            params["q"] = q_str
        if iconic:
            params["iconic_taxa"] = iconic
        if not q_str and not iconic:
            # Default to birds/wildlife for broad terrestrial queries
            params["iconic_taxa"] = "Aves"
            iconic = "Aves"

        async with httpx.AsyncClient(follow_redirects=True, timeout=12.0) as client:
            resp = await client.get(
                "https://api.inaturalist.org/v1/observations",
                params=params,
                headers={"User-Agent": _BROWSER_UA},
            )
            resp.raise_for_status()
            data = resp.json()

        total = int(data.get("total_results") or 0)
        results = data.get("results") or []
        if total == 0 or not results:
            return []

        label_parts = []
        if q_str:
            label_parts.append(q_str.title())
        if iconic and iconic.lower() not in q_str.lower():
            label_parts.append(iconic)
        label = " · ".join(label_parts) or "Wildlife Sounds"

        sample_species = list(
            dict.fromkeys(
                [
                    str(
                        (r.get("taxon") or {}).get("preferred_common_name")
                        or (r.get("taxon") or {}).get("name")
                        or ""
                    )
                    for r in results
                    if (r.get("taxon") or {}).get("preferred_common_name")
                    or (r.get("taxon") or {}).get("name")
                ]
            )
        )[:4]

        conn_params: dict[str, str | int | float | bool] = {}
        if iconic:
            conn_params["iconicTaxa"] = iconic

        return [
            DiscoveredSourceCandidate(
                candidate_id=f"inat:{q_str or iconic or 'all'}",
                provider_id=self.provider_id,
                provider_label=self.display_name,
                title=f"iNaturalist Clips: {label} ({total:,} observations)",
                description=(
                    "Individual citizen-science wildlife recordings matching your search"
                    + (f" — e.g. {', '.join(sample_species)}." if sample_species else ".")
                ),
                homepage_url="https://www.inaturalist.org/observations?sounds=true",
                license="CC-BY / CC-BY-NC",
                tags=["clips", "individual", *(sample_species[:3])],
                auth_required=False,
                auth_kind="none",
                connect_request=CreateSourceRequest(
                    kind="inaturalist",
                    source_id=slugify_source_id(label, "inat"),
                    display_name=f"iNat: {label[:38]}",
                    query=q_str,
                    anonymous=True,
                    read_only=True,
                    params=conn_params,
                ),
            )
        ]


class XenoCantoDiscoveryProvider(DiscoveryProvider):
    """Xeno-canto API v3 wildlife sound clip search (optional free personal API key)."""

    provider_id = "xenocanto"
    display_name = "Xeno-canto (API v3)"
    description = "900k+ global bird, bat, frog, and grasshopper individual recordings"
    auth_required = True
    auth_kind = "api_key"
    env_var = "XENOCANTO_API_KEY"
    auth_hint = "Requires a free personal API key from your Xeno-canto account."
    auth_docs_url = "https://xeno-canto.org/explore/api"

    async def search(
        self,
        prompt: str,
        terms: list[str],
        api_key: str | None,
        limit: int,
    ) -> list[DiscoveredSourceCandidate]:
        q_str = " ".join(terms[:3]) or "q:A"
        req = CreateSourceRequest(
            kind="xenocanto",
            source_id=slugify_source_id(q_str, "xc"),
            display_name=f"Xeno-canto: {q_str[:32]}",
            query=q_str,
            api_key=api_key,
            anonymous=False,
            read_only=True,
        )

        if not api_key:
            return [
                DiscoveredSourceCandidate(
                    candidate_id=f"xenocanto:{q_str}",
                    provider_id=self.provider_id,
                    provider_label=self.display_name,
                    title=f"Xeno-canto Library: “{q_str}”",
                    description="Search 900,000+ high-quality bird, bat, amphibian, and insect recordings by species, call type, and quality rating.",
                    homepage_url="https://xeno-canto.org/",
                    license="Creative Commons",
                    tags=["birdsong", "clips", "species", "global"],
                    auth_required=True,
                    auth_kind="api_key",
                    auth_hint=self.auth_hint,
                    auth_docs_url=self.auth_docs_url,
                    connect_request=req,
                    verification=SourceVerificationResult(
                        status="auth_required",
                        streamable=False,
                        message="Add a free Xeno-canto API v3 key to search & stream.",
                    ),
                )
            ]

        async with httpx.AsyncClient(follow_redirects=True, timeout=12.0) as client:
            resp = await client.get(
                "https://xeno-canto.org/api/3/recordings",
                params={"query": q_str, "key": api_key, "per_page": "5"},
                headers={"User-Agent": _BROWSER_UA},
            )
            resp.raise_for_status()
            data = resp.json()

        num_rec = int(data.get("numRecordings") or 0)
        if num_rec == 0:
            return []
        return [
            DiscoveredSourceCandidate(
                candidate_id=f"xenocanto:{q_str}",
                provider_id=self.provider_id,
                provider_label=self.display_name,
                title=f"Xeno-canto: “{q_str}” ({num_rec:,} recordings)",
                description=f"Verified Xeno-canto v3 search returning {num_rec:,} wildlife recordings across {data.get('numSpecies', '?')} species.",
                homepage_url="https://xeno-canto.org/",
                license="Creative Commons",
                tags=["birdsong", "clips", "species"],
                auth_required=True,
                auth_kind="api_key",
                auth_hint=self.auth_hint,
                auth_docs_url=self.auth_docs_url,
                connect_request=req,
            )
        ]


class FreesoundDiscoveryProvider(DiscoveryProvider):
    """Freesound API v2 Creative Commons sound clip library (optional free developer API key)."""

    provider_id = "freesound"
    display_name = "Freesound"
    description = "Creative Commons field recordings, stereo soundscapes, and isolated sound clips"
    auth_required = True
    auth_kind = "api_key"
    env_var = "FREESOUND_API_KEY"
    auth_hint = "Requires a free developer API key from freesound.org/apiv2/apply."
    auth_docs_url = "https://freesound.org/apiv2/apply"

    async def search(
        self,
        prompt: str,
        terms: list[str],
        api_key: str | None,
        limit: int,
    ) -> list[DiscoveredSourceCandidate]:
        q_str = " ".join(terms[:4]) or prompt.strip()
        req = CreateSourceRequest(
            kind="freesound",
            source_id=slugify_source_id(q_str, "fs"),
            display_name=f"Freesound: {q_str[:32]}",
            query=q_str,
            api_key=api_key,
            anonymous=False,
            read_only=True,
        )

        if not api_key:
            return [
                DiscoveredSourceCandidate(
                    candidate_id=f"freesound:{q_str}",
                    provider_id=self.provider_id,
                    provider_label=self.display_name,
                    title=f"Freesound Clips: “{q_str}”",
                    description="Search Creative Commons field recordings, stereo nature soundscapes, and individual sound clips.",
                    homepage_url="https://freesound.org/",
                    license="Creative Commons",
                    tags=["clips", "field-recording", "creative-commons"],
                    auth_required=True,
                    auth_kind="api_key",
                    auth_hint=self.auth_hint,
                    auth_docs_url=self.auth_docs_url,
                    connect_request=req,
                    verification=SourceVerificationResult(
                        status="auth_required",
                        streamable=False,
                        message="Add a free Freesound API key to search & stream.",
                    ),
                )
            ]

        async with httpx.AsyncClient(follow_redirects=True, timeout=12.0) as client:
            resp = await client.get(
                "https://freesound.org/apiv2/search/text/",
                params={"query": q_str, "page_size": "5", "token": api_key},
                headers={"User-Agent": _BROWSER_UA},
            )
            resp.raise_for_status()
            data = resp.json()

        count = int(data.get("count") or 0)
        if count == 0:
            return []
        return [
            DiscoveredSourceCandidate(
                candidate_id=f"freesound:{q_str}",
                provider_id=self.provider_id,
                provider_label=self.display_name,
                title=f"Freesound: “{q_str}” ({count:,} clips)",
                description=f"Freesound search matching {count:,} Creative Commons audio clips.",
                homepage_url="https://freesound.org/",
                license="Creative Commons",
                tags=["clips", "creative-commons"],
                auth_required=True,
                auth_kind="api_key",
                auth_hint=self.auth_hint,
                auth_docs_url=self.auth_docs_url,
                connect_request=req,
            )
        ]


class DiscoveryRegistry:
    """Registry of pluggable archive discovery providers."""

    def __init__(self) -> None:
        self._providers: dict[str, DiscoveryProvider] = {}

    def register(self, provider: DiscoveryProvider) -> None:
        self._providers[provider.provider_id] = provider

    def list_info(self) -> list[DiscoveryProviderInfo]:
        return [p.info() for p in self._providers.values()]

    def providers(self, filter_ids: list[str] | None = None) -> list[DiscoveryProvider]:
        if not filter_ids:
            return list(self._providers.values())
        wanted = set(filter_ids)
        return [p for pid, p in self._providers.items() if pid in wanted]


def build_default_discovery_registry() -> DiscoveryRegistry:
    reg = DiscoveryRegistry()
    reg.register(CuratedCloudCatalogProvider())
    reg.register(ZenodoDiscoveryProvider())
    reg.register(HuggingFaceDiscoveryProvider())
    reg.register(INaturalistDiscoveryProvider())
    reg.register(XenoCantoDiscoveryProvider())
    reg.register(FreesoundDiscoveryProvider())
    return reg


async def discover_online_sources(
    registry: DiscoveryRegistry,
    req: DiscoverSourcesRequest,
) -> DiscoverSourcesResponse:
    prompt = req.prompt.strip()
    if not prompt:
        return DiscoverSourcesResponse(prompt="", warnings=["Enter a search prompt to discover archives."])

    terms = await _expand_prompt_terms(prompt, req.gemini_api_key)
    active_providers = registry.providers(req.provider_ids)
    per_provider_limit = max(2, min(6, (req.limit // max(1, len(active_providers))) + 2))

    warnings: list[str] = []

    async def _run_provider(prov: DiscoveryProvider) -> list[DiscoveredSourceCandidate]:
        key = prov.resolve_key(req.provider_keys)
        try:
            return await asyncio.wait_for(
                prov.search(prompt, terms, key, per_provider_limit),
                timeout=12.0,
            )
        except Exception as exc:
            warnings.append(f"{prov.display_name}: {exc}")
            return []

    raw_lists = await asyncio.gather(*(_run_provider(p) for p in active_providers))

    # Deduplicate by candidate_id and homepage_url
    candidates: list[DiscoveredSourceCandidate] = []
    seen_ids: set[str] = set()
    seen_urls: set[str] = set()
    for sublist in raw_lists:
        for cand in sublist:
            if cand.candidate_id in seen_ids:
                continue
            if cand.homepage_url and cand.homepage_url in seen_urls:
                continue
            seen_ids.add(cand.candidate_id)
            if cand.homepage_url:
                seen_urls.add(cand.homepage_url)
            candidates.append(cand)

    # Live-verify top candidates that aren't already marked zipped_only / auth_required
    to_verify = [
        c for c in candidates if c.verification.status == "untested"
    ][: req.verify_top_n]

    async def _verify_one(cand: DiscoveredSourceCandidate) -> None:
        try:
            src = build_source_from_request(cand.connect_request)
            cand.verification = await verify_audio_source(src, timeout_sec=10.0)
        except Exception as exc:
            cand.verification = SourceVerificationResult(
                status="error",
                streamable=False,
                message=str(exc),
            )

    if to_verify:
        await asyncio.gather(*(_verify_one(c) for c in to_verify))

    # Sort: verified first, then untested streamable, then auth_required, then zipped_only/incompatible/error
    rank_map = {
        "verified": 0,
        "untested": 1,
        "auth_required": 2,
        "zipped_only": 3,
        "incompatible": 4,
        "error": 5,
    }
    candidates.sort(key=lambda c: (rank_map.get(c.verification.status, 9), -c.verification.audio_file_count))

    return DiscoverSourcesResponse(
        prompt=prompt,
        normalized_terms=terms,
        providers_queried=[p.provider_id for p in active_providers],
        candidates=candidates[: req.limit],
        warnings=warnings,
    )
