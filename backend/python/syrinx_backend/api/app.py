from __future__ import annotations

import os
from pathlib import Path

from fastapi import Depends, FastAPI, File, HTTPException, Query, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware

from syrinx_backend import __version__
from syrinx_backend.ai.explorer import analyze_recording_with_ai
from syrinx_backend.annotations.formats import (
    export_annotations,
    import_annotations,
    infer_annotation_format,
)
from syrinx_backend.api.auth import verify_token_dependency
from syrinx_backend.audio.loader import AudioSessionCache, expand_clip_template
from syrinx_backend.dsp.detector import detect_sound_events
from syrinx_backend.dsp.measurements import measure_acoustic_selection
from syrinx_backend.dsp.stft_engine import compute_spectrogram, render_spectrogram_png
from syrinx_backend.models import (
    AiAnalyzeRequest,
    AiAnalyzeResponse,
    AnnotationDocument,
    AudioFileInfo,
    BrowseEntry,
    ClipSaveRequest,
    ClipSaveResponse,
    ColormapName,
    CreateSourceRequest,
    DataSourceInfo,
    DetectSoundsRequest,
    DetectSoundsResponse,
    DiscoverSourcesRequest,
    DiscoverSourcesResponse,
    DiscoveryProviderInfo,
    DspMode,
    ExportAnnotationsTextRequest,
    ImportAnnotationsSourceRequest,
    ImportAnnotationsTextRequest,
    MeasureSelectionRequest,
    OpenAudioRequest,
    SaveAnnotationsSourceRequest,
    SelectionMeasurements,
    SourceVerificationResult,
    WaveformOverview,
    WindowType,
)
from syrinx_backend.storage.base import SourceRegistry
from syrinx_backend.storage.cloud_adapters import (
    GCSAudioDataSource,
    HttpUrlAudioDataSource,
    S3AudioDataSource,
    load_custom_source_plugins,
)
from syrinx_backend.storage.discovery import (
    DiscoveryRegistry,
    build_default_discovery_registry,
    build_source_from_request,
    discover_online_sources,
    verify_audio_source,
)
from syrinx_backend.storage.local_fs import LocalFilesystemSource


def _build_default_registry() -> SourceRegistry:
    registry = SourceRegistry()

    disable_local = os.environ.get("SYRINX_DISABLE_LOCAL_FS", "0") == "1"
    if not disable_local:
        default_root = os.environ.get("SYRINX_DEFAULT_DIR")
        restrict = os.environ.get("SYRINX_RESTRICT_LOCAL_ROOT", "0") == "1"
        registry.register(
            LocalFilesystemSource(
                source_id="local",
                display_name="Local Filesystem",
                root_dir=default_root if default_root else None,
                restrict_to_root=restrict,
            )
        )

    # Built-in public bioacoustic archives (anonymous HTTPS read, zero credentials required)
    registry.register(
        S3AudioDataSource(
            source_id="mbari_16khz",
            bucket="pacific-sound-16khz",
            region="us-west-2",
            display_name="MBARI Pacific Ocean Sound (16 kHz, AWS)",
            description="MBARI Monterey Bay MARS cabled hydrophone decimated 16 kHz recordings (AWS Open Data)",
            anonymous=True,
            read_only=True,
            removable=False,
        )
    )
    registry.register(
        S3AudioDataSource(
            source_id="mbari_2khz",
            bucket="pacific-sound-2khz",
            region="us-west-2",
            display_name="MBARI Pacific Ocean Sound (2 kHz Whales, AWS)",
            description="MBARI Monterey Bay 2 kHz low-frequency archive (blue/fin/humpback whales)",
            anonymous=True,
            read_only=True,
            removable=False,
        )
    )
    registry.register(
        GCSAudioDataSource(
            source_id="noaa_ncei",
            bucket="noaa-passive-bioacoustic",
            display_name="NOAA NCEI Passive Acoustic Archive (GCS)",
            description="NOAA National Centers for Environmental Information Passive Acoustic Data Archive (SanctSound, SoundCoop)",
            anonymous=True,
            read_only=True,
            removable=False,
        )
    )

    s3_bucket = os.environ.get("SYRINX_S3_BUCKET")
    if s3_bucket:
        registry.register(
            S3AudioDataSource(
                source_id="s3",
                bucket=s3_bucket,
                prefix=os.environ.get("SYRINX_S3_PREFIX", ""),
                region=os.environ.get("SYRINX_S3_REGION"),
                endpoint_url=os.environ.get("SYRINX_S3_ENDPOINT_URL"),
                anonymous=os.environ.get("SYRINX_S3_ANONYMOUS", "0") == "1",
            )
        )

    gcs_bucket = os.environ.get("SYRINX_GCS_BUCKET")
    if gcs_bucket:
        registry.register(
            GCSAudioDataSource(
                source_id="gcs",
                bucket=gcs_bucket,
                prefix=os.environ.get("SYRINX_GCS_PREFIX", ""),
                anonymous=os.environ.get("SYRINX_GCS_ANONYMOUS", "0") == "1",
            )
        )

    http_catalog = os.environ.get("SYRINX_HTTP_URLS", "")
    urls = [u.strip() for u in http_catalog.split(",") if u.strip()] if http_catalog.strip() else []
    registry.register(HttpUrlAudioDataSource(source_id="http", catalog_urls=urls))

    plugin_specs = os.environ.get("SYRINX_SOURCE_PLUGINS", "")
    if plugin_specs.strip():
        load_custom_source_plugins(registry, plugin_specs.split(","))

    return registry


