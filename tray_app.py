"""
Ollama Chat — Windows Tray Application
Pornește serverul Flask, stă în system tray, oferă fereastră de administrare.
"""
import sys
import os
import threading
import subprocess
import time
import webbrowser
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
import queue
import winreg

import pystray
from PIL import Image
import urllib.request

# ─── Config ──────────────────────────────────────────────────────────────────
APP_NAME   = 'OllamaChat'
DEFAULT_PORT = 5050
AUTORUN_KEY  = r'Software\Microsoft\Windows\CurrentVersion\Run'
LOG_LINES    = 200

# ─── Paths ───────────────────────────────────────────────────────────────────
def _base():
    return getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))

def _appdata():
    p = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), APP_NAME)
    os.makedirs(p, exist_ok=True)
    return p

def _server_exe():
    candidates = [
        os.path.join(os.path.dirname(sys.executable), 'server', 'server.exe'),
        os.path.join(_base(), 'server', 'server.exe'),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), 'server_dist', 'server', 'server.exe'),
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return None

# ─── Port config (persisted in AppData) ──────────────────────────────────────
def _cfg_path():
    return os.path.join(_appdata(), 'tray_config.json')

def load_port():
    try:
        import json
        with open(_cfg_path()) as f:
            return int(json.load(f).get('port', DEFAULT_PORT))
    except Exception:
        return DEFAULT_PORT

def save_port(port):
    import json
    with open(_cfg_path(), 'w') as f:
        json.dump({'port': port}, f)

# ─── Auto-start helpers ───────────────────────────────────────────────────────
def _exe_path():
    return sys.executable if getattr(sys, 'frozen', False) else os.path.abspath(__file__)

def get_autostart():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTORUN_KEY) as k:
            winreg.QueryValueEx(k, APP_NAME)
            return True
    except Exception:
        return False

def set_autostart(enable):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTORUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            if enable:
                winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, f'"{_exe_path()}"')
            else:
                try:
                    winreg.DeleteValue(k, APP_NAME)
                except FileNotFoundError:
                    pass
    except Exception as e:
        return str(e)

# ─── Server status check ──────────────────────────────────────────────────────
def server_alive(port):
    try:
        urllib.request.urlopen(f'http://127.0.0.1:{port}/', timeout=2)
        return True
    except Exception:
        return False

