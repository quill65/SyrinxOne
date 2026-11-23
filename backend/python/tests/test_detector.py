from __future__ import annotations

from pathlib import Path

import numpy as np
from fastapi.testclient import TestClient

from syrinx_backend.api.app import create_app
from syrinx_backend.dsp.detector import detect_sound_events
from syrinx_backend.storage.base import SourceRegistry
from syrinx_backend.storage.local_fs import LocalFilesystemSource

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def test_detector_finds_two_bandlimited_calls_and_ignores_brief_click() -> None:
    sr = 22050
    n = int(sr * 2.0)
    rng = np.random.default_rng(42)
    sig = (0.002 * rng.standard_normal(n)).astype(np.float32)

    t = np.arange(n, dtype=np.float32) / sr
    # Call 1: 0.25s - 0.55s @ 3500 Hz
    m1 = (t >= 0.25) & (t <= 0.55)
    sig[m1] += 0.35 * np.sin(2.0 * np.pi * 3500.0 * t[m1])

    # Brief 5ms click @ 1.0s (shorter than min_trigger_dur_sec=0.02s -> ignored)
    mc = (t >= 1.00) & (t <= 1.005)
    sig[mc] += 0.4 * np.sin(2.0 * np.pi * 4000.0 * t[mc])

    # Call 2: 1.30s - 1.65s @ 5200 Hz
    m2 = (t >= 1.30) & (t <= 1.65)
    sig[m2] += 0.35 * np.sin(2.0 * np.pi * 5200.0 * t[m2])

    dets, noise_floor = detect_sound_events(
        sig,
        sample_rate=sr,
        soundfile_name="synthetic.wav",
        min_freq_hz=1500.0,
        max_freq_hz=8000.0,
        trigger_threshold_db=15.0,
        min_trigger_dur_sec=0.02,
        min_sound_dur_sec=0.10,
        max_silence_gap_sec=0.15,
        pre_buffer_sec=0.02,
        post_buffer_sec=0.02,
        pop_filter=True,
        comment="AutoCall",
    )
    assert noise_floor < -40.0
    assert len(dets) == 2
    assert 0.20 <= dets[0].start_time_sec <= 0.28
    assert 0.53 <= dets[0].end_time_sec <= 0.60
    assert 1.25 <= dets[1].start_time_sec <= 1.33
    assert dets[0].comment == "AutoCall"


def test_detect_and_tsv_export_endpoints_on_real_cleansong() -> None:
    wavs = sorted(FIXTURES_DIR.glob("*.wav")) if FIXTURES_DIR.exists() else []
    if not wavs:
        return
    registry = SourceRegistry()
    registry.register(LocalFilesystemSource("local", root_dir=FIXTURES_DIR, restrict_to_root=True))
    client = TestClient(create_app(registry=registry))

    info = client.post("/api/audio/open", json={"sourceId": "local", "uri": str(wavs[0])}).json()
    file_id = info["fileId"]

    tsv = client.get(f"/api/audio/{file_id}/spectrogram.tsv?fft_size=256&hop_size=256")
    assert tsv.status_code == 200
    lines = tsv.text.strip().splitlines()
    assert lines[0].startswith("time_sec\t")
    assert len(lines) > 5

    det_resp = client.post(
        f"/api/audio/{file_id}/detect",
        json={
            "minFreqHz": 1500,
            "maxFreqHz": 9000,
            "triggerThresholdDb": 12,
            "minTriggerDurSec": 0.015,
            "minSoundDurSec": 0.08,
            "maxSilenceGapSec": 0.3,
            "preBufferSec": 0.02,
            "postBufferSec": 0.03,
            "popFilter": True,
            "comment": "SongDet",
        },
    )
    assert det_resp.status_code == 200
    payload = det_resp.json()
    assert len(payload["detections"]) >= 1
