from __future__ import annotations

import hashlib
import io
import re
import shutil
import struct
import subprocess
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np
import soundfile as sf

from syrinx_backend.models import AudioFileInfo, WaveformOverview
from syrinx_backend.storage.base import AudioDataSource

REMOTE_WINDOW_THRESHOLD_BYTES = 24 * 1024 * 1024  # 24 MB: auto-window huge cloud archive WAVs
DEFAULT_REMOTE_WINDOW_SEC = 60.0


@dataclass
class LoadedAudio:
    info: AudioFileInfo
    """Float32 array shaped (num_channels, num_frames) normalized to [-1.0, 1.0]."""
    samples: np.ndarray
    raw_wav_bytes: bytes | None = None


@dataclass(frozen=True)
class _WavHeaderInfo:
    audio_format: int  # 1 = PCM, 3 = IEEE float
    num_channels: int
    sample_rate: int
    bits_per_sample: int
    block_align: int
    data_offset: int
    data_size: int

    @property
    def total_frames(self) -> int:
        return self.data_size // max(1, self.block_align)

    @property
    def total_duration_sec(self) -> float:
        return self.total_frames / max(1, self.sample_rate)


def _parse_wav_header(header_bytes: bytes) -> _WavHeaderInfo | None:
    """Parse standard RIFF/WAVE header bytes to enable HTTP Range partial window reads."""
    if len(header_bytes) < 44:
        return None
    if header_bytes[0:4] != b"RIFF" or header_bytes[8:12] != b"WAVE":
        return None

    pos = 12
    fmt_info: tuple[int, int, int, int, int] | None = None
    while pos + 8 <= len(header_bytes):
        chunk_id = header_bytes[pos : pos + 4]
        chunk_size = struct.unpack_from("<I", header_bytes, pos + 4)[0]
        chunk_data_start = pos + 8
        if chunk_id == b"fmt " and chunk_data_start + 16 <= len(header_bytes):
            audio_fmt, num_ch, sr, _byte_rate, block_align, bps = struct.unpack_from(
                "<HHIIHH", header_bytes, chunk_data_start
            )
            # Support standard PCM (1), IEEE float (3), or WAVE_FORMAT_EXTENSIBLE (0xFFFE)
            if audio_fmt == 0xFFFE and chunk_data_start + 26 <= len(header_bytes):
                sub_fmt = struct.unpack_from("<H", header_bytes, chunk_data_start + 24)[0]
                audio_fmt = sub_fmt
            if audio_fmt in (1, 3) and num_ch > 0 and sr > 0 and block_align > 0:
                fmt_info = (audio_fmt, num_ch, sr, bps, block_align)
        elif chunk_id == b"data":
            if fmt_info is None:
                return None
            return _WavHeaderInfo(
                audio_format=fmt_info[0],
                num_channels=fmt_info[1],
                sample_rate=fmt_info[2],
                bits_per_sample=fmt_info[3],
                block_align=fmt_info[4],
                data_offset=chunk_data_start,
                data_size=chunk_size,
            )
        pos = chunk_data_start + chunk_size + (chunk_size % 2)
    return None


