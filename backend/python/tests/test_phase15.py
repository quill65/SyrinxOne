from __future__ import annotations

import asyncio
import io
from unittest.mock import AsyncMock, patch

import httpx
import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient

from syrinx_backend.api.app import create_app
from syrinx_backend.audio.loader import AudioSessionCache, _parse_wav_header
from syrinx_backend.storage.base import AudioDataSource, SourceRegistry
from syrinx_backend.storage.cloud_adapters import (
    GCSAudioDataSource,
    GoogleDriveAudioDataSource,
    S3AudioDataSource,
    parse_google_drive_id,
)


def _make_tone_wav_bytes(
    freq_hz: float = 2000.0,
    duration_sec: float = 2.0,
    sr: int = 16000,
) -> bytes:
    t = np.arange(int(duration_sec * sr), dtype=np.float32) / sr
    sig = 0.25 * np.sin(2.0 * np.pi * freq_hz * t)
    buf = io.BytesIO()
    sf.write(buf, sig, sr, format="WAV", subtype="PCM_16")
    return buf.getvalue()


def test_default_registry_includes_mbari_and_noaa_archives() -> None:
    app = create_app()
    client = TestClient(app)
    resp = client.get("/api/sources")
    assert resp.status_code == 200
    sources = resp.json()
    ids = {s["sourceId"] for s in sources}
    assert "local" in ids
    assert "http" in ids
    # Hardcoded permanent archives are no longer forced into the default source list
    assert "mbari_16khz" not in ids
    assert "noaa_ncei" not in ids


def test_cloud_safe_mode_disables_local_fs_source() -> None:
    with patch.dict("os.environ", {"SYRINX_DISABLE_LOCAL_FS": "1"}, clear=False):
        app = create_app()
    client = TestClient(app)
    resp = client.get("/api/sources")
    assert resp.status_code == 200
    ids = {s["sourceId"] for s in resp.json()}
    assert "local" not in ids
    assert "http" in ids


def test_runtime_source_create_and_delete() -> None:
    app = create_app()
    client = TestClient(app)

    create_resp = client.post(
        "/api/sources",
        json={
            "kind": "s3",
            "bucket": "s3://pacific-sound-256khz-2022/2022",
            "region": "us-west-2",
            "displayName": "MBARI 256 kHz (2022)",
            "anonymous": True,
        },
    )
    assert create_resp.status_code == 200
    created = create_resp.json()
    assert created["kind"] == "s3"
    assert created["anonymous"] is True
    assert created["removable"] is True
    sid = created["sourceId"]

    # Google Drive shared folder source
    gd_resp = client.post(
        "/api/sources",
        json={
            "kind": "gdrive",
            "folderOrUrl": "https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOpQrStUvWxYz",
            "displayName": "Field Recordings Drive",
        },
    )
    assert gd_resp.status_code == 200
    gd_info = gd_resp.json()
    assert gd_info["kind"] == "gdrive"
    assert gd_info["rootPath"] == "1AbCdEfGhIjKlMnOpQrStUvWxYz"

    # Delete removable source succeeds; built-in transport source is protected
    del_resp = client.delete(f"/api/sources/{sid}")
    assert del_resp.status_code == 200
    assert client.delete("/api/sources/local").status_code == 403


def test_parse_google_drive_urls() -> None:
    fid, is_dir = parse_google_drive_id(
        "https://drive.google.com/file/d/1XyZ9876543210AbCdEf/view?usp=sharing"
    )
    assert fid == "1XyZ9876543210AbCdEf"
    assert is_dir is False

    did, is_dir2 = parse_google_drive_id(
        "https://drive.google.com/drive/folders/1FolderId9876543210?usp=drive_link"
    )
    assert did == "1FolderId9876543210"
    assert is_dir2 is True


