# SyrinxOne: A Cloud-Ready Bioacoustic Workstation for Research & Collaboration

## Executive Summary

Modern bioacoustics has outgrown traditional file-by-file desktop workflows. Marine hydrophone observatories, terrestrial autonomous recording units (ARUs), and distributed microphone arrays now generate terabytes of audio stored across AWS S3, Google Cloud Storage, Google Drive, Zenodo, Figshare, Hugging Face, and citizen-science repositories. Yet researchers still face a costly divide: established desktop tools (such as classic **Syrinx** and **Raven**) require downloading large archives locally before inspection, while lightweight web audio players lack the spectrogram responsiveness, quantitative measurement tools, multi-channel alignment, and annotation interoperability required for rigorous acoustic science.

**SyrinxOne** bridges that gap. Inspired by the speed and visual clarity of classic Win32 Syrinx, SyrinxOne is a cross-platform bioacoustic spectrogram visualization, playback, measurement, and annotation workstation that runs **directly in any modern web browser** (desktop, tablet, or phone) as well as an **offline desktop application**. It connects directly to local disks, shared Google Drive folders, and major public scientific repositories—allowing collaborators and external stakeholders to discover, inspect, measure, and annotate terabyte-scale acoustic datasets with zero software installation and without bulk downloads.

- **Live Web Application:** https://syrinx-web-466330924576.us-west1.run.app
- **Source Repository:** https://github.com/quill65/SyrinxOne

---

## Core Capabilities Today

### 1. Fast, High-Fidelity Spectrogram Inspection
- **GPU-Accelerated Visualization:** Renders high-resolution spectrograms smoothly in the browser using WebGL2, with selectable scientific colormaps, time-scale modes, and multi-window comparisons.
- **Intuitive Gain & Floor Controls:** Streamlined **Gain (`-60` to `+80 dB`)** and **Floor (`0` to `95%` noise cutoff)** controls—paired with noise-anchored **Auto Gain** and classic Syrinx keyboard shortcuts—let analysts quickly separate faint wildlife vocalizations from ambient background noise.
- **Multi-Window & Multi-Channel Workspaces:** Desktop users can open multiple recordings simultaneously, link windows across time and frequency, split long recordings across stacked rows, or stack synchronized channels vertically (`Stack All Channels`). Mobile and tablet users get a streamlined 3-pane touch and swipe interface for field review.

### 2. Zero-Download Cloud & Scientific Archive Discovery
- **Prompt-Based Archive Search & Live Compatibility Testing:** Instead of hunting through disconnected portals, users can search in plain language (for example, *"distributed microphone array"*, *"terrestrial birdsong"*, or *"rainforest elephants"*) across seven integrated providers: an **Open Bioacoustic Catalog**, **Zenodo**, **Figshare**, **Hugging Face**, **iNaturalist**, **Xeno-canto**, and **Freesound**. SyrinxOne automatically probes candidate datasets—inspecting nested folder hierarchies and sampling WAV headers over HTTP `Range`—to confirm streamability, sample rate, channel count, and format before connecting.
- **Direct Streaming Inside Large Remote Archives:** SyrinxOne browses inside multi-gigabyte ZIP archives on Zenodo and Figshare (such as 16-channel DEMAND environment ZIPs or the 6.75 GB 61-AudioMoth validation archive) by reading only the archive index and streaming individual audio tracks on demand. It also streams Parquet-backed Hugging Face datasets via the Hugging Face Datasets Server, handles compressed web/field formats (`.m4a`, `.opus`, `.mp3`, `.flac`, `.ogg`, `.wav`), and connects directly to **AWS S3**, **Google Cloud Storage**, **Google Drive (OAuth)**, and direct audio URLs.
- **Flexible Access for Authenticated Repositories:** Archives requiring personal or institutional API keys (such as Xeno-canto v3 or Freesound v2) are supported generically through a local browser key manager or server-side deployment credentials—allowing labs to plug in private or partner repositories without custom UI changes.

### 3. Intelligent Organization of Multi-File & Multi-Channel Recordings
- **Automatic Structure Parsing:** Real-world acoustic datasets often split continuous deployments across thousands of timestamped files or separate per-channel tracks (such as `CH1`–`CH6` or `ch01`–`ch16`). SyrinxOne inspects filenames, directory trees, and archive `README` documentation to identify dates, sites/environments, sessions, and channel markers.
- **User-Defined Recording Granularity:** Researchers choose how files combine into first-class recordings—for example, stacking parallel microphone tracks into synchronized multi-channel recordings, or concatenating sequential files by **hour, day, month, year, or site**.
- **Long-Timeline Random Access & Memory:** Combined multi-day or multi-month recordings load instantaneously via sparse spectral-line sampling and seamlessly transition to full resolution on zoom. SyrinxOne remembers each source's chosen recording structure and last-viewed timestamps so collaborators immediately return to organized recordings rather than raw file trees.

