"""STFT & Spectrogram DSP engine."""

from .detector import detect_sound_events
from .stft_engine import (
    SpectrogramResult,
    apply_pop_filter,
    compute_spectrogram,
    render_spectrogram_png,
)

__all__ = [
    "SpectrogramResult",
    "apply_pop_filter",
    "compute_spectrogram",
    "detect_sound_events",
    "render_spectrogram_png",
]