def test_anonymous_s3_and_gcs_listing_parsers() -> None:
    s3_xml = """<?xml version="1.0" encoding="UTF-8"?>
    <ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
      <Name>pacific-sound-16khz</Name>
      <Prefix>2023/</Prefix>
      <CommonPrefixes><Prefix>2023/01/</Prefix></CommonPrefixes>
      <Contents>
        <Key>2023/MARS_20230101_000000_16kHz.wav</Key>
        <Size>1234567</Size>
      </Contents>
    </ListBucketResult>
    """
    s3_resp = httpx.Response(200, text=s3_xml, request=httpx.Request("GET", "https://example.com"))
    s3 = S3AudioDataSource(
        source_id="mbari_test", bucket="pacific-sound-16khz", region="us-west-2", anonymous=True
    )
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=s3_resp)):
        items = asyncio.run(s3.list_items("2023/"))
    names = [i.name for i in items]
    assert ".." in names
    assert "01" in names
    assert "MARS_20230101_000000_16kHz.wav" in names

    gcs_json = {
        "prefixes": ["sanctsound/audio/"],
        "items": [{"name": "sanctsound/sample.flac", "size": "987654"}],
    }
    gcs_resp = httpx.Response(200, json=gcs_json, request=httpx.Request("GET", "https://example.com"))
    gcs = GCSAudioDataSource(
        source_id="noaa_test", bucket="noaa-passive-bioacoustic", anonymous=True
    )
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=gcs_resp)):
        g_items = asyncio.run(gcs.list_items("sanctsound/"))
    g_names = [i.name for i in g_items]
    assert "audio" in g_names
    assert "sample.flac" in g_names


def test_gdrive_public_embedded_folder_listing() -> None:
    sample_html = (
        '<a href="https://drive.google.com/file/d/1AudioFile123456789/view">'
        '<div class="flip-entry-title">humpback_clip.wav</div></a>'
    )
    resp = httpx.Response(200, text=sample_html, request=httpx.Request("GET", "https://drive.google.com"))
    gd = GoogleDriveAudioDataSource(
        source_id="gd1",
        folder_or_url="https://drive.google.com/drive/folders/1SharedFolder1234567",
    )
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=resp)):
        items = asyncio.run(gd.list_items())
    assert len(items) == 1
    assert items[0].name == "humpback_clip.wav"
    assert items[0].kind == "audio"

    single = GoogleDriveAudioDataSource(
        source_id="gd_single",
        folder_or_url="https://drive.google.com/file/d/1SingleFile123456789/view",
        display_name="song_sparrow_take3.wav",
    )
    single_items = asyncio.run(single.list_items())
    assert len(single_items) == 1
    assert single_items[0].name == "song_sparrow_take3.wav"
    assert single_items[0].uri == "gdrive://1SingleFile123456789/song_sparrow_take3.wav"


def test_remote_wav_range_window_loader() -> None:
    wav_bytes = _make_tone_wav_bytes(freq_hz=1500.0, duration_sec=4.0, sr=16000)
    hdr = _parse_wav_header(wav_bytes[:1024])
    assert hdr is not None
    assert hdr.sample_rate == 16000
    assert hdr.total_duration_sec == pytest.approx(4.0, abs=0.01)

    class RangeOnlySource(AudioDataSource):
        source_id = "range_mock"
        display_name = "Range Mock"
        kind = "http"
        read_only = True

        def __init__(self, payload: bytes) -> None:
            self._payload = payload
            self.range_calls = 0

        async def list_items(self, prefix: str = "", limit: int = 250):
            return []

        async def read_bytes(self, uri: str) -> bytes:
            raise AssertionError("Should use range reads, not full download!")

        async def read_byte_range(self, uri: str, start: int, end_inclusive: int):
            self.range_calls += 1
            return self._payload[start : end_inclusive + 1], len(self._payload)

        async def write_bytes(self, uri: str, data: bytes) -> str:
            raise PermissionError("read-only")

    src = RangeOnlySource(wav_bytes)
    cache = AudioSessionCache()
    loaded = asyncio.run(
        cache.open_from_source(src, "ocean_clip.wav", start_sec=1.0, max_duration_sec=1.5)
    )
    assert src.range_calls == 2  # Header probe + requested PCM window only
    assert loaded.info.is_partial_window is True
    assert loaded.info.window_start_sec == pytest.approx(1.0)
    assert loaded.info.duration_sec == pytest.approx(1.5, abs=0.02)
    assert loaded.info.total_duration_sec == pytest.approx(4.0, abs=0.02)


