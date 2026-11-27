import { describe, expect, it } from 'vitest';
import {
  SPECTROGRAM_BASE_FLOOR_DB,
  SPECTROGRAM_SPAN_DB,
  type SpectrogramMatrix,
} from '@syrinxone/shared';
import { computeAutoGain } from './spectrogramFetch';

function makeSyntheticSpec(
  noiseDb: number,
  noiseJitterDb: number,
  signalDb: number,
  signalFraction = 0.04,
  dcSpikeDb?: number,
): SpectrogramMatrix {
  const numTimeBins = 120;
  const numFreqBins = 64;
  const data = new Float32Array(numTimeBins * numFreqBins);
  const sigRows = Math.max(1, Math.round(numTimeBins * signalFraction));

  for (let t = 0; t < numTimeBins; t++) {
    for (let f = 0; f < numFreqBins; f++) {
      const wave = Math.sin(t * 1.7 + f * 2.3) * 0.6 + Math.cos(t * 0.9 - f * 1.1) * 0.4;
      let v = noiseDb + wave * noiseJitterDb;
      if (dcSpikeDb !== undefined && f === 0) {
        v = dcSpikeDb;
      } else if (t < sigRows && f >= 18 && f <= 30) {
        v = signalDb;
      }
      data[t * numFreqBins + f] = v;
    }
  }

  return {
    fileId: 'test',
    channel: 0,
    numTimeBins,
    numFreqBins,
    timeStepSec: 0.01,
    freqStepHz: 125,
    startSec: 0,
    endSec: 1.2,
    minFreqHz: 0,
    maxFreqHz: 8000,
    minValue: noiseDb - noiseJitterDb,
    maxValue: Math.max(signalDb, dcSpikeDb ?? signalDb),
    dspMode: 'modern_stft',
    data,
  };
}

function mapLevel(valDb: number, gainDb: number, floor: number): number {
  const level = Math.max(
    0,
    Math.min(1, (valDb + gainDb - SPECTROGRAM_BASE_FLOOR_DB) / SPECTROGRAM_SPAN_DB),
  );
  const floorNorm = floor / 100;
  return floorNorm > 0 && level < floorNorm ? 0 : level;
}

describe('computeAutoGain', () => {
  it('anchors gain to background noise on moderate-to-high SNR recordings with low Floor (~2%)', () => {
    // Matches real recording profile: ambient noise around -64 dB (ceiling ~ -60 dB), target peak -28 dB
    const mat = makeSyntheticSpec(-64, 2.5, -28, 0.05);
    const res = computeAutoGain(mat, { gainDb: 0, floor: 0 });
    expect(res.gainDb).toBeGreaterThanOrEqual(-22);
    expect(res.gainDb).toBeLessThanOrEqual(-15);
    expect(res.floor).toBeLessThanOrEqual(5);
    expect(mapLevel(-28, res.gainDb, res.floor)).toBeGreaterThan(0.5);
    expect(mapLevel(-64, res.gainDb, res.floor)).toBe(0);
  });

  it('boosts faint low-SNR whale signal to clear visibility while snapping noise to 0', () => {
    const mat = makeSyntheticSpec(-102, 3, -78, 0.06);
    const res = computeAutoGain(mat, { gainDb: 0, floor: 0 });
    expect(res.gainDb).toBeGreaterThan(25);
    expect(mapLevel(-78, res.gainDb, res.floor)).toBeGreaterThan(0.5);
    expect(mapLevel(-102, res.gainDb, res.floor)).toBe(0);
  });

  it('attenuates hot recordings and ignores DC rumble spikes', () => {
    const mat = makeSyntheticSpec(-62, 2.5, -12, 0.05, 5);
    const res = computeAutoGain(mat, { gainDb: 0, floor: 0 });
    expect(res.gainDb).toBeLessThan(-10);
    expect(res.floor).toBeLessThanOrEqual(5);
    expect(mapLevel(-12, res.gainDb, res.floor)).toBeGreaterThan(0.7);
    expect(mapLevel(-62, res.gainDb, res.floor)).toBe(0);
  });
});
