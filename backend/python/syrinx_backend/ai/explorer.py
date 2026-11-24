from __future__ import annotations

import base64
import json
import os
import re

import httpx

from syrinx_backend.audio.loader import LoadedAudio
from syrinx_backend.dsp.measurements import measure_acoustic_selection
from syrinx_backend.dsp.stft_engine import compute_spectrogram, render_spectrogram_png
from syrinx_backend.models import (
    AiAnalyzeRequest,
    AiAnalyzeResponse,
    SelectionMeasurements,
    SuggestedDetectParams,
)


def _heuristic_detect_params(
    meas: SelectionMeasurements,
    nyquist_hz: float,
) -> SuggestedDetectParams:
    """Derive offline heuristic Detect Sounds settings directly from DSP measurements."""
    pad_hz = max(80.0, meas.bw90_hz * 0.2)
    min_f = max(10.0, meas.q5_freq_hz - pad_hz)
    max_f = min(nyquist_hz, meas.q95_freq_hz + pad_hz)
    if max_f <= min_f + 20.0:
        min_f = max(10.0, meas.low_freq_hz)
        max_f = min(nyquist_hz, meas.high_freq_hz)
    thresh = max(6.0, min(30.0, round(meas.snr_db * 0.55, 1)))
    min_dur = max(0.02, round(min(0.5, meas.duration_sec * 0.45), 3))
    return SuggestedDetectParams(
        min_freq_hz=round(min_f, 1),
        max_freq_hz=round(max_f, 1),
        trigger_threshold_db=thresh,
        min_sound_dur_sec=min_dur,
        max_silence_gap_sec=0.25,
        comment="SimilarEvent",
    )


def _extract_structured_json(
    text: str,
) -> tuple[str, SuggestedDetectParams | None, list[str]]:
    """Extract optional fenced ```syrinx_json ... ``` recommendations from model markdown."""
    pattern = re.compile(r"```syrinx_json\s*(\{.*?\})\s*```", re.DOTALL)
    match = pattern.search(text)
    if not match:
        return text.strip(), None, []

    clean_text = (text[: match.start()] + text[match.end() :]).strip()
    suggested_params: SuggestedDetectParams | None = None
    labels: list[str] = []
    try:
        parsed = json.loads(match.group(1))
        if isinstance(parsed, dict):
            raw_labels = parsed.get("suggestedLabels") or parsed.get("suggested_labels")
            if isinstance(raw_labels, list):
                labels = [str(x).strip() for x in raw_labels if str(x).strip()][:6]
            raw_det = parsed.get("suggestedDetectParams") or parsed.get("suggested_detect_params")
            if isinstance(raw_det, dict):
                suggested_params = SuggestedDetectParams.model_validate(raw_det)
    except Exception:
        pass
    return clean_text, suggested_params, labels


