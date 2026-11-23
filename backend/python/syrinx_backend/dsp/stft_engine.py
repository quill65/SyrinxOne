from __future__ import annotations

import io
from dataclasses import dataclass

import numpy as np
from PIL import Image
from scipy import signal

from syrinx_backend.models import ColormapName, DspMode, WindowType


@dataclass
class SpectrogramResult:
    """Computed spectrogram matrix in row-major order: shape (num_time_bins, num_freq_bins)."""

    matrix: np.ndarray  # float32 (T, F), low freq at index 0 -> high freq at index F-1
    time_step_sec: float
    freq_step_hz: float
    start_sec: float
    end_sec: float
    min_freq_hz: float
    max_freq_hz: float
    min_value: float
    max_value: float
    dsp_mode: DspMode


def build_classic_window(fft_size: int, window_type: WindowType) -> np.ndarray:
    """Exact window formulas ported from legacy syrinx_original_cpp/common/FFTTools.cpp."""
    n = max(2, int(fft_size))
    i = np.arange(n, dtype=np.float64)
    denom = float(n - 1)
    if window_type == "blackman":
        w = 0.42 - 0.5 * np.cos(2.0 * np.pi * i / denom) + 0.08 * np.cos(4.0 * np.pi * i / denom)
    elif window_type == "hamming":
        # Note: Classic Syrinx FFTTools.cpp HAMMING used 0.5 * (1 + cos(pi + i*pi/(N-1)))
        # while modern mode uses standard Hamming (0.54 - 0.46*cos).
        w = 0.54 - 0.46 * np.cos(2.0 * np.pi * i / denom)
    elif window_type == "hann":
        w = 0.5 * (1.0 - np.cos(2.0 * np.pi * i / denom))
    else:  # rect / SQUARE
        w = np.ones(n, dtype=np.float64)
    w = np.clip(w, 1e-5, 1.0)
    return w.astype(np.float32)


def apply_pop_filter(matrix: np.ndarray) -> np.ndarray:
    """Suppress broadband vertical clicks/pops (inspired by SpecProfile::ApplyPopFilter)."""
    if matrix.size == 0 or matrix.shape[0] < 3:
        return matrix
    # Compare each column's median energy against global median + IQR
    col_medians = np.median(matrix, axis=1)
    global_med = float(np.median(col_medians))
    mad = float(np.median(np.abs(col_medians - global_med))) + 1e-6
    pop_mask = (col_medians - global_med) > (3.5 * mad)
    if not np.any(pop_mask):
        return matrix
    out = matrix.copy()
    pop_indices = np.where(pop_mask)[0]
    for idx in pop_indices:
        prev_idx = max(0, idx - 1)
        next_idx = min(matrix.shape[0] - 1, idx + 1)
        out[idx] = 0.5 * (matrix[prev_idx] + matrix[next_idx])
    return out


