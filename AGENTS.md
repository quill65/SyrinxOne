# SyrinxOne Project Guidelines

## Architecture & Code Sharing
- **Monorepo layout**:
  - `packages/shared` — Single source of truth for cross-platform TypeScript domain types and `AudioEngineClient`. Update contracts here first when changing backend/frontend payloads.
  - `backend/python` — Python (`uv` + `pyproject.toml`, FastAPI) DSP, audio I/O, classic Syrinx/Raven annotation engine, and pluggable storage adapters (`AudioDataSource` / `SourceRegistry`).
  - `apps/web` — Shared React + TypeScript + WebGL2 + Classic MDI UI used by both Cloud Web and Electron Desktop (keep engine/storage/renderer boundaries Capacitor-ready for future Mobile offline + Bluetooth support).
  - `apps/desktop-electron` — Thin Electron desktop shell spawning the local Python sidecar on `127.0.0.1` with a per-session Bearer token.
- **Classic Syrinx C++ Reference**:
  - `syrinx_original_cpp/` (gitignored) is a pruned, **read-only** reference of the active `Syrinx.vcproj` VC++ codebase. Never edit files inside `syrinx_original_cpp/` and never commit it.
  - Maintain round-trip compatibility with classic Syrinx tab-delimited annotation logs (`fieldkey:` + `an:` and legacy `key: value` pairs, `@`/`\x02` notes escaping, custom extra fields, preserved non-annotation header lines).
  - Keep spectrogram rendering modular: primary GPU WebGL2 (`Float32` STFT) while retaining `classic_syrinx` DSP mode and Python `server_bitmap` PNG fallback.

## Tooling & Commands
- **Python**: Always use `uv` (`uv sync`, `uv add`, `uv run`) or `backend/python/.venv/Scripts/python.exe` — never install packages globally with `pip`.
- **Node / npm (Windows)**: Use `npm.cmd` in Windows PowerShell 5.1 if execution policy blocks `npm.ps1`.
- **Electron launching**: Always clear `ELECTRON_RUN_AS_NODE` before spawning Electron (handled by `apps/desktop-electron/scripts/launch.cjs` and `Launch-SyrinxOne.bat`).

## Verification Before Finishing Changes
- Backend: `cd backend/python && .\.venv\Scripts\ruff.exe check syrinx_backend tests && .\.venv\Scripts\pytest.exe -v`
- Frontend & Electron: `npm.cmd run build && npm.cmd run typecheck && npm.cmd run test:js`