def create_app(
    registry: SourceRegistry | None = None,
    api_token: str | None = None,
    discovery_registry: DiscoveryRegistry | None = None,
) -> FastAPI:
    app = FastAPI(title="SyrinxOne Engine API", version=__version__)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=[
            "X-Spec-Time-Bins",
            "X-Spec-Freq-Bins",
            "X-Spec-Time-Step",
            "X-Spec-Freq-Step",
            "X-Spec-Start-Sec",
            "X-Spec-End-Sec",
            "X-Spec-Min-Freq",
            "X-Spec-Max-Freq",
            "X-Spec-Min-Val",
            "X-Spec-Max-Val",
        ],
    )

    source_registry = registry or _build_default_registry()
    disc_registry = discovery_registry or build_default_discovery_registry()
    audio_cache = AudioSessionCache()
    auth_dep = Depends(verify_token_dependency(api_token))

    app.state.registry = source_registry
    app.state.discovery_registry = disc_registry
    app.state.audio_cache = audio_cache

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/api/sources", response_model=list[DataSourceInfo], dependencies=[auth_dep])
    async def list_sources() -> list[DataSourceInfo]:
        return source_registry.list_info()

    @app.get(
        "/api/sources/providers",
        response_model=list[DiscoveryProviderInfo],
        dependencies=[auth_dep],
    )
    async def list_discovery_providers() -> list[DiscoveryProviderInfo]:
        return disc_registry.list_info()

    @app.post(
        "/api/sources/discover",
        response_model=DiscoverSourcesResponse,
        dependencies=[auth_dep],
    )
    async def discover_sources(req: DiscoverSourcesRequest) -> DiscoverSourcesResponse:
        return await discover_online_sources(disc_registry, req)

    @app.post(
        "/api/sources/verify",
        response_model=SourceVerificationResult,
        dependencies=[auth_dep],
    )
    async def verify_source(req: CreateSourceRequest) -> SourceVerificationResult:
        try:
            source = build_source_from_request(req)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return await verify_audio_source(source)

    @app.post("/api/sources", response_model=DataSourceInfo, dependencies=[auth_dep])
    async def create_source(req: CreateSourceRequest) -> DataSourceInfo:
        try:
            source = build_source_from_request(req)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        source_registry.register(source)
        return source.info()

    @app.delete("/api/sources/{source_id}", dependencies=[auth_dep])
    async def delete_source(source_id: str) -> dict[str, str]:
        try:
            source_registry.unregister(source_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        return {"removed": source_id}

    @app.get(
        "/api/sources/{source_id}/browse",
        response_model=list[BrowseEntry],
        dependencies=[auth_dep],
    )
    async def browse_source(
        source_id: str,
        prefix: str = "",
        limit: int = Query(default=250, ge=1, le=1000),
    ) -> list[BrowseEntry]:
        try:
            source = source_registry.get(source_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        try:
            return await source.list_items(prefix, limit=limit)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Failed to browse source: {exc}") from exc

    @app.post("/api/audio/open", response_model=AudioFileInfo, dependencies=[auth_dep])
    async def open_audio(req: OpenAudioRequest) -> AudioFileInfo:
        try:
            source = source_registry.get(req.source_id)
            loaded = await audio_cache.open_from_source(
                source,
                req.uri,
                start_sec=req.start_sec,
                max_duration_sec=req.max_duration_sec,
            )
            return loaded.info
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Failed to open audio: {exc}") from exc

    @app.post("/api/audio/upload", response_model=AudioFileInfo, dependencies=[auth_dep])
    async def upload_audio(file: UploadFile = File(...)) -> AudioFileInfo:
        raw = await file.read()
        try:
            loaded = audio_cache.register_uploaded_bytes(file.filename or "audio.wav", raw)
            return loaded.info
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Failed to decode audio: {exc}") from exc

    @app.get(
        "/api/audio/{file_id}/waveform",
        response_model=WaveformOverview,
        dependencies=[auth_dep],
    )
    async def get_waveform(
        file_id: str,
        channel: int = Query(default=0, ge=0),
        num_bins: int = Query(default=2000, ge=16, le=20000),
        start_sec: float | None = Query(default=None),
        end_sec: float | None = Query(default=None),
    ) -> WaveformOverview:
        try:
            return audio_cache.compute_waveform(file_id, channel, num_bins, start_sec, end_sec)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/audio/{file_id}/spectrogram", dependencies=[auth_dep])
    async def get_spectrogram(
        file_id: str,
        channel: int = Query(default=0, ge=0),
        fft_size: int = Query(default=512, ge=32, le=16384),
        hop_size: int = Query(default=128, ge=1, le=16384),
        window_type: WindowType = Query(default="hann"),
        dsp_mode: DspMode = Query(default="modern_stft"),
        pop_filter: bool = Query(default=False),
        start_sec: float | None = Query(default=None),
        end_sec: float | None = Query(default=None),
        min_freq_hz: float | None = Query(default=None),
        max_freq_hz: float | None = Query(default=None),
    ) -> Response:
        try:
            loaded = audio_cache.get(file_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if channel >= loaded.info.num_channels:
            raise HTTPException(status_code=400, detail="Channel index out of range")

        spec = compute_spectrogram(
            loaded.samples[channel],
            sample_rate=loaded.info.sample_rate,
            fft_size=fft_size,
            hop_size=hop_size,
            window_type=window_type,
            dsp_mode=dsp_mode,
            pop_filter=pop_filter,
            start_sec=start_sec,
            end_sec=end_sec,
            min_freq_hz=min_freq_hz,
            max_freq_hz=max_freq_hz,
        )
        headers = {
            "X-Spec-Time-Bins": str(spec.matrix.shape[0]),
            "X-Spec-Freq-Bins": str(spec.matrix.shape[1]),
            "X-Spec-Time-Step": f"{spec.time_step_sec:.9f}",
            "X-Spec-Freq-Step": f"{spec.freq_step_hz:.6f}",
            "X-Spec-Start-Sec": f"{spec.start_sec:.6f}",
            "X-Spec-End-Sec": f"{spec.end_sec:.6f}",
            "X-Spec-Min-Freq": f"{spec.min_freq_hz:.3f}",
            "X-Spec-Max-Freq": f"{spec.max_freq_hz:.3f}",
            "X-Spec-Min-Val": f"{spec.min_value:.4f}",
            "X-Spec-Max-Val": f"{spec.max_value:.4f}",
        }
        return Response(
            content=spec.matrix.astype("<f4", copy=False).tobytes(),
            media_type="application/octet-stream",
            headers=headers,
        )

    @app.get("/api/audio/{file_id}/spectrogram.png", dependencies=[auth_dep])
    async def get_spectrogram_png(
        file_id: str,
        channel: int = Query(default=0, ge=0),
        fft_size: int = Query(default=512, ge=32, le=16384),
        hop_size: int = Query(default=128, ge=1, le=16384),
        window_type: WindowType = Query(default="hann"),
        dsp_mode: DspMode = Query(default="modern_stft"),
        pop_filter: bool = Query(default=False),
        colormap: ColormapName = Query(default="grayscale_bow"),
        gain_db: float = Query(default=0.0),
        floor: float = Query(default=0.0, ge=0.0, le=95.0),
        start_sec: float | None = Query(default=None),
        end_sec: float | None = Query(default=None),
        min_freq_hz: float | None = Query(default=None),
        max_freq_hz: float | None = Query(default=None),
    ) -> Response:
        try:
            loaded = audio_cache.get(file_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if channel >= loaded.info.num_channels:
            raise HTTPException(status_code=400, detail="Channel index out of range")

        spec = compute_spectrogram(
            loaded.samples[channel],
            sample_rate=loaded.info.sample_rate,
            fft_size=fft_size,
            hop_size=hop_size,
            window_type=window_type,
            dsp_mode=dsp_mode,
            pop_filter=pop_filter,
            start_sec=start_sec,
            end_sec=end_sec,
            min_freq_hz=min_freq_hz,
            max_freq_hz=max_freq_hz,
        )
        png_bytes = render_spectrogram_png(
            spec,
            colormap=colormap,
            gain_db=gain_db,
            floor=floor,
        )
        return Response(content=png_bytes, media_type="image/png")

    @app.get("/api/audio/{file_id}/spectrogram.tsv", dependencies=[auth_dep])
    async def export_spectrogram_tsv(
        file_id: str,
        channel: int = Query(default=0, ge=0),
        fft_size: int = Query(default=512, ge=32, le=16384),
        hop_size: int = Query(default=128, ge=1, le=16384),
        window_type: WindowType = Query(default="hann"),
        dsp_mode: DspMode = Query(default="modern_stft"),
        pop_filter: bool = Query(default=False),
        start_sec: float | None = Query(default=None),
        end_sec: float | None = Query(default=None),
        min_freq_hz: float | None = Query(default=None),
        max_freq_hz: float | None = Query(default=None),
    ) -> Response:
        try:
            loaded = audio_cache.get(file_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if channel >= loaded.info.num_channels:
            raise HTTPException(status_code=400, detail="Channel index out of range")

        spec = compute_spectrogram(
            loaded.samples[channel],
            sample_rate=loaded.info.sample_rate,
            fft_size=fft_size,
            hop_size=hop_size,
            window_type=window_type,
            dsp_mode=dsp_mode,
            pop_filter=pop_filter,
            start_sec=start_sec,
            end_sec=end_sec,
            min_freq_hz=min_freq_hz,
            max_freq_hz=max_freq_hz,
        )
        t_bins, f_bins = spec.matrix.shape
        freqs = [spec.min_freq_hz + i * spec.freq_step_hz for i in range(f_bins)]
        lines = ["time_sec\t" + "\t".join(f"{f:.1f}Hz" for f in freqs)]
        for ti in range(t_bins):
            t_val = spec.start_sec + ti * spec.time_step_sec
            vals = "\t".join(f"{float(v):.2f}" for v in spec.matrix[ti])
            lines.append(f"{t_val:.6f}\t{vals}")
        return Response(content="\n".join(lines) + "\n", media_type="text/tab-separated-values")

    @app.post(
        "/api/audio/{file_id}/detect",
        response_model=DetectSoundsResponse,
        dependencies=[auth_dep],
    )
    async def detect_sounds(file_id: str, req: DetectSoundsRequest) -> DetectSoundsResponse:
        try:
            loaded = audio_cache.get(file_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if req.channel < 0 or req.channel >= loaded.info.num_channels:
            raise HTTPException(status_code=400, detail="Channel index out of range")

        detections, noise_floor = detect_sound_events(
            loaded.samples[req.channel],
            sample_rate=loaded.info.sample_rate,
            soundfile_name=loaded.info.name,
            channel_1based=req.channel + 1,
            fft_size=req.fft_size,
            hop_size=req.hop_size,
            min_freq_hz=req.min_freq_hz,
            max_freq_hz=req.max_freq_hz,
            trigger_threshold_db=req.trigger_threshold_db,
            min_trigger_dur_sec=req.min_trigger_dur_sec,
            min_sound_dur_sec=req.min_sound_dur_sec,
            max_silence_gap_sec=req.max_silence_gap_sec,
            pre_buffer_sec=req.pre_buffer_sec,
            post_buffer_sec=req.post_buffer_sec,
            pop_filter=req.pop_filter,
            start_sec=req.start_sec,
            end_sec=req.end_sec,
            comment=req.comment,
        )

        saved_clips: list[str] = []
        if req.save_clips and detections:
            try:
                source = source_registry.get(req.target_source_id)
            except KeyError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            for idx, det in enumerate(detections, start=1):
                wav_bytes = audio_cache.slice_to_wav_bytes(
                    file_id, det.start_time_sec, det.end_time_sec, [req.channel]
                )
                fname = expand_clip_template(
                    f"{req.clip_name_template}_{idx:03d}",
                    source_filename=loaded.info.name,
                    channel_1based=req.channel + 1,
                    comment=det.comment,
                )
                if req.target_directory:
                    uri = str(Path(req.target_directory) / fname)
                elif not loaded.info.uri.startswith("upload://"):
                    uri = str(Path(loaded.info.uri).parent / fname)
                else:
                    uri = fname
                saved_uri = await source.write_bytes(uri, wav_bytes)
                saved_clips.append(saved_uri)

        return DetectSoundsResponse(
            detections=detections,
            noise_floor_db=noise_floor,
            saved_clips=saved_clips,
        )

    @app.get("/api/audio/{file_id}/stream", dependencies=[auth_dep])
    async def stream_audio(file_id: str) -> Response:
        try:
            wav_bytes = audio_cache.get_wav_bytes(file_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return Response(
            content=wav_bytes,
            media_type="audio/wav",
            headers={"Accept-Ranges": "bytes"},
        )

    @app.post("/api/audio/clip-save", response_model=ClipSaveResponse, dependencies=[auth_dep])
    async def clip_and_save(req: ClipSaveRequest) -> ClipSaveResponse:
        try:
            loaded = audio_cache.get(req.file_id)
            wav_bytes = audio_cache.slice_to_wav_bytes(
                req.file_id, req.start_sec, req.end_sec, req.channels
            )
            source = source_registry.get(req.target_source_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        first_ch = (req.channels[0] + 1) if req.channels else 1
        filename = expand_clip_template(
            req.name_template,
            source_filename=loaded.info.name,
            channel_1based=first_ch,
            comment=req.comment,
        )

        if req.target_directory:
            target_uri = str(Path(req.target_directory) / filename)
        elif loaded.info.source_id == req.target_source_id and not loaded.info.uri.startswith(
            "upload://"
        ):
            target_uri = str(Path(loaded.info.uri).parent / filename)
        else:
            target_uri = filename

        try:
            saved_uri = await source.write_bytes(target_uri, wav_bytes)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

        file_info: AudioFileInfo | None = None
        if req.open_in_window:
            opened = await audio_cache.open_from_source(source, saved_uri)
            file_info = opened.info

        return ClipSaveResponse(saved_uri=saved_uri, saved_name=filename, file_info=file_info)

    @app.post(
        "/api/annotations/import-text",
        response_model=AnnotationDocument,
        dependencies=[auth_dep],
    )
    async def import_annotations_text(req: ImportAnnotationsTextRequest) -> AnnotationDocument:
        try:
            return import_annotations(req.content, req.format)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Annotation parse error: {exc}") from exc

    @app.post(
        "/api/annotations/import-source",
        response_model=AnnotationDocument,
        dependencies=[auth_dep],
    )
    async def import_annotations_source(
        req: ImportAnnotationsSourceRequest,
    ) -> AnnotationDocument:
        try:
            source = source_registry.get(req.source_id)
            raw = (await source.read_bytes(req.uri)).decode("utf-8", errors="replace")
            fmt = req.format or infer_annotation_format(req.uri, raw)
            doc = import_annotations(raw, fmt)
            doc.source_uri = req.uri
            return doc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/annotations/export-text", dependencies=[auth_dep])
    async def export_annotations_text(req: ExportAnnotationsTextRequest) -> dict[str, str]:
        content = export_annotations(
            req.annotations,
            req.format,
            non_annotation_lines=req.non_annotation_lines,
            add_time=req.add_time,
            add_date=req.add_date,
        )
        return {"content": content}

    @app.post("/api/annotations/save-source", dependencies=[auth_dep])
    async def save_annotations_source(req: SaveAnnotationsSourceRequest) -> dict[str, object]:
        try:
            source = source_registry.get(req.source_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        content = export_annotations(
            req.annotations,
            req.format,
            non_annotation_lines=req.non_annotation_lines,
        )
        saved_uri = await source.write_bytes(req.uri, content.encode("utf-8"))
        return {"savedUri": saved_uri, "count": len(req.annotations)}

    @app.post(
        "/api/audio/{file_id}/measure",
        response_model=SelectionMeasurements,
        dependencies=[auth_dep],
    )
    async def measure_selection(
        file_id: str, req: MeasureSelectionRequest
    ) -> SelectionMeasurements:
        try:
            loaded = audio_cache.get(file_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if req.channel < 0 or req.channel >= loaded.info.num_channels:
            raise HTTPException(status_code=400, detail="Channel index out of range")
        return measure_acoustic_selection(
            loaded.samples[req.channel],
            sample_rate=loaded.info.sample_rate,
            start_sec=req.start_sec,
            end_sec=req.end_sec,
            low_freq_hz=req.low_freq_hz,
            high_freq_hz=req.high_freq_hz,
            fft_size=req.fft_size,
            hop_size=req.hop_size,
        )

    @app.post(
        "/api/ai/analyze",
        response_model=AiAnalyzeResponse,
        dependencies=[auth_dep],
    )
    async def ai_analyze(req: AiAnalyzeRequest) -> AiAnalyzeResponse:
        try:
            loaded = audio_cache.get(req.file_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        try:
            return await analyze_recording_with_ai(loaded, req)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"AI analysis failed: {exc}") from exc

    return app


app = create_app()
