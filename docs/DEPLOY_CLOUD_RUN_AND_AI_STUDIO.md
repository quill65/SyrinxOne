# Deploying SyrinxOne Backend to Cloud Run & Vibe-Coding in Google AI Studio

This setup lets you run the Python DSP + Cloud Archive Engine (`backend/python`) on **Google Cloud Run** (scales to zero when idle) while vibe-coding and testing the Web UI (`apps/web`) from your phone or desktop in **Google AI Studio Build** or any mobile browser.

## 1. Deploy the Python Engine to Google Cloud Run

From `c:\Users\john\Documents\projects\SyrinxOne\backend\python` (with `gcloud` authenticated to your GCP project):

```powershell
gcloud run deploy syrinx-engine `
  --source . `
  --region us-west1 `
  --allow-unauthenticated `
  --memory 2Gi `
  --cpu 2 `
  --timeout 180 `
  --set-env-vars "SYRINX_DISABLE_LOCAL_FS=1,GEMINI_API_KEY=YOUR_GEMINI_KEY"
```

*(Optional security: add `,SYRINX_API_TOKEN=your-secret-token` to `--set-env-vars` so only requests with your Bearer token can call the engine.)*

Cloud Run prints a service URL such as:
`https://syrinx-engine-xxxxxxxxxx-uw.a.run.app`

Verify it is live:
`https://syrinx-engine-xxxxxxxxxx-uw.a.run.app/api/health`

## 2. Connect the Web App (Phone Browser or AI Studio Build)

You have three ways to point the Web App at your Cloud Run engine:

1. **In-App Engine Switcher (no rebuild needed)**:
   - Tap the **S1 SyrinxOne** badge (with the green/red connection dot) in the top-left toolbar.
   - Paste your Cloud Run URL (and optional Bearer token), then tap **Save & Test Connection**.
   - Saved in browser `localStorage` automatically.
2. **One-Tap Phone Bookmark**:
   - Open your web app with `?api=https://syrinx-engine-xxxxxxxxxx-uw.a.run.app` (and optional `&token=...`)—SyrinxOne automatically saves and connects to it.
3. **Environment Variable in AI Studio Build / Vite**:
   - Set `VITE_API_URL=https://syrinx-engine-xxxxxxxxxx-uw.a.run.app` in your web deployment or AI Studio environment settings.

## 3. Mobile Browser Controls (`<= 768px`)

- **Single-Pane Tabbed View**: Open recordings appear as tabs along the top instead of floating MDI windows.
- **Touch Controls HUD** (top-right of spectrogram):
  - **Select**: 1-finger drag draws an acoustic selection box (live measurements update automatically).
  - **Pan**: 1-finger drag pans time and frequency.
  - **2-Finger Pinch**: Zooms time smoothly around the pinch midpoint.
  - **Double-Tap**: Plays the active selection or visible view.
  - **+ / − / Fit / +Log**: Quick one-handed zoom and annotation buttons.
- **Slide-Over Panels**: Tapping **AI Explorer**, **Settings/Measurements**, or **Annotations** opens a full-width sheet on phones without squishing the spectrogram.
