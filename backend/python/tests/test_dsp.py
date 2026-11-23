from __future__ import annotations

import io

import numpy as np
from PIL import Image

from syrinx_backend.dsp.stft_engine import (
    apply_pop_filter,
    build_classic_window,
    compute_spectrogram,
    render_spectrogram_png,
)


def _make_sine(freq_hz: float, sr: int = 22050, duration_sec: float = 1.0) -> np.ndarray:
    t = np.arange(int(sr * duration_sec), dtype=np.float32) / sr
    return (0.5 * np.sin(2.0 * np.pi * freq_hz * t)).astype(np.float32)


def test_modern_stft_finds_exact_tone_frequency() -> None:
    sr = 22050
    target_hz = 2500.0
    sig = _make_sine(target_hz, sr=sr, duration_sec=0.5)
    res = compute_spectrogram(
        sig,
        sample_rate=sr,
        fft_size=512,
        hop_size=128,
        window_type="hann",
        dsp_mode="modern_stft",
    )
    assert res.matrix.dtype == np.float32
    assert res.matrix.ndim == 2
    assert res.matrix.shape[0] > 10
    assert res.matrix.shape[1] == 257

    mean_spectrum = np.mean(res.matrix, axis=0)
    peak_bin = int(np.argmax(mean_spectrum))
    peak_hz = peak_bin * res.freq_step_hz
    assert abs(peak_hz - target_hz) <= res.freq_step_hz


def test_classic_syrinx_dsp_mode_and_windows() -> None:
    sr = 22050
    target_hz = 4000.0
    sig = _make_sine(target_hz, sr=sr, duration_sec=0.5)
    for win in ("blackman", "hamming", "hann", "rect"):
        w = build_classic_window(512, win)  # type: ignore[arg-type]
        assert len(w) == 512
        assert np.all(w > 0)

    res = compute_spectrogram(
        sig,
        sample_rate=sr,
        fft_size=512,
        hop_size=128,
        window_type="blackman",
        dsp_mode="classic_syrinx",
        classic_gain=2.0,
        classic_white_thresh=10.0,
    )
    assert res.dsp_mode == "classic_syrinx"
    mean_spectrum = np.mean(res.matrix, axis=0)
    peak_hz = int(np.argmax(mean_spectrum)) * res.freq_step_hz
    assert abs(peak_hz - target_hz) <= res.freq_step_hz


def test_pop_filter_suppresses_broadband_click() -> None:
    mat = np.full((20, 64), -80.0, dtype=np.float32)
    mat[10, :] = -10.0  # broadband vertical pop
    cleaned = apply_pop_filter(mat)
    assert float(np.median(cleaned[10, :])) < -50.0


def test_server_bitmap_png_fallback_renders_valid_image() -> None:
    sig = _make_sine(1500.0, sr=16000, duration_sec=0.4)
    spec = compute_spectrogram(sig, sample_rate=16000, fft_size=256, hop_size=64)
    for cmap in ("grayscale_bow", "grayscale_wob", "viridis", "magma", "inferno", "turbo"):
        png = render_spectrogram_png(spec, colormap=cmap)  # type: ignore[arg-type]
        img = Image.open(io.BytesIO(png))
        assert img.format == "PNG"
        assert img.size == (spec.matrix.shape[0], spec.matrix.shape[1])