# ─── Main App ─────────────────────────────────────────────────────────────────
class TrayApp:
    def __init__(self):
        self.port        = load_port()
        self.server_proc = None
        self.start_time  = None
        self.log_q       = queue.Queue()
        self.icon        = None
        self.admin_win   = None

        self.root = tk.Tk()
        self.root.title('Ollama Chat — Server Manager')
        self.root.geometry('520x540')
        self.root.resizable(False, True)
        self.root.protocol('WM_DELETE_WINDOW', self._hide_admin)
        try:
            self.root.iconbitmap(os.path.join(_base(), 'assets', 'icon.ico'))
        except Exception:
            pass
        self._build_admin_ui(self.root)
        self.root.withdraw()   # hide until user opens it

    # ── Server management ────────────────────────────────────────────────────
    def _start_server(self):
        exe = _server_exe()
        if not exe:
            self._log('❌ server.exe nu a fost găsit!')
            return
        env = {**os.environ,
               'FLASK_PORT': str(self.port),
               'OLLAMA_CHAT_DATA': _appdata()}
        self.server_proc = subprocess.Popen(
            [exe], env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0,
        )
        self.start_time = time.time()
        self._log(f'▶ Server pornit (PID {self.server_proc.pid}) pe port {self.port}')
        threading.Thread(target=self._read_server_log, daemon=True).start()

    def _stop_server(self):
        if self.server_proc and self.server_proc.poll() is None:
            self.server_proc.terminate()
            try:
                self.server_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.server_proc.kill()
            self._log('⏹ Server oprit.')
        self.server_proc = None
        self.start_time  = None

    def _restart_server(self, *_):
        self._log('↺ Restart server...')
        self._stop_server()
        time.sleep(0.5)
        self._start_server()
        if self.admin_win:
            self.root.after(0, self._refresh_admin)

    def _read_server_log(self):
        proc = self.server_proc
        for line in proc.stdout:
            self._log(line.rstrip())
        self._log(f'⚠ Server process s-a încheiat (cod {proc.returncode})')

    def _log(self, msg):
        ts  = time.strftime('%H:%M:%S')
        self.log_q.put(f'[{ts}] {msg}')

    # ── Tray icon ────────────────────────────────────────────────────────────
    def _create_icon(self):
        img = Image.open(os.path.join(_base(), 'assets', 'icon.ico'))
        menu = pystray.Menu(
            pystray.MenuItem('Deschide în browser', self._open_browser, default=True),
            pystray.MenuItem('Server Manager',      self._show_admin_from_tray),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem('Restart Server',      self._restart_server),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem('Stop & Exit',         self._quit),
        )
        self.icon = pystray.Icon(APP_NAME, img, 'Ollama Chat', menu)
        self.icon.run_detached()

    def _open_browser(self, *_):
        webbrowser.open(f'http://127.0.0.1:{self.port}/')

    def _show_admin_from_tray(self, *_):
        self.root.after(0, self._show_admin)

    def _quit(self, *_):
        self._stop_server()
        if self.icon:
            self.icon.stop()
        self.root.after(0, self.root.destroy)

    # ── Admin window ─────────────────────────────────────────────────────────
    def _show_admin(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def _hide_admin(self):
        self.root.withdraw()

    def _build_admin_ui(self, w):
        """Build admin UI widgets into window w (called once on root)."""
        pad = dict(padx=12, pady=4)

        # ── Status bar ───────────────────────────────────────────────────────
        sf = ttk.LabelFrame(w, text='Status server')
        sf.pack(fill='x', **pad, pady=(12,4))

        self._lbl_status = tk.Label(sf, text='', font=('Segoe UI', 10, 'bold'), anchor='w')
        self._lbl_status.pack(fill='x', padx=8, pady=4)

        self._lbl_uptime = tk.Label(sf, text='', font=('Segoe UI', 9), anchor='w', fg='#888')
        self._lbl_uptime.pack(fill='x', padx=8, pady=(0,4))

        # ── Action buttons ───────────────────────────────────────────────────
        bf = ttk.Frame(w)
        bf.pack(fill='x', **pad, pady=4)

        ttk.Button(bf, text='▶  Deschide în Browser',
                   command=self._open_browser).pack(side='left', padx=(0,6))
        ttk.Button(bf, text='↺  Restart',
                   command=self._restart_server).pack(side='left', padx=(0,6))
        ttk.Button(bf, text='⏹  Stop & Exit',
                   command=self._quit).pack(side='left')

        # ── Port config ──────────────────────────────────────────────────────
        pf = ttk.LabelFrame(w, text='Configurare')
        pf.pack(fill='x', **pad, pady=4)

        row1 = ttk.Frame(pf)
        row1.pack(fill='x', padx=8, pady=6)
        tk.Label(row1, text='Port:').pack(side='left')
        self._port_var = tk.StringVar(value=str(self.port))
        pe = ttk.Entry(row1, textvariable=self._port_var, width=8)
        pe.pack(side='left', padx=6)
        ttk.Button(row1, text='Aplică & Restart',
                   command=self._apply_port).pack(side='left')

        row2 = ttk.Frame(pf)
        row2.pack(fill='x', padx=8, pady=(0,6))
        self._autostart_var = tk.BooleanVar(value=get_autostart())
        ttk.Checkbutton(row2, text='Pornire automată cu Windows',
                        variable=self._autostart_var,
                        command=self._toggle_autostart).pack(side='left')

        # ── Log ──────────────────────────────────────────────────────────────
        lf = ttk.LabelFrame(w, text='Log server')
        lf.pack(fill='both', expand=True, **pad, pady=(4,12))

        self._log_box = scrolledtext.ScrolledText(
            lf, height=12, font=('Consolas', 9),
            bg='#1a1a1a', fg='#cccccc', state='disabled',
            wrap='word', relief='flat')
        self._log_box.pack(fill='both', expand=True, padx=4, pady=4)

        self._poll_admin()

    def _refresh_admin(self):
        if not self.root.winfo_exists():
            return
        alive = server_alive(self.port)
        if alive:
            self._lbl_status.config(text=f'● Rulează  (port {self.port})', fg='#22c55e')
        else:
            self._lbl_status.config(text='● Oprit', fg='#ef4444')

        if self.start_time and alive:
            secs = int(time.time() - self.start_time)
            h, r = divmod(secs, 3600)
            m, s = divmod(r, 60)
            self._lbl_uptime.config(text=f'Uptime: {h:02d}:{m:02d}:{s:02d}')
        else:
            self._lbl_uptime.config(text='')

    def _poll_admin(self):
        if not self.root.winfo_exists():
            return
        self._refresh_admin()
        # Flush log queue into text widget
        lines = []
        while not self.log_q.empty():
            try:
                lines.append(self.log_q.get_nowait())
            except queue.Empty:
                break
        if lines:
            self._log_box.config(state='normal')
            self._log_box.insert('end', '\n'.join(lines) + '\n')
            total = int(self._log_box.index('end-1c').split('.')[0])
            if total > LOG_LINES:
                self._log_box.delete('1.0', f'{total - LOG_LINES}.0')
            self._log_box.see('end')
            self._log_box.config(state='disabled')
        self.root.after(1000, self._poll_admin)

    def _apply_port(self):
        try:
            p = int(self._port_var.get())
            assert 1024 <= p <= 65535
        except Exception:
            messagebox.showerror('Port invalid', 'Introdu un port între 1024 și 65535.')
            return
        self.port = p
        save_port(p)
        self._restart_server()

    def _toggle_autostart(self):
        err = set_autostart(self._autostart_var.get())
        if err:
            messagebox.showerror('Eroare registru', err)

    # ── Main loop ────────────────────────────────────────────────────────────
    def run(self):
        self._start_server()
        self._create_icon()
        # Wait for server then open browser
        def _open_when_ready():
            for _ in range(30):
                if server_alive(self.port):
                    self.root.after(0, self._open_browser)
                    return
                time.sleep(0.5)
        threading.Thread(target=_open_when_ready, daemon=True).start()
        self.root.mainloop()


if __name__ == '__main__':
    TrayApp().run()