### 4. Quantitative Analysis, Detection, Clipping & Legacy Interoperability
- **Measurements & Sound Detection:** Any time-frequency selection yields immediate quantitative metrics (duration, bandwidth, peak and centroid frequency, `Q5`/`Q50`/`Q95` energy quartiles, `BW90`, RMS/peak dBFS, noise floor, and estimated SNR). A configurable band-limited energy detector with click/pop filtering automates event detection across single or combined multi-file recordings.
- **Syrinx & Raven Annotation Round-Trip:** Full import/export fidelity with **classic Syrinx** tab-delimited annotation logs (preserving custom fields, escaped notes, and non-annotation header lines) alongside **Raven Selection Tables (TSV)**, **CSV**, and **JSON**, plus hotkey stamping and tokenized **Klip-n-Save** WAV clip extraction.
- **AI Acoustic Explorer:** Combines deterministic acoustic measurements with optional Gemini multimodal spectrogram interpretation to assist with exploratory review, taxonomic hypotheses, and detector parameter tuning.

---

## Key Research & Stakeholder Use Cases

| Use Case | How SyrinxOne Helps | Representative Datasets Supported Today |
|---|---|---|
| **Long-Duration Passive Acoustic Monitoring (PAM)** | Browse multi-year cloud buckets, group recordings by day/month/year, scan long timelines via sparse Range streaming without downloading gigabytes, and run band-limited detectors. | MBARI MARS Pacific Ocean Sound (2 kHz / 16 kHz / 256 kHz), NOAA NCEI SanctSound, Cornell Congo Rainforest (Elephant Listening Project), The Wilds Terrestrial PAM |
| **Distributed Microphone Arrays & Localization** | Automatically stack separate per-channel WAVs into synchronized multi-channel views, inspect time-aligned spectrograms across receivers, and access companion coordinate/detection tables. | OpenSoundscape 9-Mic Localization Array, DEMAND 16-Channel Array (Zenodo & Hugging Face), CHiME-3 6-Mic Array, 61-AudioMoth Forest Validation Archive |
| **Species Reference & Clip Curation** | Search and stream individual wildlife recordings directly by taxon or sound type, compare harmonic structure side-by-side, and export labeled clips or annotation tables. | iNaturalist (1.4M+ wildlife sound observations), Xeno-canto (API v3), Freesound (API v2) |
| **Multi-Institution Annotation & QA Reviews** | Open existing Syrinx or Raven logs directly against shared Google Drive folders or cloud buckets in a browser—eliminating OS/install friction for external reviewers and students. | Lab Google Drive folders, institutional S3/GCS buckets, local hard drives (Electron desktop) |
| **Field Deployment Verification** | Upload or inspect newly collected SD-card or cloud-synced recordings on a phone or tablet to check recorder health, gain levels, and ambient noise in the field. | Local uploads, Google Drive field sync folders |

---

## Architecture, Reliability & Collaboration Roadmap

SyrinxOne separates its **shared web/desktop interface** (React, TypeScript, WebGL2) from a modular **Python acoustic engine** (FastAPI, NumPy/SciPy, `libsndfile`, `ffmpeg`, pluggable storage and discovery adapters). This design keeps heavy audio I/O and format decoding server-side (solving browser CORS and large-file bottlenecks) while supporting identical workflows on Google Cloud Run, institutional servers, or offline laptops via Electron. Automated test suites continuously validate core DSP, Syrinx/Raven log round-trips, multi-file recording structure grouping, and archive discovery/verification.

### Priority Opportunities for Research Collaboration
1. **Spatial Array Mapping & TDOA Localization:** Render receiver coordinate tables (WGS84 / UTM) alongside synchronized multi-channel spectrograms and integrate interactive TDOA solvers (e.g., Sound Finder / GCC-PHAT) for end-to-end acoustic localization.
2. **Institutional & Private Archive Connectors:** Add turnkey connectors for partner repositories, authenticated S3/GCS/Earthdata buckets, or specialized formats (such as MATLAB `.mat` acoustic workspaces).
3. **Automated Classifier Integration & Active Learning:** Connect species classification models (such as BirdNET, Perch, or OpenSoundscape CNNs) directly to SyrinxOne's annotation review and hotkey verification workflow.
4. **Offline Mobile Field App:** Package the existing mobile-responsive workspace for fully offline field deployment and Bluetooth audio inspection.
