from __future__ import annotations

import uuid

import numpy as np

from syrinx_backend.dsp.stft_engine import compute_spectrogram
from syrinx_backend.models import AnnotationRecord


def detect_sound_events(
    samples_1d: np.ndarray,
    sample_rate: int,
    soundfile_name: str,
    channel_1based: int = 1,
    fft_size: int = 512,
    hop_size: int = 128,
    min_freq_hz: float = 1000.0,
    max_freq_hz: float = 10000.0,
    trigger_threshold_db: float = 12.0,
    min_trigger_dur_sec: float = 0.015,
    min_sound_dur_sec: float = 0.08,
    max_silence_gap_sec: float = 0.25,
    pre_buffer_sec: float = 0.03,
    post_buffer_sec: float = 0.05,
    pop_filter: bool = True,
    start_sec: float | None = None,
    end_sec: float | None = None,
    comment: str = "Detected",
) -> tuple[list[AnnotationRecord], float]:
    """
    Syrinx-inspired band-limited adaptive sound event detector.

    Mirrors classic CTriggerEventDetector / CSoundDetector semantics:
    1. Band-limited STFT with optional broadband pop filter.
    2. Adaptive background noise floor (25th percentile of band peak energy).
    3. Trigger events above (noise_floor + trigger_threshold_db) lasting >= min_trigger_dur_sec.
    4. Merge trigger events separated by <= max_silence_gap_sec into sound detections.
    5. Discard detections shorter than min_sound_dur_sec and apply pre/post time buffers.
    """
    duration_sec = len(samples_1d) / sample_rate if sample_rate > 0 else 0.0
    if duration_sec <= 0:
        return [], -120.0

    nyquist = sample_rate / 2.0
    lo_f = max(0.0, min(nyquist, min_freq_hz))
    hi_f = max(lo_f + 50.0, min(nyquist, max_freq_hz))

    spec = compute_spectrogram(
        samples_1d,
        sample_rate=sample_rate,
        fft_size=fft_size,
        hop_size=hop_size,
        window_type="hann",
        dsp_mode="modern_stft",
        pop_filter=pop_filter,
        start_sec=start_sec,
        end_sec=end_sec,
        min_freq_hz=lo_f,
        max_freq_hz=hi_f,
    )

    mat = spec.matrix  # shape (T, F)
    if mat.size == 0 or mat.shape[0] == 0:
        return [], -120.0

    # Peak spectral energy per frame inside the target frequency band captures narrowband vocalizations
    frame_levels = np.max(mat, axis=1)
    noise_floor_db = float(np.percentile(frame_levels, 25))
    thresh_db = noise_floor_db + float(trigger_threshold_db)
    above = frame_levels >= thresh_db

    # Step 1: Extract contiguous trigger runs >= min_trigger_dur_sec
    dt = max(1e-6, spec.time_step_sec)
    min_trig_frames = max(1, int(round(min_trigger_dur_sec / dt)))
    triggers: list[tuple[int, int]] = []
    i = 0
    n_frames = len(above)
    while i < n_frames:
        if above[i]:
            j = i + 1
            while j < n_frames and above[j]:
                j += 1
            if (j - i) >= min_trig_frames:
                triggers.append((i, j))
            i = j
        else:
            i += 1

    if not triggers:
        return [], noise_floor_db

    # Step 2: Merge trigger runs separated by <= max_silence_gap_sec
    max_gap_frames = max(0, int(round(max_silence_gap_sec / dt)))
    merged: list[tuple[int, int]] = [triggers[0]]
    for s_idx, e_idx in triggers[1:]:
        prev_s, prev_e = merged[-1]
        if (s_idx - prev_e) <= max_gap_frames:
            merged[-1] = (prev_s, e_idx)
        else:
            merged.append((s_idx, e_idx))

    # Step 3: Filter by min_sound_dur_sec, estimate active freq bounds, apply pre/post buffers
    detections: list[AnnotationRecord] = []
    freq_axis = np.linspace(spec.min_freq_hz, spec.max_freq_hz, mat.shape[1])

    for s_idx, e_idx in merged:
        raw_start = spec.start_sec + s_idx * dt
        raw_end = spec.start_sec + e_idx * dt
        if (raw_end - raw_start) < min_sound_dur_sec:
            continue

        # Estimate tight frequency envelope from bins exceeding threshold inside the detection
        sub = mat[s_idx:e_idx, :]
        active_bins = np.where(np.max(sub, axis=0) >= thresh_db)[0]
        if len(active_bins) > 0:
            det_low_f = float(freq_axis[int(active_bins[0])])
            det_high_f = float(freq_axis[int(active_bins[-1])])
            if det_high_f - det_low_f < spec.freq_step_hz * 2:
                det_low_f = max(lo_f, det_low_f - spec.freq_step_hz * 2)
                det_high_f = min(hi_f, det_high_f + spec.freq_step_hz * 2)
        else:
            det_low_f, det_high_f = lo_f, hi_f

        buf_start = max(0.0, raw_start - max(0.0, pre_buffer_sec))
        buf_end = min(duration_sec, raw_end + max(0.0, post_buffer_sec))
        peak_db = float(np.max(sub))

        detections.append(
            AnnotationRecord(
                id=str(uuid.uuid4()),
                soundfile=soundfile_name,
                channel=channel_1based,
                start_time_sec=buf_start,
                end_time_sec=buf_end,
                low_freq_hz=det_low_f,
                high_freq_hz=det_high_f,
                comment=comment,
                notes="",
                extra_fields={"peak_db": round(peak_db, 1)},
                modified=True,
            )
        )

    return detections, noise_floor_db
