# Deploying SyrinxOne to Cloud Run & Vibe-Coding in Google AI Studio

This setup runs both the **Python DSP + Acoustic Archive Engine** (`syrinx-engine`) and the **React + WebGL2 Web UI** (`syrinx-web`) on **Google Cloud Run** (scaling to zero when idle), while giving you a one-command standalone export (`npm run export:ai-studio`) for vibe-coding in **Google AI Studio Build** on your phone or browser.

## Live Services (`syrinxone-backend`, `us-west1`)

- **Web UI**: `https://syrinx-web-466330924576.us-west1.run.app`
- **Python DSP & Archive Engine**: `https://syrinx-engine-466330924576.us-west1.run.app`
- **Engine Health Check**: `https://syrinx-engine-466330924576.us-west1.run.app/api/health`

## 1. Redeploy the Python Backend (`syrinx-engine`)

From `backend/python`:

```powershell
gcloud run deploy syrinx-engine `
  --source . `
  --region us-west1 `
  --project syrinxone-backend `
  --allow-unauthenticated `
  --memory 2Gi `
  --cpu 2 `
  --timeout 180 `
  --set-env-vars "SYRINX_DISABLE_LOCAL_FS=1"
```

*(Optional security / server-side AI: run `gcloud run services update syrinx-engine --region us-west1 --project syrinxone-backend --update-env-vars "GEMINI_API_KEY=AIza...,SYRINX_API_TOKEN=your_secret"` — always pair `GEMINI_API_KEY` with `SYRINX_API_TOKEN` on a public URL.)*

## 2. Redeploy the Web UI (`syrinx-web`)

From the repository root (`c:\Users\john\Documents\projects\SyrinxOne`):

```powershell
gcloud builds submit --config cloudbuild.web.yaml --project syrinxone-backend
gcloud run deploy syrinx-web `
  --image us-west1-docker.pkg.dev/syrinxone-backend/cloud-run-source-deploy/syrinx-web:latest `
  --region us-west1 `
  --project syrinxone-backend `
  --allow-unauthenticated `
  --memory 256Mi `
  --cpu 1 `
  --port 8080
```

`cloudbuild.web.yaml` builds `apps/web/Dockerfile` with `VITE_API_URL=https://syrinx-engine-466330924576.us-west1.run.app` baked in and serves the SPA via `apps/web/nginx.conf`.

## 3. Google AI Studio Build Option (Vibe-Coding From Your Phone)

Because the main `master` branch is an npm workspaces monorepo (`packages/shared` + `apps/web` + `apps/desktop-electron`), use the included standalone exporter to generate a flat, single-package Vite + React project ready for **Google AI Studio Build**:

```powershell
npm.cmd run export:ai-studio
```

This creates `dist-ai-studio/` (gitignored on `master`) with:
- Flat `package.json`, `index.html`, `vite.config.ts`, `tsconfig.json`, and `metadata.json`
- `packages/shared/src` inlined into `src/shared/` and aliased as `@syrinxone/shared` so edited UI files copy **1:1 back into `apps/web/src/`** with zero import changes
- `.env` pre-wired to `VITE_API_URL=https://syrinx-engine-466330924576.us-west1.run.app`

### Connecting Google AI Studio Build
1. Open [Google AI Studio — Build](https://aistudio.google.com/apps), click **`+` (Add files)** in the prompt bar -> **Import from GitHub**.
2. Select **`quill65/SyrinxOne-Web:main`** (the dedicated flat companion repo) -> **Import repository**.
3. **Two-Way Sync Between `SyrinxOne` and `SyrinxOne-Web`**:
   - Push desktop monorepo updates out to AI Studio:
     ```powershell
     npm.cmd run ai-studio:push
     ```
   - Pull phone vibe-coded edits from AI Studio back into `apps/web/src` (after syncing to GitHub in AI Studio):
     ```powershell
     npm.cmd run ai-studio:pull
     ```
- **Switching Backend / Token at Runtime**:
  - Tap the **S1 SyrinxOne** badge in the top-left toolbar to change the Cloud Run URL or enter a Bearer token (`localStorage`-persisted).
  - Or share a one-click link: `https://syrinx-web-466330924576.us-west1.run.app/?api=https://syrinx-engine-466330924576.us-west1.run.app&token=YOUR_SECRET` (the `token` parameter is automatically saved to `localStorage` and stripped from the visible address bar).

## 4. Mobile Browser Controls (`<= 768px`)

- **Single-Pane Tabbed View**: Open recordings appear as tabs along the top instead of floating MDI windows.
- **Touch Controls HUD** (top-right of spectrogram):
  - **Select**: 1-finger drag draws an acoustic selection box (live measurements update automatically).
  - **Pan**: 1-finger drag pans time and frequency.
  - **2-Finger Pinch**: Zooms time smoothly around the pinch midpoint.
  - **Double-Tap**: Plays the active selection or visible view.
  - **+ / − / Fit / +Log**: Quick one-handed zoom and annotation buttons.
- **Slide-Over Panels**: Tapping **AI Explorer**, **Settings/Measurements**, or **Annotations** opens a full-width sheet on phones with a close (`X`) button.