def _decode_pcm_slice(pcm_bytes: bytes, hdr: _WavHeaderInfo) -> np.ndarray:
    """Decode raw PCM/IEEE-float WAV bytes into float32 (num_channels, num_frames)."""
    usable = (len(pcm_bytes) // hdr.block_align) * hdr.block_align
    buf = pcm_bytes[:usable]
    if not buf:
        return np.zeros((hdr.num_channels, 0), dtype=np.float32)

    if hdr.audio_format == 3 and hdr.bits_per_sample == 32:
        raw = np.frombuffer(buf, dtype="<f4")
    elif hdr.audio_format == 1 and hdr.bits_per_sample == 16:
        raw = np.frombuffer(buf, dtype="<i2").astype(np.float32) / 32768.0
    elif hdr.audio_format == 1 and hdr.bits_per_sample == 32:
        raw = np.frombuffer(buf, dtype="<i4").astype(np.float32) / 2147483648.0
    elif hdr.audio_format == 1 and hdr.bits_per_sample == 24:
        arr = np.frombuffer(buf, dtype=np.uint8).reshape(-1, 3)
        # Sign-extend 24-bit little-endian signed integers
        val = (
            arr[:, 0].astype(np.int32)
            | (arr[:, 1].astype(np.int32) << 8)
            | (arr[:, 2].astype(np.int32) << 16)
        )
        val = np.where(val & 0x800000, val - 0x1000000, val)
        raw = val.astype(np.float32) / 8388608.0
    elif hdr.audio_format == 1 and hdr.bits_per_sample == 8:
        raw = (np.frombuffer(buf, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    else:
        raise ValueError(
            f"Unsupported WAV PCM encoding (fmt={hdr.audio_format}, bps={hdr.bits_per_sample})"
        )

    interleaved = raw.reshape(-1, hdr.num_channels)
    return np.ascontiguousarray(interleaved.T, dtype=np.float32)


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
    """In-memory session cache supporting local files and windowed remote cloud archive reads."""

    def __init__(self, max_files: int = 64) -> None:
        self._max_files = max_files
        self._items: dict[str, LoadedAudio] = {}

    def _make_file_id(
        self,
        source_id: str,
        uri: str,
        start_sec: float = 0.0,
        max_duration_sec: float | None = None,
    ) -> str:
        win_tag = ""
        if start_sec > 0.0 or max_duration_sec is not None:
            win_tag = f"::win={start_sec:.3f}:{max_duration_sec or 0:.3f}"
        digest = hashlib.sha256(f"{source_id}::{uri}{win_tag}".encode()).hexdigest()[:16]
        return digest

    def _evict_if_needed(self) -> None:
        while len(self._items) >= self._max_files:
            oldest_key = next(iter(self._items))
            del self._items[oldest_key]

    def _decode_via_ffmpeg(
        self,
        local_path: Path | None,
        raw_bytes: bytes | None,
        name: str,
        start_sec: float,
        max_duration_sec: float | None,
    ) -> tuple[np.ndarray, int, float, str, str]:
        ffmpeg_bin = shutil.which("ffmpeg")
        if not ffmpeg_bin:
            raise RuntimeError(
                f"Cannot decode '{name}' with libsndfile and ffmpeg is not installed on PATH."
            )

        temp_in: Path | None = None
        try:
            if local_path is not None and local_path.is_file():
                in_path = local_path
            elif raw_bytes is not None:
                suffix = Path(name).suffix or ".bin"
                with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                    tmp.write(raw_bytes)
                    temp_in = Path(tmp.name)
                in_path = temp_in
            else:
                raise FileNotFoundError(f"Audio source could not be read: {name}")

            # Decode to IEEE float WAV in memory so soundfile can read exact sr/channels/samples
            cmd = [ffmpeg_bin, "-v", "error", "-i", str(in_path), "-vn", "-acodec", "pcm_f32le", "-f", "wav", "pipe:1"]
            proc = subprocess.run(
                cmd,
                capture_output=True,
                check=False,
                timeout=60,
            )
            if proc.returncode != 0 or not proc.stdout:
                err_msg = proc.stderr.decode("utf-8", errors="ignore").strip()[:300]
                raise RuntimeError(f"ffmpeg failed to decode '{name}': {err_msg or 'unknown error'}")

            with sf.SoundFile(io.BytesIO(proc.stdout)) as handle:
                sr = int(handle.samplerate)
                total_frames = int(len(handle))
                total_dur = float(total_frames / sr) if sr > 0 else 0.0
                start_frame = max(0, min(total_frames, int(round(start_sec * sr))))
                if start_frame > 0:
                    handle.seek(start_frame)
                frames_to_read = (
                    int(round(max_duration_sec * sr))
                    if max_duration_sec is not None and max_duration_sec > 0
                    else -1
                )
                data = handle.read(frames=frames_to_read, dtype="float32", always_2d=True)
            ext_fmt = Path(name).suffix.lstrip(".").upper() or "FFMPEG"
            return data, sr, total_dur, ext_fmt, "FFMPEG_FLOAT"
        finally:
            if temp_in is not None:
                temp_in.unlink(missing_ok=True)

    def _decode_audio(
        self,
        source_id: str,
        uri: str,
        name: str,
        local_path: Path | None,
        raw_bytes: bytes | None,
        start_sec: float = 0.0,
        max_duration_sec: float | None = None,
    ) -> LoadedAudio:
        try:
            if local_path is not None and local_path.is_file():
                with sf.SoundFile(str(local_path)) as handle:
                    sr = int(handle.samplerate)
                    total_frames = int(len(handle))
                    total_dur = float(total_frames / sr) if sr > 0 else 0.0
                    start_frame = max(0, min(total_frames, int(round(start_sec * sr))))
                    if start_frame > 0:
                        handle.seek(start_frame)
                    frames_to_read = (
                        int(round(max_duration_sec * sr))
                        if max_duration_sec is not None and max_duration_sec > 0
                        else -1
                    )
                    data = handle.read(frames=frames_to_read, dtype="float32", always_2d=True)
                    fmt = str(handle.format)
                    subtype = str(handle.subtype)
            elif raw_bytes is not None:
                with sf.SoundFile(io.BytesIO(raw_bytes)) as handle:
                    sr = int(handle.samplerate)
                    total_frames = int(len(handle))
                    total_dur = float(total_frames / sr) if sr > 0 else 0.0
                    start_frame = max(0, min(total_frames, int(round(start_sec * sr))))
                    if start_frame > 0:
                        handle.seek(start_frame)
                    frames_to_read = (
                        int(round(max_duration_sec * sr))
                        if max_duration_sec is not None and max_duration_sec > 0
                        else -1
                    )
                    data = handle.read(frames=frames_to_read, dtype="float32", always_2d=True)
                    fmt = str(handle.format)
                    subtype = str(handle.subtype)
            else:
                raise FileNotFoundError(f"Audio source could not be read: {uri}")
        except sf.LibsndfileError:
            data, sr, total_dur, fmt, subtype = self._decode_via_ffmpeg(
                local_path=local_path,
                raw_bytes=raw_bytes,
                name=name,
                start_sec=start_sec,
                max_duration_sec=max_duration_sec,
            )

        samples = np.ascontiguousarray(data.T, dtype=np.float32)
        num_channels, num_frames = samples.shape
        duration_sec = float(num_frames / sr) if sr > 0 else 0.0
        is_partial = start_sec > 0.0 or (total_dur > 0 and duration_sec < total_dur - 0.05)
        file_id = self._make_file_id(source_id, uri, start_sec, max_duration_sec)

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
            window_start_sec=start_sec,
            total_duration_sec=total_dur,
            is_partial_window=is_partial,
        )
        return LoadedAudio(info=info, samples=samples)

    async def _try_open_remote_wav_window(
        self,
        source: AudioDataSource,
        uri: str,
        name: str,
        start_sec: float,
        max_duration_sec: float | None,
    ) -> LoadedAudio | None:
        """Fast HTTP Range partial load for large remote WAV files (e.g. MBARI / NOAA archives)."""
        if not name.lower().endswith((".wav", ".wave")):
            return None
        try:
            probe, total_bytes = await source.read_byte_range(uri, 0, 65535)
        except Exception:
            return None

        hdr = _parse_wav_header(probe)
        if hdr is None:
            return None

        effective_size = total_bytes or (hdr.data_offset + hdr.data_size)
        need_window = (
            max_duration_sec is not None
            or start_sec > 0.0
            or effective_size > REMOTE_WINDOW_THRESHOLD_BYTES
        )
        if not need_window:
            return None

        win_dur = (
            max_duration_sec
            if (max_duration_sec is not None and max_duration_sec > 0)
            else DEFAULT_REMOTE_WINDOW_SEC
        )
        start_frame = max(0, min(hdr.total_frames, int(round(start_sec * hdr.sample_rate))))
        end_frame = min(hdr.total_frames, start_frame + int(round(win_dur * hdr.sample_rate)))
        byte_start = hdr.data_offset + start_frame * hdr.block_align
        byte_end = hdr.data_offset + end_frame * hdr.block_align - 1
        if byte_end < byte_start:
            samples = np.zeros((hdr.num_channels, 0), dtype=np.float32)
        else:
            pcm_bytes, _ = await source.read_byte_range(uri, byte_start, byte_end)
            samples = _decode_pcm_slice(pcm_bytes, hdr)

        num_channels, num_frames = samples.shape
        duration_sec = float(num_frames / hdr.sample_rate) if hdr.sample_rate > 0 else 0.0
        total_dur = hdr.total_duration_sec
        is_partial = start_sec > 0.0 or duration_sec < total_dur - 0.05
        file_id = self._make_file_id(source.source_id, uri, start_sec, win_dur)

        info = AudioFileInfo(
            file_id=file_id,
            source_id=source.source_id,
            uri=uri,
            name=name,
            sample_rate=hdr.sample_rate,
            num_channels=num_channels,
            num_frames=num_frames,
            duration_sec=duration_sec,
            format="WAV",
            subtype=f"PCM_{hdr.bits_per_sample}" if hdr.audio_format == 1 else "FLOAT",
            window_start_sec=start_sec,
            total_duration_sec=total_dur,
            is_partial_window=is_partial,
        )
        return LoadedAudio(info=info, samples=samples)

    async def open_from_source(
        self,
        source: AudioDataSource,
        uri: str,
        *,
        start_sec: float = 0.0,
        max_duration_sec: float | None = None,
    ) -> LoadedAudio:
        file_id = self._make_file_id(source.source_id, uri, start_sec, max_duration_sec)
        if file_id in self._items:
            return self._items[file_id]

        local_path = await source.resolve_local_path(uri)
        name = Path(uri.split("?", 1)[0]).name or uri

        if local_path is None:
            windowed = await self._try_open_remote_wav_window(
                source, uri, name, start_sec, max_duration_sec
            )
            if windowed is not None:
                self._evict_if_needed()
                self._items[windowed.info.file_id] = windowed
                return windowed
            raw_bytes = await source.read_bytes(uri)
        else:
            raw_bytes = None

        loaded = self._decode_audio(
            source.source_id,
            uri,
            name,
            local_path,
            raw_bytes,
            start_sec=start_sec,
            max_duration_sec=max_duration_sec,
        )
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
        seg = loaded.samples[channel, i0:i1]
        n = seg.shape[0]
        bins = max(1, min(num_bins, n))
        edges = np.linspace(0, n, bins + 1, dtype=np.int64)
        mins: list[float] = []
        maxs: list[float] = []
        for b in range(bins):
            chunk = seg[edges[b] : edges[b + 1]]
            if chunk.size == 0:
                mins.append(0.0)
                maxs.append(0.0)
            else:
                mins.append(float(np.min(chunk)))
                maxs.append(float(np.max(chunk)))
        return WaveformOverview(
            file_id=file_id,
            channel=channel,
            num_bins=bins,
            start_sec=s_sec,
            end_sec=e_sec,
            min_values=mins,
            max_values=maxs,
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
        total_frames = loaded.info.num_frames
        i0 = max(0, min(total_frames, int(round(min(start_sec, end_sec) * sr))))
        i1 = max(i0 + 1, min(total_frames, int(round(max(start_sec, end_sec) * sr))))
        ch_indices = list(channels) if channels else list(range(loaded.info.num_channels))
        for ch in ch_indices:
            if ch < 0 or ch >= loaded.info.num_channels:
                raise ValueError(f"Channel {ch} out of range")
        sliced = loaded.samples[ch_indices, i0:i1]
        buf = io.BytesIO()
        sf.write(buf, sliced.T, sr, format="WAV", subtype="PCM_16")
        return buf.getvalue()
