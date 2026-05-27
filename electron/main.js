const { app, BrowserWindow, shell, dialog } = require('electron');
const { spawn } = require('child_process');
const path = require('path');
const http = require('http');
const fs = require('fs');

const PORT = 5050;
let mainWindow = null;
let serverProcess = null;

// ─── Locate bundled server executable ───────────────────────────────────────
function getServerPath() {
  // In packaged app, extraResources lands in process.resourcesPath/server/
  const candidates = [
    path.join(process.resourcesPath, 'server', 'server.exe'),
    path.join(__dirname, '..', 'server_dist', 'server.exe'),
    path.join(__dirname, '..', 'server_dist', 'server'),   // Linux/Mac dev
  ];
  for (const p of candidates) {
    if (fs.existsSync(p)) return p;
  }
  return null;
}

// ─── Data directory (writable, next to exe or in AppData) ───────────────────
function getDataDir() {
  return path.join(app.getPath('userData'), 'data');
}

// ─── Start Flask server ──────────────────────────────────────────────────────
function startServer() {
  const serverExe = getServerPath();
  if (!serverExe) {
    dialog.showErrorBox('Server not found', 'Could not find server executable.\nPlease reinstall the application.');
    app.quit();
    return;
  }

  const dataDir = getDataDir();
  fs.mkdirSync(dataDir, { recursive: true });

  const env = { ...process.env, OLLAMA_CHAT_DATA: dataDir, FLASK_PORT: String(PORT) };

  serverProcess = spawn(serverExe, [], {
    env,
    windowsHide: true,
    detached: false,
  });

  serverProcess.on('error', (err) => {
    dialog.showErrorBox('Server error', `Failed to start server: ${err.message}`);
    app.quit();
  });

  serverProcess.on('exit', (code) => {
    if (code !== 0 && mainWindow) {
      dialog.showErrorBox('Server stopped', `The backend exited unexpectedly (code ${code}).\nThe app will close.`);
      app.quit();
    }
  });
}

// ─── Wait for server to be ready ────────────────────────────────────────────
function waitForServer(retries = 40, delay = 300) {
  return new Promise((resolve, reject) => {
    const attempt = () => {
      http.get(`http://127.0.0.1:${PORT}/`, (res) => {
        if (res.statusCode < 500) resolve();
        else retry();
      }).on('error', retry);
    };
    const retry = () => {
      if (--retries <= 0) return reject(new Error('Server did not start in time'));
      setTimeout(attempt, delay);
    };
    attempt();
  });
}

// ─── Create main window ──────────────────────────────────────────────────────
function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1400,
    height: 860,
    minWidth: 900,
    minHeight: 600,
    title: 'Ollama Chat',
    icon: path.join(__dirname, '..', 'assets', 'icon.ico'),
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
    backgroundColor: '#1a1a2e',
    show: false,
  });

  mainWindow.loadURL(`http://127.0.0.1:${PORT}/`);
  mainWindow.once('ready-to-show', () => mainWindow.show());

  // Open external links in system browser
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: 'deny' };
  });

  mainWindow.on('closed', () => { mainWindow = null; });
}

// ─── App lifecycle ───────────────────────────────────────────────────────────
app.whenReady().then(async () => {
  startServer();
  try {
    await waitForServer();
    createWindow();
  } catch (err) {
    dialog.showErrorBox('Startup failed', err.message);
    app.quit();
  }
});

app.on('window-all-closed', () => {
  if (serverProcess) {
    serverProcess.kill();
    serverProcess = null;
  }
  app.quit();
});

app.on('before-quit', () => {
  if (serverProcess) {
    serverProcess.kill();
    serverProcess = null;
  }
});
