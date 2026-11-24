from __future__ import annotations

import numpy as np
from scipy.signal import butter, sosfiltfilt

from syrinx_backend.dsp.stft_engine import compute_spectrogram
from syrinx_backend.models import SelectionMeasurements


def measure_acoustic_selection(
    samples: np.ndarray,
    *,
    sample_rate: int,
    start_sec: float,
    end_sec: float,
    low_freq_hz: float,
    high_freq_hz: float,
    fft_size: int = 512,
    hop_size: int = 128,
) -> SelectionMeasurements:
    """Compute bioacoustic spectral & temporal measurements inside a time-frequency box."""
    total_dur = len(samples) / max(1, sample_rate)
    nyquist = sample_rate * 0.5

    s_sec = max(0.0, min(total_dur, min(start_sec, end_sec)))
    e_sec = max(s_sec + 1e-4, min(total_dur, max(start_sec, end_sec)))
    lo_hz = max(0.0, min(nyquist, min(low_freq_hz, high_freq_hz)))
    hi_hz = max(lo_hz + 1.0, min(nyquist, max(low_freq_hz, high_freq_hz)))
    duration_sec = max(1e-4, e_sec - s_sec)

    # Compute STFT over a slightly padded context window so we can estimate local noise floor
    pad_sec = min(2.0, max(0.5, duration_sec * 0.5))
    ctx_start = max(0.0, s_sec - pad_sec)
    ctx_end = min(total_dur, e_sec + pad_sec)

    spec = compute_spectrogram(
        samples,
        sample_rate=sample_rate,
        fft_size=fft_size,
        hop_size=hop_size,
        window_type="hann",
        dsp_mode="modern_stft",
        start_sec=ctx_start,
        end_sec=ctx_end,
        min_freq_hz=lo_hz,
        max_freq_hz=hi_hz,
    )

    # Separate in-selection frames vs surrounding context frames for noise floor
    t_bins, f_bins = spec.matrix.shape
    freqs = spec.min_freq_hz + np.arange(f_bins, dtype=np.float64) * spec.freq_step_hz
    times = spec.start_sec + np.arange(t_bins, dtype=np.float64) * spec.time_step_sec

    in_mask = (times >= s_sec) & (times <= e_sec)
    if not np.any(in_mask):
        in_mask[min(t_bins - 1, max(0, t_bins // 2))] = True

    sel_db = spec.matrix[in_mask].astype(np.float64)
    sel_times = times[in_mask]
    sel_power = np.power(10.0, sel_db / 10.0)

    # Frequency-axis aggregate power inside the selection
    freq_power = np.sum(sel_power, axis=0)
    total_power = float(np.sum(freq_power))
    if total_power > 0 and f_bins > 0:
        peak_idx = int(np.argmax(freq_power))
        peak_freq_hz = float(freqs[peak_idx])
        centroid_freq_hz = float(np.sum(freqs * freq_power) / total_power)
        cum = np.cumsum(freq_power) / total_power
        q5_freq_hz = float(freqs[int(np.searchsorted(cum, 0.05))])
        q50_freq_hz = float(freqs[int(np.searchsorted(cum, 0.50))])
        q95_freq_hz = float(freqs[min(f_bins - 1, int(np.searchsorted(cum, 0.95)))])
        bw90_hz = max(0.0, q95_freq_hz - q5_freq_hz)
    else:
        mid_f = 0.5 * (lo_hz + hi_hz)
        peak_freq_hz = centroid_freq_hz = q5_freq_hz = q50_freq_hz = q95_freq_hz = mid_f
        bw90_hz = 0.0

    # Time of peak spectral energy inside selection
    frame_power = np.sum(sel_power, axis=1)
    peak_t_idx = int(np.argmax(frame_power)) if frame_power.size > 0 else 0
    time_of_peak_sec = float(sel_times[peak_t_idx]) if sel_times.size > 0 else s_sec

    # Noise floor & SNR in dB using surrounding frames (or 15th percentile of context)
    frame_max_db = np.max(spec.matrix, axis=1).astype(np.float64)
    outside_mask = ~in_mask
    if np.count_nonzero(outside_mask) >= 4:
        noise_floor_db = float(np.percentile(frame_max_db[outside_mask], 25))
    else:
        noise_floor_db = float(np.percentile(frame_max_db, 15))
    signal_peak_db = float(np.percentile(np.max(sel_db, axis=1), 90)) if sel_db.size > 0 else -120.0
    snr_db = max(0.0, signal_peak_db - noise_floor_db)

    # Time-domain RMS & Peak amplitude (band-limited when possible)
    i0 = max(0, min(len(samples), int(round(s_sec * sample_rate))))
    i1 = max(i0 + 1, min(len(samples), int(round(e_sec * sample_rate))))
    seg = samples[i0:i1].astype(np.float64)
    if seg.size >= 32 and 0.0 < lo_hz < hi_hz < nyquist * 0.99:
        try:
            sos = butter(4, [lo_hz / nyquist, hi_hz / nyquist], btype="bandpass", output="sos")
            seg_meas = sosfiltfilt(sos, seg)
        except Exception:
            seg_meas = seg
    else:
        seg_meas = seg

    rms = float(np.sqrt(np.mean(seg_meas * seg_meas))) if seg_meas.size > 0 else 0.0
    peak = float(np.max(np.abs(seg_meas))) if seg_meas.size > 0 else 0.0
    rms_db = float(20.0 * np.log10(max(rms, 1e-7)))
    peak_db = float(20.0 * np.log10(max(peak, 1e-7)))

    return SelectionMeasurements(
        duration_sec=duration_sec,
        low_freq_hz=lo_hz,
        high_freq_hz=hi_hz,
        bandwidth_hz=hi_hz - lo_hz,
        peak_freq_hz=peak_freq_hz,
        centroid_freq_hz=centroid_freq_hz,
        q5_freq_hz=q5_freq_hz,
        q50_freq_hz=q50_freq_hz,
        q95_freq_hz=q95_freq_hz,
        bw90_hz=bw90_hz,
        time_of_peak_sec=time_of_peak_sec,
        rms_db=rms_db,
        peak_db=peak_db,
        noise_floor_db=noise_floor_db,
        snr_db=snr_db,
    )
