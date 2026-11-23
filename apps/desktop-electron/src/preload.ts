import { contextBridge, ipcRenderer } from 'electron';

contextBridge.exposeInMainWorld('syrinxDesktop', {
  getBackendConfig: () => ipcRenderer.invoke('syrinx:get-backend-config'),
  openAudioFilesDialog: () => ipcRenderer.invoke('syrinx:open-audio-files'),
  openAnnotationFileDialog: () => ipcRenderer.invoke('syrinx:open-annotation-file'),
  saveFileDialog: (options: {
    defaultPath?: string;
    filters?: Array<{ name: string; extensions: string[] }>;
  }) => ipcRenderer.invoke('syrinx:save-file', options),
  onMenuCommand: (handler: (command: string) => void) => {
    const listener = (_event: Electron.IpcRendererEvent, cmd: string) => handler(cmd);
    ipcRenderer.on('syrinx:menu-command', listener);
    return () => {
      ipcRenderer.removeListener('syrinx:menu-command', listener);
    };
  },
});
