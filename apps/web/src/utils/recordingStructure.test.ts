import { describe, expect, it, beforeEach } from 'vitest';
import {
  analyzeRecordingStructure,
  buildDefinedRecordings,
  clearSavedRecordingDefinition,
  getSavedRecordingDefinition,
  makeStructureSourceKey,
  saveRecordingDefinition,
  type ScannedAudioFile,
} from './recordingStructure';

function makeChime3SampleFiles(): ScannedAudioFile[] {
  const files: ScannedAudioFile[] = [];
  const sessions = [
    { date: '150203', seq: '010', env: 'CAF' },
    { date: '150203', seq: '020', env: 'CAF' },
    { date: '150204', seq: '010', env: 'BUS' },
  ];
  for (const s of sessions) {
    for (let ch = 1; ch <= 6; ch++) {
      const name = `BGD_${s.date}_${s.seq}_${s.env}.CH${ch}.wav`;
      files.push({
        sourceId: 'hf_chime3_6ch',
        uri: `CHiME3/data/audio/16kHz/backgrounds/${name}`,
        name,
        dirParts: [],
        sizeBytes: 67_114_028,
      });
    }
  }
  return files;
}

function makeNestedPamSampleFiles(): ScannedAudioFile[] {
  const entries = [
    { dir: ['2022', '04'], name: 'MARS_20220401_000000.wav' },
    { dir: ['2022', '04'], name: 'MARS_20220401_060000.wav' },
    { dir: ['2022', '04'], name: 'MARS_20220402_000000.wav' },
    { dir: ['2022', '05'], name: 'MARS_20220501_000000.wav' },
    { dir: ['2023', '01'], name: 'MARS_20230101_000000.wav' },
  ];
  return entries.map((e) => ({
    sourceId: 'mbari_16khz',
    uri: `${e.dir.join('/')}/${e.name}`,
    name: e.name,
    dirParts: e.dir,
    sizeBytes: 120_000_000,
  }));
}

describe('recordingStructure', () => {
  beforeEach(() => {
    const store = new Map<string, string>();
    Object.defineProperty(globalThis, 'localStorage', {
      value: {
        getItem: (k: string) => store.get(k) ?? null,
        setItem: (k: string, v: string) => {
          store.set(k, v);
        },
        removeItem: (k: string) => {
          store.delete(k);
        },
        clear: () => store.clear(),
      },
      configurable: true,
    });
  });

  it('parses CHiME-3 filenames + README docs and recommends 6-channel session grouping with multi-granularity options', () => {
    const files = makeChime3SampleFiles();
    const readme = `
      CHiME-3 6-channel tablet microphone array background recordings.
      Environments:
      CAF - cafe
      BUS - bus
      PED - pedestrian area
      STR - street junction
    `;
    const analysis = analyzeRecordingStructure(files, {
      docText: readme,
      sourceLabel: 'CHiME-3 6-Mic Array',
    });

    expect(analysis.totalAudioFiles).toBe(18);
    expect(analysis.detectedChannelFieldId).not.toBeNull();
    expect(analysis.hasDates).toBe(true);
    expect(analysis.docInsights.some((s) => s.includes('6-channel'))).toBe(true);
    expect(analysis.docInsights.some((s) => s.includes('CAF=cafe'))).toBe(true);

    // Recommended suggestion should stack CH1..CH6 into 3 recordings (1 per session)
    const recSugg = analysis.suggestions.find((s) => s.recommended);
    expect(recSugg).toBeDefined();
    expect(recSugg?.granularity).toBe('channel_group');
    expect(recSugg?.recordingCount).toBe(3);

    const sessionRecordings = buildDefinedRecordings(files, recSugg!.definition);
    expect(sessionRecordings).toHaveLength(3);
    expect(sessionRecordings[0].channelCount).toBe(6);
    expect(sessionRecordings[0].segmentCount).toBe(1);
    expect(sessionRecordings[0].fileCount).toBe(6);
    expect(sessionRecordings[0].specs[0].channelFiles).toHaveLength(6);
    expect(sessionRecordings[0].specs[0].channelFiles?.[0].name).toContain('.CH1.wav');
    expect(sessionRecordings[0].specs[0].channelFiles?.[5].name).toContain('.CH6.wav');

    // Day granularity should combine sessions 010 & 020 on 2015-02-03 CAF into 1 6-channel 2-segment recording
    const byDay = analysis.suggestions.find((s) => s.granularity === 'day');
    expect(byDay).toBeDefined();
    const dayRecordings = buildDefinedRecordings(files, byDay!.definition);
    expect(dayRecordings).toHaveLength(2);
    const feb3 = dayRecordings.find((r) => r.title.includes('2015-02-03'));
    expect(feb3?.channelCount).toBe(6);
    expect(feb3?.segmentCount).toBe(2);
    expect(feb3?.fileCount).toBe(12);

    // Year & Individual granularities also available
    expect(analysis.suggestions.some((s) => s.granularity === 'year')).toBe(true);
    expect(analysis.suggestions.some((s) => s.granularity === 'individual')).toBe(true);
  });

  it('offers Hour, Day, Month, Year, and Individual granularities for nested PAM archives', () => {
    const files = makeNestedPamSampleFiles();
    const analysis = analyzeRecordingStructure(files, { sourceLabel: 'MBARI 16 kHz' });

    expect(analysis.detectedChannelFieldId).toBeNull();
    expect(analysis.hasDates).toBe(true);
    expect(analysis.hasTimes).toBe(true);

    const byYear = analysis.suggestions.find((s) => s.granularity === 'year');
    const byMonth = analysis.suggestions.find((s) => s.granularity === 'month');
    const byDay = analysis.suggestions.find((s) => s.granularity === 'day');
    const individual = analysis.suggestions.find((s) => s.granularity === 'individual');

    expect(byYear?.recordingCount).toBe(2); // 2022, 2023
    expect(byMonth?.recordingCount).toBe(3); // 2022-04, 2022-05, 2023-01
    expect(byDay?.recordingCount).toBe(4); // Apr 1 (2 files), Apr 2, May 1, Jan 1
    expect(individual?.recordingCount).toBe(5);
  });

  it('persists and clears per-source recording definitions in localStorage', () => {
    const key = makeStructureSourceKey('hf_chime3_6ch');
    expect(getSavedRecordingDefinition(key)).toBeNull();

    const files = makeChime3SampleFiles();
    const analysis = analyzeRecordingStructure(files);
    const def = analysis.suggestions[0].definition;
    saveRecordingDefinition(key, def);

    const loaded = getSavedRecordingDefinition(key);
    expect(loaded).not.toBeNull();
    expect(loaded?.id).toBe(def.id);
    expect(loaded?.channelFieldId).toBe(def.channelFieldId);

    clearSavedRecordingDefinition(key);
    expect(getSavedRecordingDefinition(key)).toBeNull();
  });
});
