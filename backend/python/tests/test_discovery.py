from __future__ import annotations

import asyncio
import io
from typing import Any

import numpy as np
import soundfile as sf
from fastapi.testclient import TestClient

from syrinx_backend.api.app import create_app
from syrinx_backend.models import (
    BrowseEntry,
    CreateSourceRequest,
    DiscoveredSourceCandidate,
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
from syrinx_backend.storage.base import AudioDataSource, SourceRegistry
from syrinx_backend.storage.discovery import (
    DiscoveryProvider,
    DiscoveryRegistry,
    build_source_from_request,
    verify_audio_source,
)


def _make_wav_bytes(sr: int = 44100, channels: int = 2, duration_sec: float = 0.05) -> bytes:
    n = int(sr * duration_sec)
    t = np.linspace(0.0, duration_sec, n, endpoint=False, dtype=np.float32)
    sig = 0.25 * np.sin(2.0 * np.pi * 1000.0 * t)
    arr = np.stack([sig for _ in range(channels)], axis=-1)
    buf = io.BytesIO()
    sf.write(buf, arr, sr, format="WAV", subtype="PCM_16")
    return buf.getvalue()


class _FakeStreamableSource(AudioDataSource):
    kind = "http"
    anonymous = True
    read_only = True
    removable = True

    def __init__(self, source_id: str = "fake_wav", wav_bytes: bytes | None = None) -> None:
        self.source_id = source_id
        self.display_name = "Fake Streamable Archive"
        self._wav = wav_bytes or _make_wav_bytes(sr=48000, channels=4)

    async def list_items(self, prefix: str = "", limit: int = 250) -> list[BrowseEntry]:
        if not prefix:
            return [
                BrowseEntry(name="site_a/", uri="site_a/", is_dir=True, kind="other"),
            ]
        return [
            BrowseEntry(
                name="array_4ch.wav",
                uri="site_a/array_4ch.wav",
                is_dir=False,
                size_bytes=len(self._wav),
                kind="audio",
            )
        ]

    async def read_bytes(self, uri: str) -> bytes:
        return self._wav

    async def read_byte_range(
        self, uri: str, start: int, end_inclusive: int
    ) -> tuple[bytes, int | None]:
        return self._wav[start : end_inclusive + 1], len(self._wav)

    async def write_bytes(self, uri: str, data: bytes) -> str:
        raise PermissionError("read-only")


class _FakeZippedOnlySource(AudioDataSource):
    kind = "zenodo"
    anonymous = True
    read_only = True
    removable = True

    def __init__(self) -> None:
        self.source_id = "fake_zip"
        self.display_name = "Fake Zipped Archive"

    async def list_items(self, prefix: str = "", limit: int = 250) -> list[BrowseEntry]:
        return [
            BrowseEntry(
                name="recordings_bundle.zip",
                uri="https://example.org/recordings_bundle.zip",
                is_dir=False,
                size_bytes=50_000_000,
                kind="other",
            )
        ]

    async def read_bytes(self, uri: str) -> bytes:
        return b""

    async def write_bytes(self, uri: str, data: bytes) -> str:
        raise PermissionError("read-only")


class _FakeAuthRequiredSource(AudioDataSource):
    kind = "xenocanto"
    anonymous = False
    read_only = True
    removable = True

    def __init__(self) -> None:
        self.source_id = "fake_auth"
        self.display_name = "Fake Auth Archive"

    async def list_items(self, prefix: str = "", limit: int = 250) -> list[BrowseEntry]:
        raise PermissionError("Xeno-canto API v3 requires a personal API key")

    async def read_bytes(self, uri: str) -> bytes:
        raise PermissionError("API key required")

    async def write_bytes(self, uri: str, data: bytes) -> str:
        raise PermissionError("read-only")


class _MockOpenProvider(DiscoveryProvider):
    provider_id = "mock_open"
    display_name = "Mock Open Catalog"
    description = "Mock open archive provider"
    auth_required = False
    auth_kind = "none"

    async def search(
        self,
        prompt: str,
        terms: list[str],
        api_key: str | None,
        limit: int,
    ) -> list[DiscoveredSourceCandidate]:
        return [
            DiscoveredSourceCandidate(
                candidate_id="mock_open:dataset_1",
                provider_id=self.provider_id,
                provider_label=self.display_name,
                title="Mock Birdsong Dataset",
                description="Matched prompt: " + prompt,
                license="CC-BY-4.0",
                tags=["bird", "terrestrial"],
                connect_request=CreateSourceRequest(
                    kind="http",
                    source_id="mock_bird_1",
                    display_name="Mock Birdsong Dataset",
                    folder_or_url="https://example.org/bird.wav",
                ),
                verification=SourceVerificationResult(
                    status="verified",
                    streamable=True,
                    audio_file_count=5,
                    sample_rate_hz=44100,
                    num_channels=2,
                    formats=["WAV"],
                    message="Pre-verified in mock",
                ),
            )
        ]


class _MockKeyProvider(DiscoveryProvider):
    provider_id = "mock_keyed"
    display_name = "Mock Keyed Library"
    description = "Mock archive requiring an API key"
    auth_required = True
    auth_kind = "api_key"
    auth_hint = "Provide a personal token"
    auth_docs_url = "https://example.org/api-key"
    env_var_name = "MOCK_KEYED_TEST_API_KEY"

    async def search(
        self,
        prompt: str,
        terms: list[str],
        api_key: str | None,
        limit: int,
    ) -> list[DiscoveredSourceCandidate]:
        if not api_key:
            return [
                DiscoveredSourceCandidate(
                    candidate_id="mock_keyed:need_key",
                    provider_id=self.provider_id,
                    provider_label=self.display_name,
                    title=f"Mock Keyed Search: {prompt}",
                    description="Requires API key",
                    auth_required=True,
                    connect_request=CreateSourceRequest(
                        kind="xenocanto",
                        query=prompt,
                    ),
                    verification=SourceVerificationResult(
                        status="auth_required",
                        streamable=False,
                        message="API key required to query this provider.",
                    ),
                )
            ]
        return [
            DiscoveredSourceCandidate(
                candidate_id="mock_keyed:unlocked",
                provider_id=self.provider_id,
                provider_label=self.display_name,
                title=f"Unlocked Keyed Search ({api_key[:4]}): {prompt}",
                description="Authenticated search succeeded",
                auth_required=True,
                connect_request=CreateSourceRequest(
                    kind="xenocanto",
                    query=prompt,
                    api_key=api_key,
                ),
                verification=SourceVerificationResult(
                    status="verified",
                    streamable=True,
                    audio_file_count=12,
                    formats=["MP3"],
                    message="Authenticated search returned 12 clips",
                ),
            )
        ]


def test_build_source_from_request_all_archive_kinds() -> None:
    z = build_source_from_request(
        CreateSourceRequest(kind="zenodo", params={"recordId": "2575467"}, display_name="Carsington")
    )
    assert isinstance(z, ZenodoAudioDataSource)
    assert z.record_id == "2575467"

    hf = build_source_from_request(
        CreateSourceRequest(
            kind="huggingface",
            folder_or_url="https://huggingface.co/datasets/chimechallenge/chime3-background-noise",
        )
    )
    assert isinstance(hf, HuggingFaceAudioDataSource)
    assert hf.repo_id == "chimechallenge/chime3-background-noise"

    inat = build_source_from_request(
        CreateSourceRequest(kind="inaturalist", query="turdus migratorius")
    )
    assert isinstance(inat, INaturalistAudioDataSource)
    assert inat.query == "turdus migratorius"

    xc = build_source_from_request(
        CreateSourceRequest(kind="xenocanto", query="thrush", api_key="xc_test_key")
    )
    assert isinstance(xc, XenoCantoAudioDataSource)
    assert xc.api_key == "xc_test_key"

    fs = build_source_from_request(
        CreateSourceRequest(kind="freesound", query="forest wind", api_key="fs_test_key")
    )
    assert isinstance(fs, FreesoundAudioDataSource)
    assert fs.api_key == "fs_test_key"


def test_verify_audio_source_states() -> None:
    # 1. Nested directory with 4-channel 48 kHz WAV -> verified with WAV header metadata
    res_ok = asyncio.run(verify_audio_source(_FakeStreamableSource()))
    assert res_ok.status == "verified"
    assert res_ok.streamable is True
    assert res_ok.audio_file_count == 1
    assert res_ok.sample_rate_hz == 48000
    assert res_ok.num_channels == 4
    assert res_ok.supports_range is True
    assert "wav" in res_ok.formats

    # 2. Zip-only archive -> zipped_only
    res_zip = asyncio.run(verify_audio_source(_FakeZippedOnlySource()))
    assert res_zip.status == "zipped_only"
    assert res_zip.streamable is False
    assert "zip" in res_zip.formats

    # 3. PermissionError -> auth_required
    res_auth = asyncio.run(verify_audio_source(_FakeAuthRequiredSource()))
    assert res_auth.status == "auth_required"
    assert res_auth.streamable is False


def test_discovery_api_endpoints_and_provider_keys() -> None:
    disc_reg = DiscoveryRegistry()
    disc_reg.register(_MockOpenProvider())
    disc_reg.register(_MockKeyProvider())

    app = create_app(registry=SourceRegistry(), discovery_registry=disc_reg)
    client = TestClient(app)

    # 1. GET /api/sources/providers returns generic metadata schema
    resp_p = client.get("/api/sources/providers")
    assert resp_p.status_code == 200
    provs: list[dict[str, Any]] = resp_p.json()
    assert len(provs) == 2
    by_id = {p["providerId"]: p for p in provs}
    assert by_id["mock_open"]["authKind"] == "none"
    assert by_id["mock_keyed"]["authRequired"] is True
    assert by_id["mock_keyed"]["authKind"] == "api_key"
    assert by_id["mock_keyed"]["authDocsUrl"] == "https://example.org/api-key"

    # 2. POST /api/sources/discover without API key ranks verified first, includes auth_required
    resp_d1 = client.post(
        "/api/sources/discover",
        json={"prompt": "terrestrial birdsong", "verifyTopN": 0},
    )
    assert resp_d1.status_code == 200
    data1 = resp_d1.json()
    assert len(data1["candidates"]) == 2
    assert data1["candidates"][0]["verification"]["status"] == "verified"
    assert data1["candidates"][1]["verification"]["status"] == "auth_required"

    # 3. POST /api/sources/discover with browser-supplied providerKeys unlocks keyed provider
    resp_d2 = client.post(
        "/api/sources/discover",
        json={
            "prompt": "terrestrial birdsong",
            "providerKeys": {"mock_keyed": "secret_12345"},
            "verifyTopN": 0,
        },
    )
    assert resp_d2.status_code == 200
    data2 = resp_d2.json()
    assert len(data2["candidates"]) == 2
    assert all(c["verification"]["status"] == "verified" for c in data2["candidates"])
    assert any("secr" in c["title"] for c in data2["candidates"])

    # 4. POST /api/sources creates generic archive source dynamically
    resp_create = client.post(
        "/api/sources",
        json={"kind": "inaturalist", "query": "wood thrush", "displayName": "Wood Thrush Clips"},
    )
    assert resp_create.status_code == 200
    created = resp_create.json()
    assert created["kind"] == "inaturalist"
    assert created["displayName"] == "Wood Thrush Clips"
    assert created["removable"] is True


def test_default_app_exposes_all_builtin_discovery_providers() -> None:
    app = create_app(registry=SourceRegistry())
    client = TestClient(app)
    resp = client.get("/api/sources/providers")
    assert resp.status_code == 200
    providers = [DiscoveryProviderInfo.model_validate(item) for item in resp.json()]
    ids = {p.provider_id for p in providers}
    assert {"open_catalog", "zenodo", "huggingface", "inaturalist", "xenocanto", "freesound"} <= ids


class _DeepNestedSource(AudioDataSource):
    kind = "huggingface"
    anonymous = True
    read_only = True
    removable = True

    def __init__(self, depth_to_audio: int) -> None:
        self.source_id = f"deep_{depth_to_audio}"
        self.display_name = "Deep Nested Archive"
        self._depth = depth_to_audio
        self._wav = _make_wav_bytes(sr=16000, channels=1)

    async def list_items(self, prefix: str = "", limit: int = 250) -> list[BrowseEntry]:
        level = len([p for p in prefix.strip("/").split("/") if p])
        if level < self._depth:
            nxt = f"{prefix.rstrip('/')}/d{level + 1}".lstrip("/")
            return [BrowseEntry(name=f"d{level + 1}", uri=nxt, is_dir=True, kind="other")]
        return [
            BrowseEntry(
                name="track.CH1.wav",
                uri=f"{prefix.rstrip('/')}/track.CH1.wav",
                is_dir=False,
                size_bytes=len(self._wav),
                kind="audio",
            )
        ]

    async def read_bytes(self, uri: str) -> bytes:
        return self._wav

    async def read_byte_range(
        self, uri: str, start: int, end_inclusive: int
    ) -> tuple[bytes, int | None]:
        return self._wav[start : end_inclusive + 1], len(self._wav)

    async def write_bytes(self, uri: str, data: bytes) -> str:
        raise PermissionError("read-only")


def test_verify_audio_source_deep_nested_trees() -> None:
    # 5 levels deep (like CHiME-3 without prefix) -> verified
    res_5 = asyncio.run(verify_audio_source(_DeepNestedSource(5)))
    assert res_5.status == "verified"
    assert res_5.sample_rate_hz == 16000

    # Deeper than 7 levels -> untested (never falsely locked out as incompatible)
    res_9 = asyncio.run(verify_audio_source(_DeepNestedSource(9)))
    assert res_9.status == "untested"

