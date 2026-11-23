import path from 'node:path';
import { app, BrowserWindow, dialog, ipcMain, Menu } from 'electron';
import { startPythonSidecar, type SidecarHandle } from './pythonSidecar';

let mainWindow: BrowserWindow | null = null;
let sidecar: SidecarHandle | null = null;

function resolveRepoRoot(): string {
  // dist/main.js -> apps/desktop-electron -> repo root
  return path.resolve(__dirname, '..', '..', '..');
}

function sendMenuCommand(cmd: string): void {
  mainWindow?.webContents.send('syrinx:menu-command', cmd);
}

function buildAppMenu(): void {
  const template: Electron.MenuItemConstructorOptions[] = [
    {
      label: 'File',
      submenu: [
        {
          label: 'Open Audio Files...',
          accelerator: 'CmdOrCtrl+O',
          click: () => sendMenuCommand('open-audio'),
        },
        {
          label: 'Open Annotation Log...',
          accelerator: 'CmdOrCtrl+Shift+O',
          click: () => sendMenuCommand('open-annotations'),
        },
        { type: 'separator' },
        {
          label: 'Save Entire Display...',
          click: () => sendMenuCommand('save-display'),
        },
        {
          label: 'Load Display...',
          click: () => sendMenuCommand('load-display'),
        },
        {
          label: 'Merge Display...',
          click: () => sendMenuCommand('merge-display'),
        },
        { type: 'separator' },
        {
          label: 'Previous File in Folder',
          accelerator: 'Alt+Left',
          click: () => sendMenuCommand('prev-file'),
        },
        {
          label: 'Next File in Folder',
          accelerator: 'Alt+Right',
          click: () => sendMenuCommand('next-file'),
        },
        { type: 'separator' },
        {
          label: 'Copy Spectrogram Image (PNG)',
          click: () => sendMenuCommand('copy-image'),
        },
        {
          label: 'Clip & Save Selection...',
          accelerator: 'CmdOrCtrl+K',
          click: () => sendMenuCommand('clip_save'),
        },
        { type: 'separator' },
        { role: 'quit' },
      ],
    },
    {
      label: 'Window',
      submenu: [
        { label: 'Tile Grid', click: () => sendMenuCommand('tile_grid') },
        { label: 'Tile Horizontal', click: () => sendMenuCommand('tile_horizontal') },
        { label: 'Tile Vertical', click: () => sendMenuCommand('tile_vertical') },
        { label: 'Cascade', click: () => sendMenuCommand('cascade') },
        { type: 'separator' },
        { label: 'Resize All to Active Window', click: () => sendMenuCommand('resize_all_to_active') },
        { label: 'Toggle Child Titlebars', click: () => sendMenuCommand('toggle_titlebars') },
        {
          label: 'Toggle Fixed / Stretch Time Scale',
          accelerator: 'Alt+T',
          click: () => sendMenuCommand('toggle-timescale'),
        },
      ],
    },
    {
      label: 'Tools',
      submenu: [
        { label: 'Detect Sounds in Recording...', click: () => sendMenuCommand('detect-sounds') },
        { label: 'Configure Annotation Hotkeys...', click: () => sendMenuCommand('hotkeys') },
        { type: 'separator' },
        { role: 'toggleDevTools' },
        { role: 'reload' },
      ],
    },
  ];

  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

async function createMainWindow(): Promise<void> {
  const repoRoot = resolveRepoRoot();
  sidecar = await startPythonSidecar(repoRoot);

  mainWindow = new BrowserWindow({
    width: 1440,
    height: 900,
    minWidth: 960,
    minHeight: 620,
    title: 'SyrinxOne',
    backgroundColor: '#0b0f14',
    show: true,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  mainWindow.once('ready-to-show', () => {
    mainWindow?.show();
    mainWindow?.focus();
    mainWindow?.moveTop();
  });

  const devUrl = process.env.SYRINX_DEV_URL;
  if (devUrl) {
    await mainWindow.loadURL(devUrl);
  } else {
    const webIndex = path.join(repoRoot, 'apps', 'web', 'dist', 'index.html');
    await mainWindow.loadFile(webIndex);
  }

  mainWindow.on('closed', () => {
    mainWindow = null;
  });
}

ipcMain.handle('syrinx:get-backend-config', () => {
  if (!sidecar) throw new Error('Python sidecar is not running');
  return { baseUrl: sidecar.baseUrl, token: sidecar.token };
});

ipcMain.handle('syrinx:open-audio-files', async () => {
  if (!mainWindow) return [];
  const res = await dialog.showOpenDialog(mainWindow, {
    title: 'Open Bioacoustic Audio Files',
    properties: ['openFile', 'multiSelections'],
    filters: [
      { name: 'Audio Files', extensions: ['wav', 'flac', 'aif', 'aiff', 'ogg', 'mp3'] },
      { name: 'All Files', extensions: ['*'] },
    ],
  });
  return res.canceled ? [] : res.filePaths;
});

ipcMain.handle('syrinx:open-annotation-file', async () => {
  if (!mainWindow) return null;
  const res = await dialog.showOpenDialog(mainWindow, {
    title: 'Open Annotation Log',
    properties: ['openFile'],
    filters: [
      { name: 'Annotation Logs', extensions: ['txt', 'log', 'csv', 'json', 'tsv'] },
      { name: 'All Files', extensions: ['*'] },
    ],
  });
  return res.canceled ? null : (res.filePaths[0] ?? null);
});

ipcMain.handle(
  'syrinx:save-file',
  async (
    _event,
    options: { defaultPath?: string; filters?: Array<{ name: string; extensions: string[] }> },
  ) => {
    if (!mainWindow) return null;
    const res = await dialog.showSaveDialog(mainWindow, {
      defaultPath: options.defaultPath,
      filters: options.filters,
    });
    return res.canceled ? null : (res.filePath ?? null);
  },
);

app.whenReady().then(async () => {
  buildAppMenu();
  try {
    await createMainWindow();
  } catch (err) {
    dialog.showErrorBox(
      'SyrinxOne Startup Error',
      err instanceof Error ? err.message : String(err),
    );
    app.quit();
  }
});

app.on('window-all-closed', () => {
  sidecar?.stop();
  if (process.platform !== 'darwin') app.quit();
});

app.on('before-quit', () => {
  sidecar?.stop();
});
