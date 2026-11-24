# SyrinxOne Architecture

**SyrinxOne** is a multi-platform rewrite of the classic Win32 VC++ bioacoustics application **Syrinx**, built to maximize code sharing across Desktop, Web, and future Mobile platforms while keeping DSP and scientific workflows extensible in Python.

## Platform Matrix

| Platform | Shell / Host | UI Codebase | Engine / DSP | Primary Use Case |
| --- | --- | --- | --- | --- |
| **Desktop** | Electron (`apps/desktop-electron`) | Shared React + WebGL2 + MDI (`apps/web`) | Local Python sidecar (`backend/python`, FastAPI + uvicorn) | Local audio files, sound cards & triggered recording (Phase 2), Python DSP scripts |
| **Web** | Browser SPA (`apps/web`) | Shared React + WebGL2 + MDI (`apps/web`) | Cloud Python backend (`backend/python`) | Visualizing & annotating cloud acoustic datasets |
| **Mobile (Phase 2+)** | Capacitor (planned) | Same React + WebGL2 UI (single-pane mobile mode) | Native Bluetooth audio bridge + client-side WASM/JS FFT | Offline field recording & real-time spectrograms |

## Architectural Boundaries

1. **Shared TypeScript Contracts (`packages/shared`)**:
   - Defines `AudioEngineClient`, audio metadata, spectrogram parameters, Syrinx-compatible annotations, hotkeys, and classic MDI window layout state.
   - Keeps the React UI decoupled from transport and storage details so Desktop IPC, Cloud HTTPS, and future offline Mobile engines plug in behind the same interface.

2. **Pluggable Python Backend (`backend/python`)**:
   - **Storage Adapters (`storage/`)**: `AudioDataSource` and `AnnotationStore` ABCs supporting `LocalFilesystemSource`, `S3AudioDataSource`, `GCSAudioDataSource`, `HttpUrlAudioDataSource`, and custom Python lab plugins.
   - **Dual-Mode DSP (`dsp/`)**:
     - `modern_stft` (default): NumPy/SciPy Float32 dB STFT streamed as compact binary Float32 frames to WebGL2 shaders for instant 60fps contrast/gain/threshold/colormap/zoom adjustments.
     - `classic_syrinx`: Exact windowing, magnitude scaling, and pop filter ported from legacy `FFTTools.cpp` / `SpecProfile.cpp`.
     - `server_bitmap` fallback: Server-rendered PNG spectrogram tiles when GPU rendering is disabled or exact bitmap parity is preferred.
   - **Annotations (`annotations/`)**: Faithful round-trip compatibility with classic Syrinx `.txt`/`.log` files (`fieldkey:` + `an:` and legacy `key: value` pair format, `@`/`\x02` newline escaping, custom extra fields, preserved non-annotation header lines) plus Raven selection tables, CSV, and JSON.

3. **Classic MDI Workspace (`apps/web`)**:
   - Floating, draggable, resizable, z-ordered child windows inside an MDI canvas with Tile Grid, Tile Horizontal, Tile Vertical, Cascade, Resize All to Active, Fit Screen, Titlebar toggles, and synchronized linked views.

## Phase 1.5 — Web Acoustic Viewer, Cloud Archive Connectors & AI Explorer

- **Web-First Cloud & Archive Connectors**:
  - Runtime source management (`POST/DELETE /api/sources`) with 1-click public bioacoustic archive presets (**MBARI Pacific Ocean Sound on AWS Open Data**, **NOAA NCEI Passive Acoustic Data Archive on GCS**) using anonymous public S3/GCS HTTPS APIs (zero cloud credentials required).
  - **Google Drive** connector (public/shared file & folder links + optional API key / OAuth token for private folders), **Direct Audio URL** paste, and workspace drag-and-drop.
  - Large archive file windowed loading (HTTP `Range` partial WAV/FLAC reads so multi-hundred-MB hydrophone files open rapidly in the browser) and paginated/searchable directory browsing.
- **Syrinx Display, Acoustic Measurements & Annotation Polish**:
  - Server-computed **Acoustic Measurement Inspector** (`peakFreqHz`, `centroidFreqHz`, `bw90Hz`, `q5/q50/q95FreqHz`, `rmsDb`, `peakDb`, `snrDb`, `timeOfPeakSec`) with 1-click logging into Syrinx/Raven annotation `extraFields`.
  - Browser auto-save recovery for annotations (`localStorage`) alongside Syrinx `.txt`, Raven, CSV, and JSON import/export.
- **AI Bioacoustic Analytic Explorer**:
  - Multimodal side panel pairing Python DSP measurements + clean spectrogram PNG + annotations with Gemini for deeper acoustic interpretation, pattern summaries, label suggestions, and 1-click **Detect Sounds** parameter recommendations.

## Phase 2+ Roadmap

- Live desktop sound-card capture & triggered event recording (`TriggerEventDetector`, `CircularBuffer`)
- Audio editing/DSP tools (bandpass/highpass/lowpass filters, amp/atten, ramps, amplitude envelopes, insert tone/noise/silence)
- Multi-GB streaming & classic `MetaFileGroup` multi-file series timelines
- Playlists, experimental playback trials, and multi-user cloud authentication
- Mobile app (Capacitor + native Bluetooth audio + offline real-time spectrograms)
