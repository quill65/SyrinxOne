from __future__ import annotations

from pathlib import Path

import numpy as np
import soundfile as sf
from fastapi.testclient import TestClient

from syrinx_backend.api.app import create_app
from syrinx_backend.storage.base import SourceRegistry
from syrinx_backend.storage.local_fs import LocalFilesystemSource

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _make_test_client(tmp_path: Path, api_token: str | None = None) -> TestClient:
    registry = SourceRegistry()
    registry.register(
        LocalFilesystemSource(
            source_id="local",
            display_name="Test Local",
            root_dir=tmp_path,
            restrict_to_root=True,
        )
    )
    app = create_app(registry=registry, api_token=api_token)
    return TestClient(app)


def _write_synthetic_wav(path: Path, sr: int = 22050, duration: float = 0.5) -> None:
    t = np.arange(int(sr * duration), dtype=np.float32) / sr
    ch1 = 0.5 * np.sin(2.0 * np.pi * 2000.0 * t)
    ch2 = 0.4 * np.sin(2.0 * np.pi * 4500.0 * t)
    stereo = np.stack([ch1, ch2], axis=-1).astype(np.float32)
    sf.write(str(path), stereo, sr, subtype="PCM_16")


def test_full_audio_spectrogram_clip_and_annotation_api(tmp_path: Path) -> None:
    wav_path = tmp_path / "stereo_test.wav"
    _write_synthetic_wav(wav_path)

    client = _make_test_client(tmp_path, api_token="secret-token")

    # Health is public; protected endpoints enforce Bearer token
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/sources").status_code == 401

    headers = {"Authorization": "Bearer secret-token"}
    sources = client.get("/api/sources", headers=headers).json()
    assert len(sources) == 1
    assert sources[0]["sourceId"] == "local"

    # Open stereo WAV
    open_resp = client.post(
        "/api/audio/open",
        json={"sourceId": "local", "uri": str(wav_path)},
        headers=headers,
    )
    assert open_resp.status_code == 200
    info = open_resp.json()
    assert info["numChannels"] == 2
    assert info["sampleRate"] == 22050
    file_id = info["fileId"]

    # Waveform overview
    wf = client.get(
        f"/api/audio/{file_id}/waveform?channel=0&num_bins=100", headers=headers
    ).json()
    assert wf["numBins"] == 100
    assert len(wf["minValues"]) == 100

    # Binary Float32 spectrogram (Modern STFT)
    spec_resp = client.get(
        f"/api/audio/{file_id}/spectrogram?channel=0&fft_size=512&hop_size=128&dsp_mode=modern_stft",
        headers=headers,
    )
    assert spec_resp.status_code == 200
    t_bins = int(spec_resp.headers["X-Spec-Time-Bins"])
    f_bins = int(spec_resp.headers["X-Spec-Freq-Bins"])
    arr = np.frombuffer(spec_resp.content, dtype="<f4").reshape(t_bins, f_bins)
    assert arr.shape == (t_bins, f_bins)

    # PNG Bitmap fallback endpoint
    png_resp = client.get(
        f"/api/audio/{file_id}/spectrogram.png?channel=1&dsp_mode=modern_stft&colormap=grayscale_bow&token=secret-token"
    )
    assert png_resp.status_code == 200
    assert png_resp.headers["content-type"] == "image/png"
    assert png_resp.content[:4] == b"\x89PNG"

    # Audio stream for WebAudio playback
    stream_resp = client.get(f"/api/audio/{file_id}/stream?token=secret-token")
    assert stream_resp.status_code == 200
    assert stream_resp.content[:4] == b"RIFF"

    # Clip-and-Save with open_in_window=True
    clip_resp = client.post(
        "/api/audio/clip-save",
        json={
            "fileId": file_id,
            "startSec": 0.1,
            "endSec": 0.3,
            "channels": [0],
            "targetSourceId": "local",
            "targetDirectory": str(tmp_path),
            "nameTemplate": "%f_%c_%ch",
            "comment": "SongA",
            "openInWindow": True,
        },
        headers=headers,
    )
    assert clip_resp.status_code == 200
    clip_data = clip_resp.json()
    assert clip_data["savedName"] == "stereo_test_SongA_ch1.wav"
    assert Path(clip_data["savedUri"]).is_file()
    assert abs(clip_data["fileInfo"]["durationSec"] - 0.2) < 0.01

    # Save & re-import Syrinx annotation log via storage source
    ann_uri = str(tmp_path / "test_log.txt")
    save_ann = client.post(
        "/api/annotations/save-source",
        json={
            "sourceId": "local",
            "uri": ann_uri,
            "format": "syrinx_txt",
            "annotations": [
                {
                    "id": "1",
                    "soundfile": "stereo_test.wav",
                    "channel": 1,
                    "startTimeSec": 0.1,
                    "endTimeSec": 0.3,
                    "lowFreqHz": 1500,
                    "highFreqHz": 3000,
                    "comment": "SongA",
                    "notes": "line1\nline2",
                    "extraFields": {"quality": "Q3"},
                }
            ],
        },
        headers=headers,
    )
    assert save_ann.status_code == 200
    loaded_doc = client.post(
        "/api/annotations/import-source",
        json={"sourceId": "local", "uri": ann_uri},
        headers=headers,
    ).json()
    assert len(loaded_doc["annotations"]) == 1
    assert loaded_doc["annotations"][0]["notes"] == "line1\nline2"
    assert loaded_doc["annotations"][0]["extraFields"]["quality"] == "Q3"


def test_real_cleansongs_fixtures_if_present() -> None:
    wavs = sorted(FIXTURES_DIR.glob("*.wav")) if FIXTURES_DIR.exists() else []
    if not wavs:
        return
    registry = SourceRegistry()
    registry.register(LocalFilesystemSource("local", root_dir=FIXTURES_DIR, restrict_to_root=True))
    client = TestClient(create_app(registry=registry))
    for wav in wavs:
        info = client.post(
            "/api/audio/open", json={"sourceId": "local", "uri": str(wav)}
        ).json()
        assert info["durationSec"] > 0.1
        spec = client.get(f"/api/audio/{info['fileId']}/spectrogram")
        assert spec.status_code == 200
        assert int(spec.headers["X-Spec-Time-Bins"]) > 0