def compute_spectrogram(
    samples_1d: np.ndarray,
    sample_rate: int,
    fft_size: int = 512,
    hop_size: int = 128,
    window_type: WindowType = "hann",
    dsp_mode: DspMode = "modern_stft",
    pop_filter: bool = False,
    classic_gain: float = 1.0,
    classic_white_thresh: float = 0.0,
    start_sec: float | None = None,
    end_sec: float | None = None,
    min_freq_hz: float | None = None,
    max_freq_hz: float | None = None,
) -> SpectrogramResult:
    """Compute Float32 spectrogram matrix in either Modern dB STFT or Classic Syrinx mode."""
    n_total = len(samples_1d)
    duration_sec = n_total / sample_rate if sample_rate > 0 else 0.0
    s_sec = max(0.0, 0.0 if start_sec is None else start_sec)
    e_sec = min(duration_sec, duration_sec if end_sec is None else end_sec)
    i0 = max(0, min(n_total, int(round(s_sec * sample_rate))))
    i1 = max(i0, min(n_total, int(round(e_sec * sample_rate))))
    segment = np.asarray(samples_1d[i0:i1], dtype=np.float32)

    fft_size = max(32, min(16384, int(fft_size)))
    hop_size = max(1, min(fft_size, int(hop_size)))

    if len(segment) < fft_size:
        padded = np.zeros(fft_size, dtype=np.float32)
        padded[: len(segment)] = segment
        segment = padded

    if dsp_mode == "classic_syrinx":
        # Ported from FFTTools::WaveToSpecLine / MakeMagnitudes (16-bit PCM scale)
        win = build_classic_window(fft_size, window_type)
        n_frames = 1 + max(0, (len(segment) - fft_size) // hop_size)
        stride = segment.strides[0]
        frames = np.lib.stride_tricks.as_strided(
            segment,
            shape=(n_frames, fft_size),
            strides=(hop_size * stride, stride),
        )
        # Scale normalized [-1,1] float audio to 16-bit integer amplitude range like Win32 Syrinx
        pcm_frames = frames * 32767.0
        windowed = pcm_frames * win[None, :]
        spec = np.fft.rfft(windowed, n=fft_size, axis=1)
        # Classic Syrinx linear magnitude scaled into dB-like [0, 255] -> mapped to [-100, 0] range
        mag = np.abs(spec).astype(np.float32) / float(fft_size)
        if classic_gain > 0:
            mag = mag * float(classic_gain)
        elif classic_gain < 0:
            mag = mag / float(-classic_gain)
        # Convert classic scaled linear magnitude to normalized dB so WebGL gain/thresh still work
        mag_norm = mag / 32767.0
        db = (20.0 * np.log10(np.maximum(mag_norm, 1e-6))).astype(np.float32)
        if classic_white_thresh > 0:
            # Classic white threshold clips weak floor bins to -120 dB
            floor_db = -100.0 + (float(classic_white_thresh) / 255.0) * 80.0
            db = np.where(db < floor_db, -120.0, db).astype(np.float32)
        matrix = db
        freqs = np.fft.rfftfreq(fft_size, d=1.0 / sample_rate)
    else:
        win_map = {
            "blackman": "blackman",
            "hamming": "hamming",
            "hann": "hann",
            "rect": "boxcar",
        }
        win_name = win_map.get(window_type, "hann")
        noverlap = fft_size - hop_size
        freqs, _times, zxx = signal.stft(
            segment,
            fs=sample_rate,
            window=win_name,
            nperseg=fft_size,
            noverlap=noverlap,
            boundary=None,
            padded=False,
        )
        # zxx shape is (F, T); transpose to (T, F)
        mag = np.abs(zxx.T).astype(np.float32)
        matrix = (20.0 * np.log10(np.maximum(mag, 1e-7))).astype(np.float32)

    if pop_filter:
        matrix = apply_pop_filter(matrix)

    nyquist = sample_rate / 2.0
    f_min = max(0.0, 0.0 if min_freq_hz is None else float(min_freq_hz))
    f_max = min(nyquist, nyquist if max_freq_hz is None else float(max_freq_hz))
    if f_max <= f_min:
        f_min, f_max = 0.0, nyquist

    freq_mask = (freqs >= f_min) & (freqs <= f_max)
    if not np.any(freq_mask):
        freq_mask = np.ones(len(freqs), dtype=bool)
    freqs_sliced = freqs[freq_mask]
    matrix = np.ascontiguousarray(matrix[:, freq_mask], dtype=np.float32)

    freq_step = float(sample_rate) / float(fft_size)
    time_step = float(hop_size) / float(sample_rate)
    min_val = float(np.min(matrix)) if matrix.size else -120.0
    max_val = float(np.max(matrix)) if matrix.size else 0.0

    return SpectrogramResult(
        matrix=matrix,
        time_step_sec=time_step,
        freq_step_hz=freq_step,
        start_sec=s_sec,
        end_sec=e_sec,
        min_freq_hz=float(freqs_sliced[0]),
        max_freq_hz=float(freqs_sliced[-1]),
        min_value=min_val,
        max_value=max_val,
        dsp_mode=dsp_mode,
    )


def _build_lut(colormap: ColormapName) -> np.ndarray:
    """Build 256x3 uint8 RGB lookup table matching frontend WebGL2 colormaps."""
    t = np.linspace(0.0, 1.0, 256, dtype=np.float32)
    if colormap == "grayscale_bow":
        v = np.round((1.0 - t) * 255.0).astype(np.uint8)
        return np.stack([v, v, v], axis=-1)
    if colormap == "grayscale_wob":
        v = np.round(t * 255.0).astype(np.uint8)
        return np.stack([v, v, v], axis=-1)

    # Compact piecewise-linear scientific LUTs matching frontend colormaps.ts
    stops: dict[str, list[tuple[float, tuple[int, int, int]]]] = {
        "viridis": [
            (0.0, (68, 1, 84)),
            (0.25, (59, 82, 139)),
            (0.5, (33, 145, 140)),
            (0.75, (94, 201, 98)),
            (1.0, (253, 231, 37)),
        ],
        "magma": [
            (0.0, (0, 0, 4)),
            (0.25, (81, 18, 124)),
            (0.5, (183, 55, 121)),
            (0.75, (252, 137, 97)),
            (1.0, (252, 253, 191)),
        ],
        "inferno": [
            (0.0, (0, 0, 4)),
            (0.25, (87, 16, 110)),
            (0.5, (188, 55, 84)),
            (0.75, (249, 142, 9)),
            (1.0, (252, 255, 164)),
        ],
        "turbo": [
            (0.0, (48, 18, 59)),
            (0.25, (40, 188, 235)),
            (0.5, (164, 252, 60)),
            (0.75, (251, 126, 33)),
            (1.0, (122, 4, 3)),
        ],
    }
    pts = stops.get(colormap, stops["viridis"])
    xs = np.array([p[0] for p in pts], dtype=np.float32)
    r = np.interp(t, xs, [p[1][0] for p in pts])
    g = np.interp(t, xs, [p[1][1] for p in pts])
    b = np.interp(t, xs, [p[1][2] for p in pts])
    return np.stack([r, g, b], axis=-1).clip(0, 255).astype(np.uint8)


def render_spectrogram_png(
    spec: SpectrogramResult,
    colormap: ColormapName = "grayscale_bow",
    gain_db: float = 0.0,
    threshold_db: float = -80.0,
    dynamic_range_db: float = 75.0,
) -> bytes:
    """Server-side bitmap fallback renderer (SpecDIB equivalent) returning PNG bytes."""
    mat = spec.matrix  # (T, F), low freq at 0
    if mat.size == 0:
        img = Image.new("RGB", (1, 1), (255, 255, 255))
    else:
        dyn = max(1.0, float(dynamic_range_db))
        norm = (mat + float(gain_db) - float(threshold_db)) / dyn
        norm = np.clip(norm, 0.0, 1.0)
        idx = np.round(norm * 255.0).astype(np.uint8)
        lut = _build_lut(colormap)
        # Flip frequency axis so high frequency is at the top (row 0) and time runs left->right
        rgb = lut[idx.T[::-1, :]]
        img = Image.fromarray(rgb, mode="RGB")

    buf = io.BytesIO()
    img.save(buf, format="PNG", compress_level=1)
    return buf.getvalue()
