# SyrinxOne

Multi-platform bioacoustic spectrogram visualization, playback, and annotation workstation — a modern rewrite of classic Win32 **Syrinx**.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for full architecture and roadmap details.

## Monorepo Layout

- `packages/shared` — Shared TypeScript domain types and `AudioEngineClient` interface
- `backend/python` — Python (`uv` + `FastAPI`) DSP, audio I/O, classic Syrinx/Raven annotation engine, and pluggable storage adapters
- `apps/web` — Shared React + TypeScript + WebGL2 + Classic MDI frontend
- `apps/desktop-electron` — Electron desktop shell managing the local Python sidecar, native file dialogs, and desktop menus

## Quick Start

### 1. Python Backend
```powershell
cd backend\python
uv sync
uv run uvicorn syrinx_backend.api.app:app --reload --port 8765
```

### 2. Web App (against local or cloud backend)
```powershell
npm install
npm run dev:web
```

### 3. Electron Desktop App (auto-starts local Python sidecar)
```powershell
npm run build
npm run dev:desktop
```

## Running Tests
```powershell
# Backend tests
cd backend\python
uv run pytest -v

# Frontend & shared tests + typecheck
cd ..\..
npm run typecheck
npm run test:js
```
