import React, { useEffect, useRef } from 'react';
import { useWorkspaceStore } from '../../store/workspaceStore';
import { MDIChildWindow } from './MDIChildWindow';

export const MDIWorkspace: React.FC = () => {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const windows = useWorkspaceStore((s) => s.windows);
  const files = useWorkspaceStore((s) => s.files);
  const activeWindowId = useWorkspaceStore((s) => s.activeWindowId);
  const setCanvasBounds = useWorkspaceStore((s) => s.setCanvasBounds);
  const focusWindow = useWorkspaceStore((s) => s.focusWindow);
  const openUploadedFile = useWorkspaceStore((s) => s.openUploadedFile);
  const importAnnotationsFromText = useWorkspaceStore((s) => s.importAnnotationsFromText);
  const setModalState = useWorkspaceStore((s) => s.setModalState);

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const update = () => {
      setCanvasBounds({ width: el.clientWidth, height: el.clientHeight });
    };
    update();
    const ro = new ResizeObserver(update);
    ro.observe(el);
    return () => ro.disconnect();
  }, [setCanvasBounds]);

  const handleDrop = async (e: React.DragEvent) => {
    e.preventDefault();
    const dropped = Array.from(e.dataTransfer.files);
    for (const file of dropped) {
      const lower = file.name.toLowerCase();
      if (lower.endsWith('.txt') || lower.endsWith('.log') || lower.endsWith('.csv') || lower.endsWith('.json')) {
        const text = await file.text();
        const fmt = lower.endsWith('.json')
          ? 'json'
          : lower.endsWith('.csv')
            ? 'csv'
            : text.includes('Begin Time (s)')
              ? 'raven_txt'
              : 'syrinx_txt';
        await importAnnotationsFromText(text, fmt);
      } else {
        await openUploadedFile(file);
      }
    }
  };

  const minimizedWindows = windows.filter((w) => w.minimized);

  return (
    <div
      ref={containerRef}
      className="mdi-workspace"
      onDragOver={(e) => e.preventDefault()}
      onDrop={(e) => void handleDrop(e)}
    >
      {windows.length === 0 && (
        <div className="mdi-empty-state">
          <h2>SyrinxOne MDI Workspace</h2>
          <p>
            Open audio files (WAV, FLAC, AIFF, OGG, MP3) via <strong>Browse / Open</strong> or drag
            &amp; drop files directly onto this canvas.
          </p>
          <p className="mdi-empty-hint">
            Controls: Drag to draw selection box · Press configured hotkeys to stamp annotations ·
            Wheel to zoom time · Alt+Wheel to zoom frequency · Shift+Drag to pan · Double-click to
            reset view
          </p>
          <button className="btn-primary" onClick={() => setModalState('showFileBrowser', true)}>
            Browse Local / Cloud Sources
          </button>
        </div>
      )}

      {windows.map((win) => {
        const file = files[win.fileId];
        if (!file) return null;
        return (
          <MDIChildWindow
            key={win.windowId}
            win={win}
            file={file}
            isActive={win.windowId === activeWindowId}
          />
        );
      })}

      {minimizedWindows.length > 0 && (
        <div className="mdi-minimized-tray">
          {minimizedWindows.map((w) => (
            <button
              key={w.windowId}
              className="mdi-minimized-pill"
              onClick={() => focusWindow(w.windowId)}
            >
              {w.title}
            </button>
          ))}
        </div>
      )}
    </div>
  );
};
