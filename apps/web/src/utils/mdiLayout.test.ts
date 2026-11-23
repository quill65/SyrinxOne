import { describe, expect, it } from 'vitest';
import {
  DEFAULT_DISPLAY_SETTINGS,
  DEFAULT_SPECTROGRAM_PARAMS,
  type MDIWindowState,
} from '@syrinxone/shared';
import { applyMdiLayout } from './mdiLayout';
import { buildColormapRGBA } from '../components/spectrogram/colormaps';

function makeWindow(id: string, x = 0, y = 0, width = 400, height = 300): MDIWindowState {
  return {
    windowId: id,
    fileId: `file-${id}`,
    title: id,
    x,
    y,
    width,
    height,
    zIndex: 1,
    minimized: false,
    maximized: false,
    showTitlebar: true,
    linkGroupId: null,
    viewRange: { startSec: 0, endSec: 2, minFreqHz: 0, maxFreqHz: 11025 },
    specParams: { ...DEFAULT_SPECTROGRAM_PARAMS },
    displaySettings: { ...DEFAULT_DISPLAY_SETTINGS },
    selection: null,
  };
}

describe('MDI Layout Engine', () => {
  it('tiles 4 windows into a 2x2 non-overlapping grid', () => {
    const wins = ['a', 'b', 'c', 'd'].map((id) => makeWindow(id));
    const laid = applyMdiLayout(wins, 'tile_grid', { width: 1000, height: 800 }, 'a');
    expect(laid).toHaveLength(4);
    expect(laid[0].x).toBeLessThan(laid[1].x);
    expect(laid[0].y).toBe(laid[1].y);
    expect(laid[2].y).toBeGreaterThan(laid[0].y);
    expect(laid[2].x).toBe(laid[0].x);
  });

  it('cascades windows with diagonal offset and ascending zIndex', () => {
    const wins = ['a', 'b', 'c'].map((id) => makeWindow(id));
    const laid = applyMdiLayout(wins, 'cascade', { width: 1200, height: 800 }, 'a');
    expect(laid[1].x).toBeGreaterThan(laid[0].x);
    expect(laid[1].y).toBeGreaterThan(laid[0].y);
    expect(laid[2].zIndex).toBeGreaterThan(laid[1].zIndex);
  });

  it('resizes all windows to match active window dimensions', () => {
    const wins = [makeWindow('a', 0, 0, 520, 340), makeWindow('b', 10, 10, 300, 220)];
    const laid = applyMdiLayout(wins, 'resize_all_to_active', { width: 1200, height: 800 }, 'a');
    expect(laid[1].width).toBe(520);
    expect(laid[1].height).toBe(340);
  });

  it('builds 256-color RGBA LUTs for classic grayscale and scientific colormaps', () => {
    const bow = buildColormapRGBA('grayscale_bow');
    expect(bow.length).toBe(256 * 4);
    expect(bow[0]).toBe(255); // quiet = white in Black-on-White
    expect(bow[255 * 4]).toBe(0); // loud = black

    const wob = buildColormapRGBA('grayscale_wob');
    expect(wob[0]).toBe(0); // quiet = black in White-on-Black
    expect(wob[255 * 4]).toBe(255);
  });
});
