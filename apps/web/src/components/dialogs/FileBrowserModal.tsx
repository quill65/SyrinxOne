import React, { useEffect, useState } from 'react';
import { FileAudio, FileText, Folder, RefreshCw, Upload, X } from 'lucide-react';
import type { BrowseEntry, DataSourceInfo } from '@syrinxone/shared';
import { useWorkspaceStore } from '../../store/workspaceStore';

export const FileBrowserModal: React.FC = () => {
  const show = useWorkspaceStore((s) => s.showFileBrowser);
  const engine = useWorkspaceStore((s) => s.engine);
  const openAudioFromSource = useWorkspaceStore((s) => s.openAudioFromSource);
  const importAnnotationsFromSource = useWorkspaceStore((s) => s.importAnnotationsFromSource);
  const openUploadedFile = useWorkspaceStore((s) => s.openUploadedFile);
  const setModalState = useWorkspaceStore((s) => s.setModalState);

  const [sources, setSources] = useState<DataSourceInfo[]>([]);
  const [selectedSource, setSelectedSource] = useState<string>('local');
  const [prefix, setPrefix] = useState<string>('');
  const [entries, setEntries] = useState<BrowseEntry[]>([]);
  const [loading, setLoading] = useState(false);

  const refresh = async (srcId: string, pfx: string) => {
    setLoading(true);
    try {
      const list = await engine.browseSource(srcId, pfx);
      setEntries(list);
    } catch {
      setEntries([]);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (!show) return;
    void (async () => {
      try {
        const srcs = await engine.listSources();
        setSources(srcs);
        const first = srcs[0]?.sourceId || 'local';
        setSelectedSource(first);
        setPrefix(srcs[0]?.rootPath || '');
        await refresh(first, srcs[0]?.rootPath || '');
      } catch {
        setSources([]);
      }
    })();
  }, [show, engine]);

  if (!show) return null;

  const handleEntryClick = async (entry: BrowseEntry) => {
    if (entry.isDir) {
      setPrefix(entry.uri);
      await refresh(selectedSource, entry.uri);
    } else if (entry.kind === 'audio') {
      await openAudioFromSource(selectedSource, entry.uri);
    } else if (entry.kind === 'annotation') {
      await importAnnotationsFromSource(selectedSource, entry.uri);
    }
  };

  return (
    <div className="modal-backdrop" onClick={() => setModalState('showFileBrowser', false)}>
      <div className="modal-card modal-wide" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <span>Open Audio / Annotation File (Local &amp; Cloud Sources)</span>
          <button className="mdi-icon-btn" onClick={() => setModalState('showFileBrowser', false)}>
            <X size={14} />
          </button>
        </div>

        <div className="modal-body">
          <div className="browser-bar">
            <select
              value={selectedSource}
              onChange={(e) => {
                const next = e.target.value;
                const meta = sources.find((s) => s.sourceId === next);
                const nextPfx = meta?.rootPath || '';
                setSelectedSource(next);
                setPrefix(nextPfx);
                void refresh(next, nextPfx);
              }}
            >
              {sources.map((s) => (
                <option key={s.sourceId} value={s.sourceId}>
                  {s.displayName} ({s.kind})
                </option>
              ))}
            </select>

            <input
              type="text"
              value={prefix}
              placeholder="Folder path or cloud prefix..."
              onChange={(e) => setPrefix(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') void refresh(selectedSource, prefix);
              }}
            />

            <button className="btn-small" onClick={() => void refresh(selectedSource, prefix)}>
              <RefreshCw size={12} /> Go
            </button>

            <label className="btn-small btn-primary" style={{ cursor: 'pointer' }}>
              <Upload size={12} /> Upload File
              <input
                type="file"
                multiple
                accept=".wav,.flac,.aif,.aiff,.ogg,.mp3"
                style={{ display: 'none' }}
                onChange={(e) => {
                  const list = Array.from(e.target.files ?? []);
                  void Promise.all(list.map((f) => openUploadedFile(f)));
                  setModalState('showFileBrowser', false);
                }}
              />
            </label>
          </div>

          <div className="browser-list">
            {loading && <div className="panel-empty">Loading entries...</div>}
            {!loading &&
              entries.map((entry) => (
                <button
                  key={entry.uri}
                  className="browser-row"
                  onClick={() => void handleEntryClick(entry)}
                >
                  <span className="browser-icon">
                    {entry.isDir ? (
                      <Folder size={14} color="#60a5fa" />
                    ) : entry.kind === 'audio' ? (
                      <FileAudio size={14} color="#4ade80" />
                    ) : (
                      <FileText size={14} color="#facc15" />
                    )}
                  </span>
                  <span className="browser-name">{entry.name}</span>
                  <span className="browser-size mono">
                    {entry.sizeBytes != null ? `${(entry.sizeBytes / 1024).toFixed(1)} KB` : ''}
                  </span>
                </button>
              ))}
            {!loading && entries.length === 0 && (
              <div className="panel-empty">No files found in this location.</div>
            )}
          </div>
        </div>

        <div className="modal-footer">
          <button className="btn-small" onClick={() => setModalState('showFileBrowser', false)}>
            Done
          </button>
        </div>
      </div>
    </div>
  );
};
