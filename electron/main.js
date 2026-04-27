const { app, BrowserWindow, shell, dialog } = require('electron');
const { spawn } = require('child_process');
const path = require('path');
const http = require('http');
const os = require('os');

const PORT = 5757;
const FLASK_URL = `http://127.0.0.1:${PORT}`;

let flaskProcess = null;
let mainWindow = null;

// ─── Find Flask binary or fall back to python for dev ───
function getServerCommand() {
  if (app.isPackaged) {
    const ext = process.platform === 'win32' ? '.exe' : '';
    const bin = path.join(process.resourcesPath, 'server', 'server' + ext);
    return { cmd: bin, args: [], cwd: process.resourcesPath };
  }
  // Development: run python directly from project root
  const py = process.platform === 'win32' ? 'python' : 'python3';
  return { cmd: py, args: ['server.py'], cwd: path.join(__dirname, '..') };
}

// ─── User data dir for writable files ───
function getDataDir() {
  return app.getPath('userData');
}

// ─── Start Flask ───
function startFlask() {
  return new Promise((resolve, reject) => {
    const { cmd, args, cwd } = getServerCommand();
    const env = {
      ...process.env,
      PORT: String(PORT),
      OLLAMA_CHAT_DATA: getDataDir(),
    };

    flaskProcess = spawn(cmd, args, { cwd, env, stdio: ['ignore', 'pipe', 'pipe'] });

    flaskProcess.stdout.on('data', d => console.log('[Flask]', d.toString().trim()));
    flaskProcess.stderr.on('data', d => console.log('[Flask]', d.toString().trim()));

    flaskProcess.on('error', err => {
      dialog.showErrorBox('Eroare pornire server',
        `Nu s-a putut porni serverul Flask:\n${err.message}`);
      reject(err);
    });

    waitForFlask(resolve, reject);
  });
}

function waitForFlask(resolve, reject, attempts = 0) {
  if (attempts > 40) {
    reject(new Error('Serverul Flask nu a pornit în timp util.'));
    return;
  }
  http.get(FLASK_URL, () => resolve())
    .on('error', () => setTimeout(() => waitForFlask(resolve, reject, attempts + 1), 500));
}

// ─── Create window ───
function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1400,
    height: 900,
    minWidth: 900,
    minHeight: 600,
    title: 'Ollama Loop Chat',
    show: false,
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
    },
  });

  mainWindow.loadURL(FLASK_URL);

  mainWindow.once('ready-to-show', () => {
    mainWindow.show();
  });

  // Open external links in system browser, not in Electron window
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: 'deny' };
  });

  mainWindow.webContents.on('will-navigate', (event, url) => {
    if (!url.startsWith(FLASK_URL)) {
      event.preventDefault();
      shell.openExternal(url);
    }
  });
}

// ─── App lifecycle ───
app.whenReady().then(async () => {
  try {
    await startFlask();
    createWindow();
  } catch (err) {
    dialog.showErrorBox('Eroare', err.message);
    app.quit();
  }
});

app.on('window-all-closed', () => {
  stopFlask();
  if (process.platform !== 'darwin') app.quit();
});

app.on('activate', () => {
  if (BrowserWindow.getAllWindows().length === 0) createWindow();
});

app.on('before-quit', stopFlask);

function stopFlask() {
  if (flaskProcess) {
    flaskProcess.kill();
    flaskProcess = null;
  }
}