async def analyze_recording_with_ai(
    loaded: LoadedAudio,
    req: AiAnalyzeRequest,
) -> AiAnalyzeResponse:
    """Run Python DSP measurements + optional multimodal Gemini analysis on a recording/selection."""
    info = loaded.info
    ch = max(0, min(info.num_channels - 1, req.channel))
    nyquist = info.sample_rate * 0.5

    # Determine focus region: explicit selection > current viewRange > full file
    if req.selection is not None:
        s_sec, e_sec = req.selection.start_sec, req.selection.end_sec
        lo_hz, hi_hz = req.selection.low_freq_hz, req.selection.high_freq_hz
        region_kind = "active selection"
    elif req.view_range is not None:
        s_sec, e_sec = req.view_range.start_sec, req.view_range.end_sec
        lo_hz, hi_hz = req.view_range.min_freq_hz, req.view_range.max_freq_hz
        region_kind = "visible viewport"
    else:
        s_sec, e_sec = 0.0, info.duration_sec
        lo_hz, hi_hz = 0.0, nyquist
        region_kind = "full recording"

    fft_size = req.spec_params.fft_size if req.spec_params else 512
    hop_size = req.spec_params.hop_size if req.spec_params else 128

    measurements = measure_acoustic_selection(
        loaded.samples[ch],
        sample_rate=info.sample_rate,
        start_sec=s_sec,
        end_sec=e_sec,
        low_freq_hz=lo_hz,
        high_freq_hz=hi_hz,
        fft_size=fft_size,
        hop_size=hop_size,
    )
    fallback_detect = _heuristic_detect_params(measurements, nyquist)

    api_key = (req.api_key or os.environ.get("GEMINI_API_KEY") or "").strip()
    if not api_key:
        summary_md = (
            f"### Offline Acoustic Analysis ({region_kind})\n"
            f"- **File**: `{info.name}` ({info.sample_rate:,} Hz, {info.duration_sec:.2f}s)\n"
            f"- **Peak Frequency**: **{measurements.peak_freq_hz:.1f} Hz** "
            f"(Centroid: {measurements.centroid_freq_hz:.1f} Hz)\n"
            f"- **90% Energy Band**: {measurements.q5_freq_hz:.1f} – {measurements.q95_freq_hz:.1f} Hz "
            f"(BW90: {measurements.bw90_hz:.1f} Hz)\n"
            f"- **Duration**: {measurements.duration_sec:.3f} s "
            f"(Peak at {measurements.time_of_peak_sec:.3f} s)\n"
            f"- **Amplitude / SNR**: RMS {measurements.rms_db:.1f} dBFS, "
            f"Peak {measurements.peak_db:.1f} dBFS, Est. SNR **{measurements.snr_db:.1f} dB**\n\n"
            "> *Add a Gemini API key (in the AI Explorer settings or `GEMINI_API_KEY` env var) "
            "to enable multimodal spectrogram interpretation, taxonomic hypotheses, and interactive "
            "Q&A. You can still apply the DSP-derived Detect Sounds parameters below.*"
        )
        return AiAnalyzeResponse(
            answer=summary_md,
            model_used="offline-dsp",
            measurements=measurements,
            suggested_detect_params=fallback_detect,
            suggested_labels=[],
        )

    # Render spectrogram PNG of the focus window for multimodal visual inspection
    png_b64: str | None = None
    if req.include_spectrogram_image:
        spec = compute_spectrogram(
            loaded.samples[ch],
            sample_rate=info.sample_rate,
            fft_size=fft_size,
            hop_size=hop_size,
            window_type=req.spec_params.window_type if req.spec_params else "hann",
            dsp_mode=req.spec_params.dsp_mode if req.spec_params else "modern_stft",
            pop_filter=req.spec_params.pop_filter if req.spec_params else False,
            start_sec=max(0.0, s_sec - min(0.5, measurements.duration_sec * 0.25)),
            end_sec=min(info.duration_sec, e_sec + min(0.5, measurements.duration_sec * 0.25)),
            min_freq_hz=max(0.0, lo_hz * 0.85),
            max_freq_hz=min(nyquist, hi_hz * 1.15),
        )
        png_bytes = render_spectrogram_png(
            spec,
            colormap=req.display_settings.colormap if req.display_settings else "viridis",
            gain_db=req.display_settings.gain_db if req.display_settings else 0.0,
            threshold_db=req.display_settings.threshold_db if req.display_settings else -80.0,
            dynamic_range_db=(
                req.display_settings.dynamic_range_db if req.display_settings else 75.0
            ),
        )
        png_b64 = base64.b64encode(png_bytes).decode("ascii")

    ann_summary = ""
    if req.annotations:
        lines = [
            f"- [{a.start_time_sec:.2f}s–{a.end_time_sec:.2f}s, "
            f"{a.low_freq_hz:.0f}–{a.high_freq_hz:.0f} Hz] {a.comment or '(unlabeled)'}"
            for a in req.annotations[:25]
        ]
        ann_summary = "Existing annotations in session:\n" + "\n".join(lines)

    context_block = (
        "You are an expert bioacoustics & passive acoustic monitoring (PAM) research assistant "
        "embedded inside SyrinxOne. Ground every claim in the exact DSP measurements and "
        "spectrogram image provided; be scientifically candid about uncertainty (never claim "
        "100% species ID when multiple taxa overlap).\n\n"
        f"Recording metadata:\n"
        f"- File: {info.name} (source={info.source_id}, uri={info.uri})\n"
        f"- Sample rate: {info.sample_rate} Hz (Nyquist {nyquist:.0f} Hz), "
        f"Channels: {info.num_channels}, Loaded duration: {info.duration_sec:.2f}s"
        + (
            f" (window starting at {info.window_start_sec:.1f}s of {info.total_duration_sec:.1f}s)"
            if info.is_partial_window and info.total_duration_sec
            else ""
        )
        + f"\n\nMeasured {region_kind} ({s_sec:.3f}s–{e_sec:.3f}s, "
        f"{lo_hz:.0f}–{hi_hz:.0f} Hz):\n"
        f"- Duration: {measurements.duration_sec:.3f}s (time of peak: {measurements.time_of_peak_sec:.3f}s)\n"
        f"- Peak freq: {measurements.peak_freq_hz:.1f} Hz, Centroid: {measurements.centroid_freq_hz:.1f} Hz\n"
        f"- Quantiles Q5/Q50/Q95: {measurements.q5_freq_hz:.1f} / {measurements.q50_freq_hz:.1f} / "
        f"{measurements.q95_freq_hz:.1f} Hz (BW90: {measurements.bw90_hz:.1f} Hz)\n"
        f"- RMS: {measurements.rms_db:.1f} dBFS, Peak: {measurements.peak_db:.1f} dBFS, "
        f"Noise floor: {measurements.noise_floor_db:.1f} dB, SNR: {measurements.snr_db:.1f} dB\n"
        + (f"\n{ann_summary}\n" if ann_summary else "")
        + "\nWhen helpful (especially if asked to classify, label, or detect similar sounds), "
        "append ONE fenced JSON block at the very end using this exact fence tag:\n"
        "```syrinx_json\n"
        '{"suggestedLabels": ["Label1", "Label2"], "suggestedDetectParams": '
        '{"minFreqHz": 1000, "maxFreqHz": 5000, "triggerThresholdDb": 12, '
        '"minSoundDurSec": 0.08, "maxSilenceGapSec": 0.25, "comment": "Label1"}}\n'
        "```"
    )

    contents: list[dict[str, object]] = []
    for msg in req.history[-8:]:
        role = "model" if msg.role == "assistant" else "user"
        contents.append({"role": role, "parts": [{"text": msg.content}]})

    user_parts: list[dict[str, object]] = [{"text": f"{context_block}\n\nUser question: {req.prompt}"}]
    if png_b64 is not None:
        user_parts.append({"inline_data": {"mime_type": "image/png", "data": png_b64}})
    contents.append({"role": "user", "parts": user_parts})

    model = (req.model or "gemini-2.5-flash").strip()
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            url,
            headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
            json={"contents": contents},
        )
        if resp.status_code != 200:
            err_text = resp.text[:300]
            raise RuntimeError(f"Gemini API error (HTTP {resp.status_code}): {err_text}")
        data = resp.json()

    text_chunks: list[str] = []
    for cand in data.get("candidates", []):
        for part in cand.get("content", {}).get("parts", []):
            if part.get("text"):
                text_chunks.append(str(part["text"]))

    raw_answer = "\n".join(text_chunks).strip()
    if not raw_answer:
        raise RuntimeError("Gemini API returned an empty response")

    clean_answer, parsed_det, labels = _extract_structured_json(raw_answer)
    return AiAnalyzeResponse(
        answer=clean_answer,
        model_used=model,
        measurements=measurements,
        suggested_detect_params=parsed_det or fallback_detect,
        suggested_labels=labels,
    )