def test_measure_selection_and_ai_explorer_offline_and_gemini() -> None:
    app = create_app(registry=SourceRegistry())
    client = TestClient(app)

    wav_bytes = _make_tone_wav_bytes(freq_hz=2000.0, duration_sec=2.0, sr=16000)
    up = client.post(
        "/api/audio/upload",
        files={"file": ("tone_2khz.wav", wav_bytes, "audio/wav")},
    )
    assert up.status_code == 200
    file_id = up.json()["fileId"]

    # 1. Server-side acoustic measurement accuracy
    meas_resp = client.post(
        f"/api/audio/{file_id}/measure",
        json={
            "channel": 0,
            "startSec": 0.4,
            "endSec": 1.6,
            "lowFreqHz": 1000.0,
            "highFreqHz": 3500.0,
        },
    )
    assert meas_resp.status_code == 200
    meas = meas_resp.json()
    assert meas["peakFreqHz"] == pytest.approx(2000.0, abs=40.0)
    assert meas["centroidFreqHz"] == pytest.approx(2000.0, abs=80.0)
    assert meas["durationSec"] == pytest.approx(1.2, abs=0.02)
    assert meas["bw90Hz"] < 250.0

    # 2. AI explorer offline DSP fallback (no API key)
    with patch.dict("os.environ", {"GEMINI_API_KEY": ""}, clear=False):
        ai_off = client.post(
            "/api/ai/analyze",
            json={
                "fileId": file_id,
                "channel": 0,
                "prompt": "What is in this selection?",
                "selection": {
                    "startSec": 0.4,
                    "endSec": 1.6,
                    "lowFreqHz": 1000.0,
                    "highFreqHz": 3500.0,
                },
            },
        )
    assert ai_off.status_code == 200
    off_data = ai_off.json()
    assert off_data["modelUsed"] == "offline-dsp"
    assert off_data["measurements"]["peakFreqHz"] == pytest.approx(2000.0, abs=40.0)
    assert off_data["suggestedDetectParams"] is not None

    # 3. AI explorer with mocked Gemini multimodal response & syrinx_json extraction
    mock_gemini_body = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {
                            "text": (
                                "This is a clean tonal 2 kHz narrowband signal.\n\n"
                                "```syrinx_json\n"
                                '{"suggestedLabels": ["Tonal 2kHz", "Whistle"], '
                                '"suggestedDetectParams": {"minFreqHz": 1800, "maxFreqHz": 2200, '
                                '"triggerThresholdDb": 10, "minSoundDurSec": 0.1, '
                                '"maxSilenceGapSec": 0.2, "comment": "Tonal2k"}}\n'
                                "```"
                            )
                        }
                    ]
                }
            }
        ]
    }
    mock_resp = httpx.Response(
        200, json=mock_gemini_body, request=httpx.Request("POST", "https://generativelanguage.googleapis.com")
    )
    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=mock_resp)):
        ai_on = client.post(
            "/api/ai/analyze",
            json={
                "fileId": file_id,
                "channel": 0,
                "prompt": "Identify this call and recommend detector settings.",
                "apiKey": "fake-key",
                "selection": {
                    "startSec": 0.4,
                    "endSec": 1.6,
                    "lowFreqHz": 1000.0,
                    "highFreqHz": 3500.0,
                },
            },
        )
    assert ai_on.status_code == 200
    on_data = ai_on.json()
    assert "2 kHz narrowband" in on_data["answer"]
    assert "```syrinx_json" not in on_data["answer"]
    assert on_data["suggestedLabels"] == ["Tonal 2kHz", "Whistle"]
    assert on_data["suggestedDetectParams"]["minFreqHz"] == 1800
    assert on_data["suggestedDetectParams"]["comment"] == "Tonal2k"
