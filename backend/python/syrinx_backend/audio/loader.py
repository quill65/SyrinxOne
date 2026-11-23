from __future__ import annotations

import hashlib
import io
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np
import soundfile as sf

from syrinx_backend.models import AudioFileInfo, WaveformOverview
from syrinx_backend.storage.base import AudioDataSource


@dataclass
class LoadedAudio:
    info: AudioFileInfo
    """Float32 array shaped (num_channels, num_frames) normalized to [-1.0, 1.0]."""
    samples: np.ndarray
    raw_wav_bytes: bytes | None = None


def _sanitize_filename_token(text: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", text.strip())
    return cleaned.strip("._ ") or "clip"


def expand_clip_template(
    template: str,
    source_filename: str,
    channel_1based: int = 1,
    comment: str = "",
    timestamp: datetime | None = None,
) -> str:
    """Expand classic Syrinx DlgKlipnSave filename tokens (%f, %c, %ch, %d, %t)."""
    ts = timestamp or datetime.now()
    stem = Path(source_filename).stem
    result = template or "%f_clip"
    result = result.replace("%f", stem)
    result = result.replace("%ch", f"ch{channel_1based}")
    result = result.replace("%c", _sanitize_filename_token(comment) if comment else "clip")
    result = result.replace("%d", ts.strftime("%Y%m%d"))
    result = result.replace("%t", ts.strftime("%H%M%S"))
    result = _sanitize_filename_token(result)
    if not result.lower().endswith(".wav"):
        result = f"{result}.wav"
    return result


class AudioSessionCache:
    """In-memory session cache for open single-file recordings (V1)."""

    def __init__(self, max_files: int = 64) -> None:
        self._max_files = max_files
        self._items: dict[str, LoadedAudio] = {}

    def _make_file_id(self, source_id: str, uri: str) -> str:
        digest = hashlib.sha256(f"{source_id}::{uri}".encode()).hexdigest()[:16]
        return digest

    def _evict_if_needed(self) -> None:
        while len(self._items) >= self._max_files:
            oldest_key = next(iter(self._items))
            del self._items[oldest_key]

    def _decode_audio(
        self,
        source_id: str,
        uri: str,
        name: str,
        local_path: Path | None,
        raw_bytes: bytes | None,
    ) -> LoadedAudio:
        if local_path is not None and local_path.is_file():
            with sf.SoundFile(str(local_path)) as handle:
                data = handle.read(dtype="float32", always_2d=True)
                sr = int(handle.samplerate)
                fmt = str(handle.format)
                subtype = str(handle.subtype)
        elif raw_bytes is not None:
            with sf.SoundFile(io.BytesIO(raw_bytes)) as handle:
                data = handle.read(dtype="float32", always_2d=True)
                sr = int(handle.samplerate)
                fmt = str(handle.format)
                subtype = str(handle.subtype)
        else:
            raise FileNotFoundError(f"Audio source could not be read: {uri}")

        # Transpose to (num_channels, num_frames) contiguous float32
        samples = np.ascontiguousarray(data.T, dtype=np.float32)
        num_channels, num_frames = samples.shape
        duration_sec = float(num_frames / sr) if sr > 0 else 0.0
        file_id = self._make_file_id(source_id, uri)

        info = AudioFileInfo(
            file_id=file_id,
            source_id=source_id,
            uri=uri,
            name=name,
            sample_rate=sr,
            num_channels=num_channels,
            num_frames=num_frames,
            duration_sec=duration_sec,
            format=fmt,
            subtype=subtype,
        )
        return LoadedAudio(info=info, samples=samples)

    async def open_from_source(self, source: AudioDataSource, uri: str) -> LoadedAudio:
        file_id = self._make_file_id(source.source_id, uri)
        if file_id in self._items:
            return self._items[file_id]

        local_path = await source.resolve_local_path(uri)
        raw_bytes: bytes | None = None
        if local_path is None:
            raw_bytes = await source.read_bytes(uri)
        name = Path(uri).name or uri
        loaded = self._decode_audio(source.source_id, uri, name, local_path, raw_bytes)
        self._evict_if_needed()
        self._items[loaded.info.file_id] = loaded
        return loaded

    def register_uploaded_bytes(self, filename: str, raw_bytes: bytes) -> LoadedAudio:
        content_hash = hashlib.sha256(raw_bytes).hexdigest()[:12]
        uri = f"upload://{content_hash}/{filename}"
        loaded = self._decode_audio("upload", uri, filename, None, raw_bytes)
        self._evict_if_needed()
        self._items[loaded.info.file_id] = loaded
        return loaded

    def get(self, file_id: str) -> LoadedAudio:
        if file_id not in self._items:
            raise KeyError(f"Audio file '{file_id}' is not loaded in session")
        return self._items[file_id]

    def get_wav_bytes(self, file_id: str) -> bytes:
        loaded = self.get(file_id)
        if loaded.raw_wav_bytes is None:
            buf = io.BytesIO()
            sf.write(buf, loaded.samples.T, loaded.info.sample_rate, format="WAV", subtype="PCM_16")
            loaded.raw_wav_bytes = buf.getvalue()
        return loaded.raw_wav_bytes

    def compute_waveform(
        self,
        file_id: str,
        channel: int = 0,
        num_bins: int = 2000,
        start_sec: float | None = None,
        end_sec: float | None = None,
    ) -> WaveformOverview:
        loaded = self.get(file_id)
        sr = loaded.info.sample_rate
        total_frames = loaded.info.num_frames
        if channel < 0 or channel >= loaded.info.num_channels:
            raise ValueError(
                f"Channel {channel} out of range (0..{loaded.info.num_channels - 1})"
            )

        s_sec = max(0.0, 0.0 if start_sec is None else start_sec)
        e_sec = min(
            loaded.info.duration_sec,
            loaded.info.duration_sec if end_sec is None else end_sec,
        )
        if e_sec <= s_sec:
            return WaveformOverview(
                file_id=file_id,
                channel=channel,
                num_bins=0,
                start_sec=s_sec,
                end_sec=e_sec,
                min_values=[],
                max_values=[],
            )

        i0 = max(0, min(total_frames, int(round(s_sec * sr))))
        i1 = max(i0 + 1, min(total_frames, int(round(e_sec * sr))))
        segment = loaded.samples[channel, i0:i1]
        n = len(segment)
        bins = max(1, min(int(num_bins), n))

        # Vectorized min/max envelope across bins
        edges = np.linspace(0, n, bins + 1, dtype=np.int64)
        min_vals = np.empty(bins, dtype=np.float32)
        max_vals = np.empty(bins, dtype=np.float32)
        for b in range(bins):
            a, c = int(edges[b]), int(edges[b + 1])
            if c <= a:
                c = min(n, a + 1)
            chunk = segment[a:c]
            min_vals[b] = float(np.min(chunk))
            max_vals[b] = float(np.max(chunk))

        return WaveformOverview(
            file_id=file_id,
            channel=channel,
            num_bins=bins,
            start_sec=s_sec,
            end_sec=e_sec,
            min_values=min_vals.tolist(),
            max_values=max_vals.tolist(),
        )

    def slice_to_wav_bytes(
        self,
        file_id: str,
        start_sec: float,
        end_sec: float,
        channels: Sequence[int] | None = None,
    ) -> bytes:
        loaded = self.get(file_id)
        sr = loaded.info.sample_rate
        i0 = max(0, min(loaded.info.num_frames, int(round(start_sec * sr))))
        i1 = max(i0, min(loaded.info.num_frames, int(round(end_sec * sr))))
        if i1 <= i0:
            raise ValueError("Clip selection has zero duration")

        if channels:
            ch_indices = [c for c in channels if 0 <= c < loaded.info.num_channels]
            if not ch_indices:
                raise ValueError("No valid channels specified for clip")
            sliced = loaded.samples[ch_indices, i0:i1]
        else:
            sliced = loaded.samples[:, i0:i1]

        buf = io.BytesIO()
        sf.write(buf, sliced.T, sr, format="WAV", subtype="PCM_16")
        return buf.getvalue()
