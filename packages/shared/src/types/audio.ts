export type WindowType = 'blackman' | 'hamming' | 'hann' | 'rect';
export type DspMode = 'modern_stft' | 'classic_syrinx';
export type RenderMode = 'webgl_float32' | 'server_bitmap';
export type TimeScaleMode = 'stretch' | 'fixed';
export type ColormapName =
  | 'grayscale_bow'
  | 'grayscale_wob'
  | 'viridis'
  | 'magma'
  | 'inferno'
  | 'turbo';

export interface DataSourceInfo {
  sourceId: string;
  displayName: string;
  kind: 'local_fs' | 's3' | 'gcs' | 'http' | 'custom';
  readOnly: boolean;
  rootPath?: string;
}

export interface BrowseEntry {
  name: string;
  uri: string;
  isDir: boolean;
  sizeBytes?: number;
  kind?: 'audio' | 'annotation' | 'other';
}

export interface AudioFileInfo {
  fileId: string;
  sourceId: string;
  uri: string;
  name: string;
  sampleRate: number;
  numChannels: number;
  numFrames: number;
  durationSec: number;
  format: string;
  subtype: string;
  startDateTimeIso?: string | null;
}

export interface SpectrogramParams {
  channel: number; // 0-based internal channel index
  fftSize: number;
  hopSize: number;
  windowType: WindowType;
  dspMode: DspMode;
  popFilter: boolean;
  classicGain: number;
  classicWhiteThresh: number;
}

export interface DisplaySettings {
  colormap: ColormapName;
  gainDb: number;
  thresholdDb: number;
  dynamicRangeDb: number;
  renderMode: RenderMode;
  timeScaleMode: TimeScaleMode;
  /** Milliseconds per horizontal pixel when timeScaleMode === 'fixed' */
  msPerPixel: number;
  showWaveform: boolean;
  showAxes: boolean;
  showGrid: boolean;
  showAnnotations: boolean;
  showCursors: boolean;
  lockSettings: boolean;
  interpolation: 'nearest' | 'linear';
}

export interface SpectrogramMatrix {
  fileId: string;
  channel: number;
  numTimeBins: number;
  numFreqBins: number;
  timeStepSec: number;
  freqStepHz: number;
  startSec: number;
  endSec: number;
  minFreqHz: number;
  maxFreqHz: number;
  minValue: number;
  maxValue: number;
  dspMode: DspMode;
  /** Row-major [timeIndex * numFreqBins + freqIndex] Float32 values (dB or classic scaled) */
  data: Float32Array;
}

export interface WaveformOverview {
  fileId: string;
  channel: number;
  numBins: number;
  startSec: number;
  endSec: number;
  minValues: number[];
  maxValues: number[];
}

export interface ClipSaveRequest {
  fileId: string;
  startSec: number;
  endSec: number;
  channels?: number[];
  targetSourceId: string;
  targetDirectory: string;
  nameTemplate: string;
  comment?: string;
  openInWindow?: boolean;
}

export interface ClipSaveResponse {
  savedUri: string;
  savedName: string;
  fileInfo?: AudioFileInfo;
}

export interface DetectSoundsRequest {
  channel?: number;
  fftSize?: number;
  hopSize?: number;
  minFreqHz: number;
  maxFreqHz: number;
  triggerThresholdDb: number;
  minTriggerDurSec: number;
  minSoundDurSec: number;
  maxSilenceGapSec: number;
  preBufferSec: number;
  postBufferSec: number;
  popFilter: boolean;
  startSec?: number;
  endSec?: number;
  comment: string;
  saveClips?: boolean;
  targetSourceId?: string;
  targetDirectory?: string;
  clipNameTemplate?: string;
}

export interface DetectSoundsResponse {
  detections: import('./annotations.js').AnnotationRecord[];
  noiseFloorDb: number;
  savedClips: string[];
}

export const DEFAULT_SPECTROGRAM_PARAMS: SpectrogramParams = {
  channel: 0,
  fftSize: 512,
  hopSize: 128,
  windowType: 'hann',
  dspMode: 'modern_stft',
  popFilter: false,
  classicGain: 1,
  classicWhiteThresh: 0,
};

export const DEFAULT_DISPLAY_SETTINGS: DisplaySettings = {
  colormap: 'grayscale_bow',
  gainDb: 0,
  thresholdDb: -80,
  dynamicRangeDb: 75,
  renderMode: 'webgl_float32',
  timeScaleMode: 'stretch',
  msPerPixel: 4.0,
  showWaveform: true,
  showAxes: true,
  showGrid: false,
  showAnnotations: true,
  showCursors: true,
  lockSettings: false,
  interpolation: 'linear',
};
