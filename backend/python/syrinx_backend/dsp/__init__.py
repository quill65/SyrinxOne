"""STFT & Spectrogram DSP engines (Modern SciPy + Classic Syrinx FFTTools parity)."""

from .detector import detect_sound_events
from .stft_engine import (
    SpectrogramResult,
    apply_pop_filter,
    build_classic_window,
    compute_spectrogram,
    render_spectrogram_png,
)

__all__ = [
    "SpectrogramResult",
    "apply_pop_filter",
    "build_classic_window",
    "compute_spectrogram",
    "detect_sound_events",
    "render_spectrogram_png",
]
