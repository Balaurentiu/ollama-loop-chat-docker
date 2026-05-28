"""
AI Discussion v2 — Server Flask
Multi-provider: Ollama, OpenAI, Google Gemini, Anthropic
"""

from flask import Flask, request, Response, jsonify, send_from_directory, send_file
import requests
import json
import os
import re
import sys
import sqlite3
import time
import threading
import uuid as _uuid
# sys.path.insert(0, '/home') (Commented out for Docker)

_sessions = {}        # {session_id: {last_seen, is_controller, label}}
_controller_id = None
_sessions_lock = threading.Lock()
SESSION_TIMEOUT = 45  # secunde fără heartbeat = deconectat

def _cleanup_sessions():
    global _controller_id
    now = time.time()
    with _sessions_lock:
        stale = [sid for sid, s in list(_sessions.items()) if now - s['last_seen'] > SESSION_TIMEOUT]
        for sid in stale:
            del _sessions[sid]
        if _controller_id not in _sessions:
            _controller_id = None
            if _sessions:
                first = next(iter(_sessions))
                _controller_id = first
                _sessions[first]['is_controller'] = True

def _sessions_summary():
    now = time.time()
    return [{'session_id': sid, 'is_controller': s['is_controller'],
             'label': s.get('label', sid[:8]),
             'idle': int(now - s['last_seen'])} for sid, s in _sessions.items()]
try:
    import chat_export as _chat_export
except ImportError:
    _chat_export = None

app = Flask(__name__)

# When bundled with PyInstaller, read-only assets (index.html) live in sys._MEIPASS.
# Writable data files (settings.json, chat.json, ws_cache.db) use DATA_DIR,
# which Electron sets via the OLLAMA_CHAT_DATA env var to the user's app-data folder.
if getattr(sys, 'frozen', False):
    ASSET_DIR = sys._MEIPASS
else:
    ASSET_DIR = os.path.dirname(os.path.abspath(__file__))

DATA_DIR = os.environ.get('OLLAMA_CHAT_DATA', ASSET_DIR)
os.makedirs(DATA_DIR, exist_ok=True)

BASE_DIR = ASSET_DIR  # legacy alias used throughout the file

# ── WS SQLITE CACHE ───────────────────────────────────────────────────────────

WS_DB_PATH = os.path.join(DATA_DIR, 'ws_cache.db')
_db_lock = threading.Lock()


def _db_conn():
    conn = sqlite3.connect(WS_DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _db_init():
    with _db_lock:
        conn = _db_conn()
        conn.execute('''
            CREATE TABLE IF NOT EXISTS ws_cache (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                query_key    TEXT NOT NULL,
                query        TEXT NOT NULL,
                reason       TEXT,
                url          TEXT,
                title        TEXT,
                summary      TEXT,
                full_content TEXT,
                content_size INTEGER DEFAULT 0,
                success      INTEGER DEFAULT 1,
                created_at   REAL NOT NULL
            )
        ''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_query_key ON ws_cache(query_key)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_url ON ws_cache(url)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_created ON ws_cache(created_at)')
        conn.commit()
        conn.close()


_db_init()


def _cache_get(query_key, ttl_seconds):
    """Caută în cache după query_key normalizat. Returnează row sau None."""
    if ttl_seconds <= 0:
        return None
    cutoff = time.time() - ttl_seconds
    with _db_lock:
        conn = _db_conn()
        row = conn.execute(
            'SELECT * FROM ws_cache WHERE query_key=? AND created_at>? ORDER BY created_at DESC LIMIT 1',
            (query_key, cutoff)
        ).fetchone()
        conn.close()
    return row


def _cache_get_by_url(url, ttl_seconds):
    """Caută în cache după URL exact. Util pentru deduplicare cross-query."""
    if ttl_seconds <= 0:
        return None
    cutoff = time.time() - ttl_seconds
    with _db_lock:
        conn = _db_conn()
        row = conn.execute(
            'SELECT * FROM ws_cache WHERE url=? AND created_at>? ORDER BY created_at DESC LIMIT 1',
            (url, cutoff)
        ).fetchone()
        conn.close()
    return row


def _cache_store(query_key, query, reason, url, title, summary, full_content, content_size, success=1):
    with _db_lock:
        conn = _db_conn()
        conn.execute(
            '''INSERT INTO ws_cache
               (query_key, query, reason, url, title, summary, full_content, content_size, success, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)''',
            (query_key, query, reason, url, title, summary, full_content, content_size, success, time.time())
        )
        conn.commit()
        conn.close()


def _cache_clear():
    with _db_lock:
        conn = _db_conn()
        conn.execute('DELETE FROM ws_cache')
        conn.commit()
        conn.close()


def _cache_stats():
    with _db_lock:
        conn = _db_conn()
        total = conn.execute('SELECT COUNT(*) FROM ws_cache').fetchone()[0]
        oldest = conn.execute('SELECT MIN(created_at) FROM ws_cache').fetchone()[0]
        newest = conn.execute('SELECT MAX(created_at) FROM ws_cache').fetchone()[0]
        total_size = conn.execute('SELECT SUM(content_size) FROM ws_cache').fetchone()[0] or 0
        entries = conn.execute(
            'SELECT id, query, title, url, created_at, content_size FROM ws_cache ORDER BY created_at DESC LIMIT 50'
        ).fetchall()
        conn.close()
    return {
        'total': total,
        'oldest': oldest,
        'newest': newest,
        'total_content_size': total_size,
        'entries': [dict(e) for e in entries],
    }


@app.route("/")

def index():
    return send_from_directory(ASSET_DIR, "index.html")

# ─── Debug upload page (temporary) ───────────────────────────────────────────
_debug_uploads = []  # list of {id, filename, comment, time, kind}

@app.route("/debug")
def debug_page():
    items_html = ""
    for u in reversed(_debug_uploads):
        if u['kind'] == 'text':
            content_html = f'<pre style="background:#0d0d1a;padding:12px;border-radius:6px;overflow-x:auto;max-height:400px;font-size:12px;color:#ccc;white-space:pre-wrap;word-break:break-all">{u["text"]}</pre>'
        else:
            content_html = f'<img src="/debug/img/{u["id"]}" style="max-width:100%;border-radius:6px;border:1px solid #333">'
        items_html += f"""
        <div style="border:1px solid #333;border-radius:8px;padding:16px;margin-bottom:20px;background:#1e1e2e">
          <div style="color:#888;font-size:12px;margin-bottom:4px">{u['time']} &nbsp;·&nbsp; <code style="color:#4a9eff">{u['filename']}</code></div>
          <div style="color:#fff;font-size:14px;margin-bottom:12px;white-space:pre-wrap">{u['comment'] or '<em style=color:#555>fără comentariu</em>'}</div>
          {content_html}
        </div>"""
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
    <title>Debug Upload</title>
    <style>body{{background:#111;color:#eee;font-family:sans-serif;max-width:900px;margin:40px auto;padding:0 20px}}
    input,textarea{{background:#1e1e2e;border:1px solid #444;color:#fff;border-radius:6px;padding:8px;width:100%;box-sizing:border-box;margin-top:6px}}
    button{{background:#4a9eff;color:#fff;border:none;padding:10px 24px;border-radius:6px;cursor:pointer;font-size:14px;margin-top:10px}}
    h2{{color:#4a9eff}}</style></head><body>
    <h2>🐛 Debug Upload</h2>
    <form method="POST" action="/debug/upload" enctype="multipart/form-data">
      <label>Fișier (PNG/JPG/TXT/LOG):<br><input type="file" name="file" required></label><br><br>
      <label>Comentariu / descriere eroare:<br><textarea name="comment" rows="4" placeholder="Descrie ce vezi sau ce s-a întâmplat..."></textarea></label><br>
      <button type="submit">⬆ Trimite</button>
    </form>
    <hr style="border-color:#333;margin:30px 0">
    <h3>Uploads ({len(_debug_uploads)})</h3>
    {items_html or '<p style="color:#555">Niciun upload încă.</p>'}
    <script>setTimeout(()=>location.reload(), 15000)</script>
    </body></html>"""

@app.route("/debug/upload", methods=["POST"])
def debug_upload():
    import uuid, datetime, html as html_lib
    f = request.files.get("file")
    if not f:
        return "no file", 400
    uid = str(uuid.uuid4())[:8]
    data = f.read()
    comment = html_lib.escape(request.form.get("comment", "").strip())
    fname = f.filename or ''
    is_text = fname.lower().endswith(('.txt', '.log')) or (f.content_type or '').startswith('text/')
    entry = {
        "id": uid, "data": data, "comment": comment,
        "time": datetime.datetime.now().strftime("%H:%M:%S"),
        "filename": html_lib.escape(fname),
        "kind": "text" if is_text else "image",
    }
    if is_text:
        entry["text"] = html_lib.escape(data.decode('utf-8', errors='replace'))
    _debug_uploads.append(entry)
    return f"""<html><body style="background:#111;color:#eee;font-family:sans-serif;text-align:center;padding:60px">
    <h2 style="color:#22c55e">✓ Upload reușit!</h2>
    <a href="/debug" style="color:#4a9eff">← Înapoi</a></body></html>"""

@app.route("/debug/img/<uid>")
def debug_img(uid):
    for u in _debug_uploads:
        if u["id"] == uid and u["kind"] == "image":
            from flask import make_response
            r = make_response(u["data"])
            r.headers["Content-Type"] = "image/png"
            return r
    return "not found", 404


# ── MODELE ───────────────────────────────────────────────────────────────────

ANTHROPIC_MODELS = [
    "claude-opus-4-6",
    "claude-sonnet-4-6",
    "claude-haiku-4-5-20251001",
    "claude-opus-4-5",
    "claude-sonnet-4-5",
    "claude-3-5-sonnet-20241022",
    "claude-3-5-haiku-20241022",
    "claude-3-opus-20240229",
    "claude-3-sonnet-20240229",
    "claude-3-haiku-20240307",
]


@app.route("/api/models")
def get_models():
    provider = request.args.get("provider", "ollama")

    if provider == "ollama":
        server = request.args.get("server", "http://192.168.0.17:11434").rstrip("/")
        try:
            resp = requests.get(f"{server}/api/tags", timeout=10)
            resp.raise_for_status()
            models = [m["name"] for m in resp.json().get("models", [])]
            return jsonify({"models": sorted(models)})
        except requests.exceptions.ConnectionError:
            return jsonify({"error": "Nu se poate conecta la Ollama."}), 503
        except requests.exceptions.Timeout:
            return jsonify({"error": "Timeout — Ollama nu răspunde."}), 504
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    elif provider == "openai":
        api_key = request.args.get("apiKey", "")
        if not api_key:
            return jsonify({"error": "API key lipsă"}), 400
        try:
            resp = requests.get(
                "https://api.openai.com/v1/models",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=10,
            )
            resp.raise_for_status()
            all_models = [m["id"] for m in resp.json().get("data", [])]
            # Filtrare modele relevante
            models = sorted([
                m for m in all_models
                if any(p in m for p in ("gpt-4", "gpt-3.5", "o1", "o3", "o4"))
            ])
            return jsonify({"models": models})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    elif provider == "gemini":
        api_key = request.args.get("apiKey", "")
        if not api_key:
            return jsonify({"error": "API key lipsă"}), 400
        try:
            resp = requests.get(
                f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}",
                timeout=10,
            )
            resp.raise_for_status()
            models = []
            for m in resp.json().get("models", []):
                if "generateContent" in m.get("supportedGenerationMethods", []):
                    name = m["name"].split("/")[-1]
                    models.append(name)
            return jsonify({"models": sorted(models)})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    elif provider == "anthropic":
        # Anthropic nu are endpoint de listare — returnam lista hardcodata
        return jsonify({"models": ANTHROPIC_MODELS})

    else:
        return jsonify({"error": f"Provider necunoscut: {provider}"}), 400


# ── CHAT STREAMING ────────────────────────────────────────────────────────────

def _emit(content="", thinking="", done=False, error=None):
    """Emite un chunk NDJSON normalizat."""
    obj = {"content": content, "thinking": thinking, "done": done}
    if error:
        obj["error"] = error
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")


@app.route("/api/chat", methods=["POST"])
def chat():
    data = request.get_json()
    if not data:
        return jsonify({"error": "Body JSON invalid"}), 400

    provider = data.get("provider", "ollama")

    generators = {
        "ollama":    _stream_ollama,
        "openai":    _stream_openai,
        "gemini":    _stream_gemini,
        "anthropic": _stream_anthropic,
    }

    if provider not in generators:
        return jsonify({"error": f"Provider necunoscut: {provider}"}), 400

    return Response(generators[provider](data), content_type="application/x-ndjson")


def _stream_ollama(data):
    server  = data.get("serverUrl", "http://192.168.0.17:11434").rstrip("/")
    model   = data.get("model", "")
    msgs    = list(data.get("messages", []))
    system  = data.get("systemPrompt", "")
    temp       = data.get("temperature", 0.7)
    ctx        = data.get("contextSize", 4096)
    think      = data.get("thinkEnabled", False)
    force_cpu  = data.get("forceCpu", False)
    keep_alive = data.get("keepAlive", "")
    timeout    = int(data.get("requestTimeout", 300))

    if system:
        msgs = [{"role": "system", "content": system}] + msgs

    options = {"num_ctx": ctx, "temperature": temp}
    if force_cpu:
        options["num_gpu"] = 0

    payload = {
        "model": model,
        "messages": msgs,
        "stream": True,
        "options": options,
    }
    if think:
        payload["think"] = True
    if keep_alive != "":
        payload["keep_alive"] = int(keep_alive) if keep_alive in ("-1", "0") else keep_alive

    try:
        with requests.post(
            f"{server}/api/chat", json=payload, stream=True, timeout=timeout
        ) as resp:
            if not resp.ok:
                try:
                    err_body = resp.json()
                    err_msg = err_body.get("error", resp.text)
                except Exception:
                    err_msg = resp.text
                yield _emit(error=f"Ollama {resp.status_code}: {err_msg}", done=True)
                return
            resp.raise_for_status()
            content_sent = False
            for chunk in resp.iter_lines():
                if chunk:
                    try:
                        obj = json.loads(chunk)
                        if obj.get("error"):
                            err_msg = obj["error"]
                            # Ollama tool-call parse error — recover raw content only if
                            # nothing was streamed yet (avoid duplication)
                            if "error parsing tool call" in err_msg:
                                if not content_sent:
                                    raw_match = re.search(r"raw='([\s\S]*?)'(?:,\s*err=|$)", err_msg)
                                    if raw_match:
                                        yield _emit(content=raw_match.group(1), done=True)
                                else:
                                    # Content already streamed — just close normally
                                    yield _emit(done=True)
                                return
                            yield _emit(error=err_msg, done=True)
                            return
                        msg = obj.get("message", {})
                        c = msg.get("content", "")
                        if c:
                            content_sent = True
                        yield _emit(
                            content=c,
                            thinking=msg.get("thinking", ""),
                            done=obj.get("done", False),
                        )
                    except json.JSONDecodeError:
                        pass
    except requests.exceptions.ConnectionError:
        yield _emit(error="Conexiune pierdută cu Ollama.", done=True)
    except requests.exceptions.Timeout:
        yield _emit(error="Timeout la generare.", done=True)
    except Exception as e:
        yield _emit(error=str(e), done=True)


def _stream_openai(data):
    api_key    = data.get("apiKey", "")
    model      = data.get("model", "gpt-4o")
    msgs       = list(data.get("messages", []))
    system     = data.get("systemPrompt", "")
    temp       = data.get("temperature", 0.7)
    max_tokens = data.get("contextSize", 4096)
    timeout    = int(data.get("requestTimeout", 300))

    if system:
        msgs = [{"role": "system", "content": system}] + msgs

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": msgs,
        "temperature": temp,
        "max_tokens": max_tokens,
        "stream": True,
    }

    try:
        with requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers=headers, json=payload, stream=True, timeout=timeout,
        ) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines():
                if not line:
                    continue
                if isinstance(line, bytes):
                    line = line.decode("utf-8")
                if not line.startswith("data: "):
                    continue
                chunk = line[6:]
                if chunk.strip() == "[DONE]":
                    yield _emit(done=True)
                    return
                try:
                    obj = json.loads(chunk)
                    delta = obj["choices"][0]["delta"]
                    content = delta.get("content", "") or ""
                    done = obj["choices"][0].get("finish_reason") is not None
                    yield _emit(content=content, done=done)
                except (json.JSONDecodeError, KeyError, IndexError):
                    pass
        yield _emit(done=True)
    except Exception as e:
        yield _emit(error=str(e), done=True)


def _stream_gemini(data):
    api_key    = data.get("apiKey", "")
    model      = data.get("model", "gemini-2.0-flash")
    msgs       = list(data.get("messages", []))
    system     = data.get("systemPrompt", "")
    temp       = data.get("temperature", 0.7)
    timeout    = int(data.get("requestTimeout", 300))
    max_tokens = data.get("contextSize", 4096)

    # Conversie mesaje → format Gemini (alternare user/model obligatorie)
    contents = []
    for msg in msgs:
        role = "user" if msg["role"] == "user" else "model"
        if contents and contents[-1]["role"] == role:
            contents[-1]["parts"][0]["text"] += "\n" + msg["content"]
        else:
            contents.append({"role": role, "parts": [{"text": msg["content"]}]})

    # Gemini cere sa inceapa cu user
    if not contents or contents[0]["role"] != "user":
        contents.insert(0, {"role": "user", "parts": [{"text": "(start)"}]})

    # Nu poate termina cu model
    if contents and contents[-1]["role"] == "model":
        contents.append({"role": "user", "parts": [{"text": "(continuă)"}]})

    payload = {
        "contents": contents,
        "generationConfig": {
            "temperature": temp,
            "maxOutputTokens": max_tokens,
        },
    }
    if system:
        payload["systemInstruction"] = {"parts": [{"text": system}]}

    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:streamGenerateContent?key={api_key}&alt=sse"
    )

    try:
        with requests.post(url, json=payload, stream=True, timeout=timeout) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines():
                if not line:
                    continue
                if isinstance(line, bytes):
                    line = line.decode("utf-8")
                if not line.startswith("data: "):
                    continue
                try:
                    obj = json.loads(line[6:])
                    candidates = obj.get("candidates", [])
                    if not candidates:
                        continue
                    parts = candidates[0].get("content", {}).get("parts", [])
                    content = "".join(p.get("text", "") for p in parts)
                    finish = candidates[0].get("finishReason")
                    done = finish not in (None, "", "STOP") or finish == "STOP"
                    yield _emit(content=content, done=(finish == "STOP"))
                except json.JSONDecodeError:
                    pass
        yield _emit(done=True)
    except Exception as e:
        yield _emit(error=str(e), done=True)


def _stream_anthropic(data):
    api_key    = data.get("apiKey", "")
    model      = data.get("model", "claude-sonnet-4-6")
    msgs       = list(data.get("messages", []))
    system     = data.get("systemPrompt", "")
    temp       = data.get("temperature", 0.7)
    timeout    = int(data.get("requestTimeout", 300))
    max_tokens = data.get("contextSize", 4096)

    # Anthropic: mesajele trebuie sa alterneze user/assistant, sa inceapa cu user
    clean = []
    for msg in msgs:
        role = msg["role"] if msg["role"] in ("user", "assistant") else "user"
        if clean and clean[-1]["role"] == role:
            clean[-1]["content"] += "\n\n" + msg["content"]
        else:
            clean.append({"role": role, "content": msg["content"]})

    if not clean or clean[0]["role"] != "user":
        clean.insert(0, {"role": "user", "content": "(start)"})

    headers = {
        "x-api-key": api_key,
        "Content-Type": "application/json",
        "anthropic-version": "2023-06-01",
    }
    payload = {
        "model": model,
        "messages": clean,
        "temperature": temp,
        "max_tokens": max_tokens,
        "stream": True,
    }
    if system:
        payload["system"] = system

    try:
        with requests.post(
            "https://api.anthropic.com/v1/messages",
            headers=headers, json=payload, stream=True, timeout=timeout,
        ) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines():
                if not line:
                    continue
                if isinstance(line, bytes):
                    line = line.decode("utf-8")
                if not line.startswith("data: "):
                    continue
                try:
                    obj = json.loads(line[6:])
                    evt = obj.get("type", "")
                    if evt == "content_block_delta":
                        content = obj.get("delta", {}).get("text", "")
                        yield _emit(content=content)
                    elif evt == "message_stop":
                        yield _emit(done=True)
                        return
                    elif evt == "error":
                        yield _emit(error=obj.get("error", {}).get("message", "Eroare Anthropic"), done=True)
                        return
                except json.JSONDecodeError:
                    pass
        yield _emit(done=True)
    except Exception as e:
        yield _emit(error=str(e), done=True)


# ── SETĂRI ────────────────────────────────────────────────────────────────────

SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")


@app.route("/api/settings", methods=["GET"])
def get_settings():
    if not os.path.exists(SETTINGS_FILE):
        return jsonify({})
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            return jsonify(json.load(f))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/settings", methods=["POST"])
def save_settings():
    data = request.get_json()
    if not data:
        return jsonify({"error": "Body JSON invalid"}), 400
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── WS CACHE ENDPOINTS ────────────────────────────────────────────────────────

@app.route('/api/ws-cache/clear', methods=['POST'])
def ws_cache_clear():
    _cache_clear()
    return jsonify({'ok': True})


@app.route('/api/ws-cache/stats', methods=['GET'])
def ws_cache_stats():
    return jsonify(_cache_stats())


# ── CHAT HISTORY ──────────────────────────────────────────────────────────────

CHAT_FILE = os.path.join(DATA_DIR, "chat.json")


@app.route("/api/chat-history", methods=["GET"])
def get_chat_history():
    if not os.path.exists(CHAT_FILE):
        return jsonify({})
    try:
        with open(CHAT_FILE, "r", encoding="utf-8") as f:
            return jsonify(json.load(f))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/chat-history", methods=["POST"])
def save_chat_history():
    data = request.get_json()
    if not data:
        return jsonify({"error": "Body JSON invalid"}), 400
    try:
        with open(CHAT_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/chat-history", methods=["DELETE"])
def delete_chat_history():
    try:
        if os.path.exists(CHAT_FILE):
            with open(CHAT_FILE, "w", encoding="utf-8") as f:
                json.dump({}, f)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── WEB SEARCH ────────────────────────────────────────────────────────────────

def _call_llm_simple(provider, model, prompt, api_key='', server_url='http://192.168.0.17:11434', temp=0.3, ctx=8192):
    """Non-streaming LLM call. Returns text response string."""
    if provider == 'ollama':
        server = server_url.rstrip('/')
        payload = {
            'model': model,
            'messages': [{'role': 'user', 'content': prompt}],
            'stream': False,
            'options': {'temperature': temp, 'num_ctx': ctx},
        }
        resp = requests.post(f'{server}/api/chat', json=payload, timeout=90)
        resp.raise_for_status()
        return resp.json().get('message', {}).get('content', '')

    elif provider == 'openai':
        headers = {
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json',
        }
        payload = {
            'model': model,
            'messages': [{'role': 'user', 'content': prompt}],
            'temperature': temp,
            'max_tokens': ctx,
            'stream': False,
        }
        resp = requests.post('https://api.openai.com/v1/chat/completions',
                             headers=headers, json=payload, timeout=90)
        resp.raise_for_status()
        return resp.json()['choices'][0]['message']['content']

    elif provider == 'gemini':
        payload = {
            'contents': [{'role': 'user', 'parts': [{'text': prompt}]}],
            'generationConfig': {'temperature': temp, 'maxOutputTokens': ctx},
        }
        url = (f'https://generativelanguage.googleapis.com/v1beta/models/'
               f'{model}:generateContent?key={api_key}')
        resp = requests.post(url, json=payload, timeout=90)
        resp.raise_for_status()
        candidates = resp.json().get('candidates', [])
        if candidates:
            parts = candidates[0].get('content', {}).get('parts', [])
            return ''.join(p.get('text', '') for p in parts)
        return ''

    elif provider == 'anthropic':
        headers = {
            'x-api-key': api_key,
            'Content-Type': 'application/json',
            'anthropic-version': '2023-06-01',
        }
        payload = {
            'model': model,
            'messages': [{'role': 'user', 'content': prompt}],
            'temperature': temp,
            'max_tokens': ctx,
        }
        resp = requests.post('https://api.anthropic.com/v1/messages',
                             headers=headers, json=payload, timeout=90)
        resp.raise_for_status()
        content = resp.json().get('content', [])
        return ''.join(c.get('text', '') for c in content if c.get('type') == 'text')

    else:
        raise ValueError(f'Unknown provider: {provider}')


def _search_ddg(query, max_results=5, region='wt-wt'):
    """Search DuckDuckGo. Returns list of {title, url, snippet}."""
    try:
        from ddgs import DDGS
    except ImportError:
        from duckduckgo_search import DDGS

    results = []
    with DDGS() as ddgs:
        for r in ddgs.text(query, region=region, safesearch='off', max_results=max_results):
            results.append({
                'title': r.get('title', ''),
                'url': r.get('href', ''),
                'snippet': r.get('body', ''),
            })
    return results


def _fetch_page_content(url, max_size=30000):
    """Fetch URL and extract text. Returns (text, content_type_str)."""
    import re as _re
    headers = {
        'User-Agent': ('Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
                       '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')
    }
    resp = requests.get(url, headers=headers, timeout=30, allow_redirects=True)
    resp.raise_for_status()
    content_type = resp.headers.get('Content-Type', '').lower()

    if 'text/html' in content_type or not content_type:
        # Try trafilatura first
        try:
            import trafilatura
            from trafilatura.settings import use_config
            _tcfg = use_config()
            text = trafilatura.extract(resp.text, include_links=False, include_tables=True, config=_tcfg)
            if text and len(text.strip()) > 100:
                return text[:max_size], 'html'
        except Exception:
            pass

        # Fallback: BeautifulSoup
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(resp.text, 'html.parser')
            for el in soup.find_all(['script', 'style', 'nav', 'footer', 'header', 'aside']):
                el.decompose()
            text = soup.get_text(separator='\n', strip=True)
            text = _re.sub(r'\n{3,}', '\n\n', text)
            if text.strip():
                return text[:max_size], 'html'
        except ImportError:
            pass

        # Last resort: regex strip
        text = _re.sub(r'<[^>]+>', '', resp.text)
        text = _re.sub(r'\s+', ' ', text).strip()
        return text[:max_size], 'html'

    if 'text/plain' in content_type:
        return resp.text[:max_size], 'text'

    # Generic fallback
    text = _re.sub(r'<[^>]+>', '', resp.text)
    text = _re.sub(r'\s+', ' ', text).strip()
    return text[:max_size], 'unknown'


_DEFAULT_WS_OPT_PROMPT = (
    "Your task is to formulate the best search query for DuckDuckGo.\n\n"
    "Today's date: {date}\n"
    "User's reason: {reason}\n"
    "User's query: {query}\n\n"
    "Instructions:\n"
    "1. Optimize the query for DuckDuckGo search (add relevant technical terms).\n"
    "2. ALWAYS write the query in English, regardless of input language.\n"
    "3. Keep it concise (max 10 words).\n"
    "4. Replace any outdated years in the query with the current year from Today's date (e.g. if today is 2026, replace 2024/2025 with 2026).\n"
    "5. DO NOT use search operators like site:, inurl:, intitle:, filetype:\n"
    "6. Return ONLY the optimized search query, nothing else. No quotes, no explanation."
)

_DEFAULT_WS_RANK_PROMPT = (
    "Evaluate these search results and rank them by relevance.\n\n"
    "REASON the user needs this information:\n{reason}\n\n"
    "SEARCH QUERY: {query}\n\n"
    "SEARCH RESULTS:\n{results}\n"
    "Instructions:\n"
    "1. Rank the results by relevance to the REASON (most relevant first).\n"
    "2. Prefer official sources: documentation, changelogs, official blogs, GitHub repos of the project.\n"
    "3. Exclude obviously irrelevant results (ads, unrelated topics, wrong language communities).\n"
    "4. Return ONLY the numbers of the top results in order, comma-separated.\n"
    "   Example: 2,1,4\n"
    "5. Maximum {max_fetch} results."
)

_DEFAULT_WS_REL_PROMPT = (
    "Evaluate if this web page is a relevant SOURCE for the user's needs.\n\n"
    "REASON: {reason}\n"
    "SEARCH QUERY: {query}\n\n"
    "PAGE TITLE: {title}\n"
    "PAGE URL: {url}\n"
    "PAGE CONTENT (sample):\n{content}\n\n"
    "Answer YES or NO.\n"
    "- Answer YES if this source TYPE is likely to contain the needed information "
    "(e.g. official docs, release pages, changelogs, official blogs, GitHub repos) "
    "— even if the specific version/detail isn't visible in this sample.\n"
    "- Answer NO only if the source is clearly unrelated (wrong project, wrong topic, forum noise, ads).\n"
    "One line: YES or NO followed by a brief reason."
)

_DEFAULT_WS_OVERVIEW_PROMPT = (
    "Read the following web page content and provide a brief structured overview.\n\n"
    "CONTENT:\n{content}\n\n"
    "Provide:\n"
    "- Page type (e.g. official docs, changelog, blog post, forum, GitHub repo, tutorial)\n"
    "- Main topics covered (2-4 bullet points)\n"
    "- Key details present: versions, dates, commands, names (if any)\n"
    "Be concise — maximum 8 bullet points total."
)

_DEFAULT_WS_SUMM_PROMPT = (
    "You are summarizing a web page for a user. Today's date is {date}.\n\n"
    "REASON the user needs this information: {reason}\n"
    "SEARCH QUERY: {query}\n"
    "SOURCE: {source_title} ({source_url})\n\n"
    "PAGE CONTENT:\n{content}\n\n"
    "Instructions:\n"
    "1. Summarize only what is relevant to the REASON — include as much detail as needed, but omit irrelevant content.\n"
    "2. Preserve specific details: versions, dates, numbers, commands, code snippets, names.\n"
    "3. If the content is outdated relative to today ({date}), mention it explicitly.\n"
    "4. Respond in the same language as the REASON.\n"
    "5. Start directly with the information found — no meta-commentary."
)

_DEFAULT_WS_ADAPTIVE_PROMPT = (
    "You are a web search planner. Given a research query, decide how many sub-queries and how many result sources are needed.\n\n"
    "Today's date: {date}\n"
    "REASON / RESEARCH GOAL: {reason}\n"
    "ORIGINAL QUERY: {query}\n\n"
    "Constraints set by user (do NOT exceed these):\n"
    "  max_queries_limit: {max_queries_limit}\n"
    "  max_sources_limit: {max_sources_limit}\n\n"
    "Rules:\n"
    "- Simple factual questions (one clear answer): max_queries=1, max_sources=1\n"
    "- Moderate questions (comparison, how-to, recent event): max_queries=1-2, max_sources=2-3\n"
    "- Complex research (multiple aspects, pros/cons, synthesis needed): max_queries=3-4, max_sources=3-5\n"
    "- Never exceed the user's limits above.\n\n"
    "Respond ONLY with a valid JSON object, no explanation, no markdown:\n"
    '{"max_queries": <int>, "max_sources": <int>, "reason": "<one short sentence why>"}'
)

_DEFAULT_WS_DECOMP_PROMPT = (
    "You are a search query decomposer. Your task is to break down a complex research question into {max_queries} independent, specific search queries that together cover all important aspects of the topic.\n\n"
    "Today's date: {date}\n"
    "REASON / RESEARCH GOAL: {reason}\n"
    "ORIGINAL QUERY: {query}\n\n"
    "Instructions:\n"
    "1. Generate between 2 and {max_queries} search queries, each targeting a DIFFERENT angle of the topic.\n"
    "2. Each query must be self-contained and specific enough for a web search engine.\n"
    "3. Avoid redundant or overlapping queries.\n"
    "4. If the original query is already specific enough and does not benefit from decomposition, output only 1 query (the original or an optimized version).\n"
    "5. IMPORTANT: Replace any outdated years in the queries with the current year from Today's date. "
    "For example, if Today's date is 2026-05-27 and the original query contains '2024' or '2025', replace them with '2026'.\n"
    "6. Output ONLY the queries, one per line, with no numbering, no explanations, no extra text.\n\n"
    "OUTPUT (one query per line):"
)

_DEFAULT_WS_MULTISYNTH_PROMPT = (
    "You are synthesizing web research results from multiple searches into a single coherent answer.\n\n"
    "Today's date: {date}\n"
    "RESEARCH GOAL: {reason}\n"
    "ORIGINAL QUERY: {query}\n\n"
    "RESULTS FROM MULTIPLE SEARCHES:\n{results}\n\n"
    "Instructions:\n"
    "1. Synthesize all the above search results into one comprehensive, well-structured answer.\n"
    "2. Eliminate redundancy — if multiple sources say the same thing, state it once.\n"
    "3. Preserve all specific details: numbers, dates, statistics, names, URLs.\n"
    "4. Organize by topic/theme, not by search query.\n"
    "5. If sources contradict each other, note the discrepancy explicitly.\n"
    "6. Respond in the same language as the RESEARCH GOAL.\n"
    "7. Start directly with the content — no meta-commentary about the search process."
)


def _run_single_query_pipeline(query, reason, original_query, config, chunk, _today, _re, live_stats=None):
    """Rulează pipeline-ul complet (search→rank→fetch→summarize) pentru un singur query.
    Yield-uiește chunk-uri bytes (log) și ca ultim element yielduiește lista summaries.
    """
    provider    = config.get('provider', 'ollama')
    model       = config.get('model', '')
    api_key     = config.get('apiKey', '')
    server_url  = config.get('serverUrl', 'http://localhost:11434')
    max_results       = int(config.get('maxResults', 5))
    max_fetch         = int(config.get('maxFetch', 3))
    max_page          = int(config.get('maxPageSize', 30000))
    brief_thr         = int(config.get('briefThreshold', 3000))
    region            = config.get('region', 'wt-wt')
    max_store_results = int(config.get('maxStoreResults', 1))
    overview_enabled  = bool(config.get('overviewEnabled', True))
    rank_prompt_tpl   = config.get('rankPrompt', '')     or _DEFAULT_WS_RANK_PROMPT
    rel_prompt_tpl    = config.get('relPrompt', '')      or _DEFAULT_WS_REL_PROMPT
    overview_prompt_tpl = config.get('overviewPrompt', '') or _DEFAULT_WS_OVERVIEW_PROMPT
    summ_prompt_tpl   = config.get('summPrompt', '')     or _DEFAULT_WS_SUMM_PROMPT
    cache_ttl         = int(config.get('cacheTTL', 7200))
    ctx_size          = int(config.get('contextSize', 8192))

    # Search
    yield chunk(log=f'  → DDG search: "{query}"')
    try:
        results = _search_ddg(query, max_results=max_results, region=region)
    except Exception as e:
        yield chunk(log=f'  Search failed: {e}')
        results = []

    if not results:
        yield chunk(log=f'  No results for "{query}"')
        return

    yield chunk(log=f'  Found {len(results)} results')

    # Rank
    ranked_indices = list(range(min(max_fetch, len(results))))
    try:
        results_text = ''
        for i, r in enumerate(results, 1):
            results_text += f'#{i}. {r["title"]}\n   {r["url"]}\n   {r["snippet"]}\n\n'
        rank_prompt = (
            rank_prompt_tpl
            .replace('{reason}', reason)
            .replace('{query}', query)
            .replace('{results}', results_text)
            .replace('{max_fetch}', str(max_fetch))
        )
        rank_resp = _call_llm_simple(provider, model, rank_prompt,
                                     api_key=api_key, server_url=server_url, temp=0.3, ctx=ctx_size).strip()
        ranked = []
        for num in _re.findall(r'\d+', rank_resp):
            idx = int(num) - 1
            if 0 <= idx < len(results) and idx not in ranked:
                ranked.append(idx)
        if ranked:
            ranked_indices = ranked[:max_fetch]
    except Exception as e:
        yield chunk(log=f'  Ranking failed ({e})')

    # Fetch + relevance + summarize
    summaries = []
    for idx in ranked_indices:
        if len(summaries) >= max_store_results:
            break
        r = results[idx]
        url   = r['url']
        title = r['title']

        # Cache check
        if cache_ttl > 0:
            url_cached = _cache_get_by_url(url, cache_ttl)
            if url_cached and url_cached['full_content']:
                yield chunk(log=f'  Cache hit: {title}')
                content = url_cached['full_content']
                title   = url_cached['title'] or title
                # summarize cached content
                summ_prompt = (
                    summ_prompt_tpl
                    .replace('{date}', _today)
                    .replace('{reason}', reason)
                    .replace('{query}', query)
                    .replace('{source_title}', title)
                    .replace('{source_url}', url)
                    .replace('{content}', content[:max_page])
                )
                try:
                    summary = _call_llm_simple(provider, model, summ_prompt,
                                               api_key=api_key, server_url=server_url, temp=0.3, ctx=ctx_size).strip()
                except Exception:
                    summary = ''
                if len(summary) < 50:
                    yield chunk(log=f'  Summary too short ({len(summary)} chars), skipping')
                    if live_stats is not None:
                        live_stats['sources_skip'] += 1
                        yield chunk(stats=dict(live_stats))
                    continue
                summaries.append({'summary': summary, 'title': title, 'url': url, 'content_size': len(content)})
                if live_stats is not None:
                    live_stats['sources_ok'] += 1
                    live_stats['chars'] += len(content)
                    yield chunk(stats=dict(live_stats))
                continue

        yield chunk(log=f'  Fetching: {url}')
        try:
            content, ctype = _fetch_page_content(url, max_size=max_page)
            if len(content.strip()) < 100:
                yield chunk(log='  Content too short, skipping')
                continue

            # Overview
            overview_text = content[:3000]
            if overview_enabled:
                try:
                    ov_prompt = overview_prompt_tpl.replace('{content}', content)
                    overview_text = _call_llm_simple(provider, model, ov_prompt,
                                                     api_key=api_key, server_url=server_url,
                                                     temp=0.1, ctx=ctx_size).strip()
                except Exception as e:
                    yield chunk(log=f'  Overview failed ({e})')

            # Relevance
            rel_prompt = (
                rel_prompt_tpl
                .replace('{reason}', reason)
                .replace('{query}', query)
                .replace('{title}', title)
                .replace('{url}', url)
                .replace('{content}', overview_text)
            )
            try:
                rel_resp = _call_llm_simple(provider, model, rel_prompt,
                                            api_key=api_key, server_url=server_url, temp=0.1, ctx=ctx_size).strip()
                is_relevant = rel_resp.upper().startswith('YES')
                yield chunk(log=f'  Relevance: {"YES" if is_relevant else "NO"} — {title}')
            except Exception as e:
                yield chunk(log=f'  Relevance check failed ({e}), accepting')
                is_relevant = True

            if not is_relevant:
                if live_stats is not None:
                    live_stats['sources_skip'] += 1
                    yield chunk(stats=dict(live_stats))
                continue

            # Summarize
            summ_prompt = (
                summ_prompt_tpl
                .replace('{date}', _today)
                .replace('{reason}', reason)
                .replace('{query}', query)
                .replace('{source_title}', title)
                .replace('{source_url}', url)
                .replace('{content}', content[:max_page])
            )
            try:
                summary = _call_llm_simple(provider, model, summ_prompt,
                                           api_key=api_key, server_url=server_url, temp=0.3, ctx=ctx_size).strip()
            except Exception as e:
                summary = ''
                yield chunk(log=f'  Summary failed ({e})')

            if len(summary) < 50:
                yield chunk(log=f'  Summary too short ({len(summary)} chars), skipping')
                if live_stats is not None:
                    live_stats['sources_skip'] += 1
                    yield chunk(stats=dict(live_stats))
                continue

            yield chunk(log=f'  Summary: {title} ({len(summary)} chars)')
            _cache_store(query.lower().strip(), query, reason, url, title, summary, content, len(content), success=1)
            summaries.append({'summary': summary, 'title': title, 'url': url, 'content_size': len(content)})
            if live_stats is not None:
                live_stats['sources_ok'] += 1
                live_stats['chars'] += len(content)
                yield chunk(stats=dict(live_stats))

        except Exception as e:
            yield chunk(log=f'  Fetch failed {url}: {e}')
            if live_stats is not None:
                live_stats['sources_err'] += 1
                yield chunk(stats=dict(live_stats))

    # Ultimul element yielded este lista de summaries (nu bytes)
    yield summaries


def _stream_websearch(data):
    """Generator that streams NDJSON chunks for web search progress."""
    import re as _re
    import time as _time

    reason = data.get('reason', '')
    query  = data.get('query', '')
    config = data.get('config', {})

    provider    = config.get('provider', 'ollama')
    model       = config.get('model', '')
    api_key     = config.get('apiKey', '')
    server_url  = config.get('serverUrl', 'http://localhost:11434')
    cache_ttl         = int(config.get('cacheTTL', 7200))
    ctx_size          = int(config.get('contextSize', 8192))
    brief_thr         = int(config.get('briefThreshold', 3000))
    opt_prompt_tpl    = config.get('optPrompt', '')       or _DEFAULT_WS_OPT_PROMPT
    decomp_prompt_tpl = config.get('decompPrompt', '')    or _DEFAULT_WS_DECOMP_PROMPT
    multisynth_prompt_tpl = config.get('multiSynthPrompt', '') or _DEFAULT_WS_MULTISYNTH_PROMPT
    multi_query_enabled = bool(config.get('multiQueryEnabled', False))
    max_queries       = max(1, min(4, int(config.get('maxQueries', 3))))
    max_store_results = max(1, int(config.get('maxStoreResults', 1)))
    adaptive_enabled  = bool(config.get('wsAdaptive', False))
    adaptive_prompt_tpl = config.get('adaptivePrompt', '') or _DEFAULT_WS_ADAPTIVE_PROMPT

    _live_stats = {'sources_ok': 0, 'sources_skip': 0, 'sources_err': 0, 'chars': 0, 'queries_done': 0}

    def chunk(step=None, step_status=None, log=None, done=False, result=None, stats=None):
        obj = {'done': done}
        if step is not None:
            obj['step'] = step
        if step_status:
            obj['stepStatus'] = step_status
        if log:
            obj['log'] = log
        if result is not None:
            obj['result'] = result
        if stats is not None:
            obj['stats'] = stats
        return (json.dumps(obj, ensure_ascii=False) + '\n').encode('utf-8')

    def stats_chunk():
        return chunk(stats=dict(_live_stats))

    try:
        import datetime as _dt
        _today = _dt.date.today().isoformat()

        # ── STEP 0: Optimize / Decompose query ──
        yield chunk(step=0, step_status='active', log=f'Reason: {reason}')
        yield chunk(log=f'Original query: {query}')

        # Cache check (whole query)
        cache_key = query.lower().strip()
        if cache_ttl > 0:
            cached_row = _cache_get(cache_key, cache_ttl)
            if cached_row:
                age = int(time.time() - cached_row['created_at'])
                cached_result = {
                    'success': bool(cached_row['success']),
                    'summary': cached_row['summary'],
                    'sourceUrl': cached_row['url'],
                    'sourceTitle': cached_row['title'],
                    'contentSize': cached_row['content_size'],
                }
                yield chunk(log=f'Cache hit (age {age}s): {(cached_row["title"] or "")}')
                yield chunk(step=0, step_status='done')
                yield chunk(step=1, step_status='done')
                yield chunk(step=2, step_status='done')
                yield chunk(step=3, step_status='done')
                yield chunk(step=4, step_status='done', log='Done (cached).')
                yield chunk(done=True, result=cached_result)
                return

        # ── Adaptive planning (LLM decides max_queries / max_store_results) ──
        if adaptive_enabled:
            try:
                adap_prompt = (
                    adaptive_prompt_tpl
                    .replace('{date}', _today)
                    .replace('{reason}', reason)
                    .replace('{query}', query)
                    .replace('{max_queries_limit}', str(max_queries))
                    .replace('{max_sources_limit}', str(max_store_results))
                )
                adap_raw = _call_llm_simple(provider, model, adap_prompt,
                                            api_key=api_key, server_url=server_url, temp=0.0, ctx=512).strip()
                # extract JSON even if model wraps it in markdown
                import re as _re2
                m = _re2.search(r'\{[^{}]+\}', adap_raw, _re2.DOTALL)
                if m:
                    adap = json.loads(m.group())
                    new_mq = max(1, min(max_queries, int(adap.get('max_queries', max_queries))))
                    new_ms = max(1, min(max_store_results, int(adap.get('max_sources', max_store_results))))
                    adap_reason = adap.get('reason', '')
                    yield chunk(log=f'Adaptive plan: sub-queries={new_mq} (was {max_queries}), sources={new_ms} (was {max_store_results}). {adap_reason}')
                    max_queries = new_mq
                    max_store_results = new_ms
                    config = dict(config, maxStoreResults=new_ms, maxQueries=new_mq)
                else:
                    yield chunk(log=f'Adaptive planning: could not parse LLM response, using defaults.')
            except Exception as e:
                yield chunk(log=f'Adaptive planning skipped: {e}')

        # Decompose (multi-query) sau optimizare simplă
        sub_queries = []
        if multi_query_enabled:
            yield chunk(log=f'Multi-query mode: decomposing into max {max_queries} sub-queries...')
            try:
                decomp_prompt = (
                    decomp_prompt_tpl
                    .replace('{date}', _today)
                    .replace('{reason}', reason)
                    .replace('{query}', query)
                    .replace('{max_queries}', str(max_queries))
                )
                decomp_resp = _call_llm_simple(provider, model, decomp_prompt,
                                               api_key=api_key, server_url=server_url, temp=0.3, ctx=ctx_size).strip()
                lines = [l.strip().strip('"\'- ') for l in decomp_resp.splitlines() if l.strip()]
                sub_queries = [l for l in lines if 3 < len(l) < 200][:max_queries]
                yield chunk(log=f'Decomposed into {len(sub_queries)} queries:')
                for i, q in enumerate(sub_queries, 1):
                    yield chunk(log=f'  Q{i}: {q}')
            except Exception as e:
                yield chunk(log=f'Decomposition failed ({e}), falling back to single query')

        if not sub_queries:
            # Optimizare simplă
            optimized = query
            try:
                opt_prompt = (
                    opt_prompt_tpl
                    .replace('{date}', _today)
                    .replace('{reason}', reason)
                    .replace('{query}', query)
                )
                optimized = _call_llm_simple(provider, model, opt_prompt,
                                             api_key=api_key, server_url=server_url, temp=0.3, ctx=ctx_size).strip()
                optimized = optimized.strip('"\'').strip()
                optimized = _re.sub(r'\b(site|inurl|intitle|filetype):\S+\s*', '', optimized).strip()
                if len(optimized) > 150 or len(optimized) < 3:
                    optimized = query
                yield chunk(log=f'Optimized query: {optimized}')
            except Exception as e:
                yield chunk(log=f'Optimization failed ({e}), using original')
            sub_queries = [optimized]

        yield chunk(step=0, step_status='done')

        # ── STEPS 1-4: Pipeline per sub-query ──
        yield chunk(step=1, step_status='active')
        yield chunk(step=2, step_status='active')
        yield chunk(step=3, step_status='active')
        yield chunk(step=4, step_status='active', log=f'Running pipeline for {len(sub_queries)} query/queries...')

        all_summaries = []   # [{summary, title, url, content_size, query}]
        seen_urls = set()

        for qi, sq in enumerate(sub_queries, 1):
            if len(sub_queries) > 1:
                yield chunk(log=f'─── Query {qi}/{len(sub_queries)}: "{sq}" ───')
            gen = _run_single_query_pipeline(sq, reason, query, config, chunk, _today, _re, live_stats=_live_stats)
            summaries = []
            for item in gen:
                if isinstance(item, bytes):
                    yield item   # log/stats chunk
                elif isinstance(item, list):
                    summaries = item  # ultimul element = lista summaries

            _live_stats['queries_done'] = qi
            yield stats_chunk()

            # Deduplicare URL între query-uri
            for s in (summaries or []):
                if s['url'] not in seen_urls:
                    seen_urls.add(s['url'])
                    s['query'] = sq
                    all_summaries.append(s)

        yield chunk(step=1, step_status='done')
        yield chunk(step=2, step_status='done')
        yield chunk(step=3, step_status='done')

        if not all_summaries:
            fallback = (
                f'--- WEB SEARCH PARTIAL RESULT ---\n'
                f'Query: {query}\n'
                f'Note: Search found results but no relevant page could be fetched.\n'
                f'--- END WEB SEARCH ---'
            )
            fallback_obj = {'success': True, 'summary': fallback, 'sourceUrl': None, 'sourceTitle': None, 'contentSize': 0, 'sourceCount': 0}
            yield chunk(step=4, step_status='done', log='No relevant pages found.')
            yield chunk(done=True, result=fallback_obj)
            return

        # ── Formatare / sinteză finală ──
        if len(all_summaries) == 1 and len(sub_queries) == 1:
            # Rezultat clasic single-source
            s = all_summaries[0]
            formatted = (
                f'--- WEB SEARCH RESULT ---\n'
                f'Query: {query}\n'
                f'Source: {s["title"]}\nURL: {s["url"]}\n\n'
                f'{s["summary"]}\n'
                f'--- END WEB SEARCH ---'
            )
        elif len(sub_queries) == 1:
            # Multiple surse, single query — format clasic multi-source
            parts = [f'--- WEB SEARCH RESULT ---\nQuery: {query}\nSources found: {len(all_summaries)}\n']
            for i, s in enumerate(all_summaries, 1):
                parts.append(f'\n--- SOURCE {i}: {s["title"]} ---\nURL: {s["url"]}\n\n{s["summary"]}\n')
            parts.append('\n--- END WEB SEARCH ---')
            formatted = ''.join(parts)
        else:
            # Multi-query: sinteză LLM
            yield chunk(log=f'Synthesizing {len(all_summaries)} results from {len(sub_queries)} queries...')
            raw_results = ''
            for i, s in enumerate(all_summaries, 1):
                raw_results += f'--- Result {i} (query: "{s["query"]}") ---\nSource: {s["title"]}\nURL: {s["url"]}\n\n{s["summary"]}\n\n'
            try:
                synth_prompt = (
                    multisynth_prompt_tpl
                    .replace('{date}', _today)
                    .replace('{reason}', reason)
                    .replace('{query}', query)
                    .replace('{results}', raw_results)
                )
                synthesis = _call_llm_simple(provider, model, synth_prompt,
                                             api_key=api_key, server_url=server_url, temp=0.3, ctx=ctx_size).strip()
                yield chunk(log=f'Synthesis generated ({len(synthesis)} chars)')
            except Exception as e:
                yield chunk(log=f'Synthesis failed ({e}), using concatenated summaries')
                synthesis = raw_results

            parts = [f'--- WEB SEARCH RESULT (Multi-Query) ---\n']
            parts.append(f'Original query: {query}\n')
            parts.append(f'Sub-queries used: {len(sub_queries)}\n')
            parts.append(f'Sources analyzed: {len(all_summaries)}\n\n')
            parts.append(synthesis)
            parts.append('\n\n--- SOURCES ---\n')
            for i, s in enumerate(all_summaries, 1):
                parts.append(f'{i}. {s["title"]}\n   {s["url"]}\n')
            parts.append('--- END WEB SEARCH ---')
            formatted = ''.join(parts)

        total_size = sum(s['content_size'] for s in all_summaries)
        primary = all_summaries[0]
        result_obj = {
            'success': True,
            'summary': formatted,
            'sourceUrl': primary['url'],
            'sourceTitle': primary['title'],
            'contentSize': total_size,
            'sourceCount': len(all_summaries),
            'subQueryCount': len(sub_queries),
            'sources': [{'url': s['url'], 'title': s['title']} for s in all_summaries],
        }
        _cache_store(cache_key, query, reason, primary['url'], primary['title'], formatted,
                     None, total_size, success=1)
        yield chunk(step=4, step_status='done', log=f'Done. {len(all_summaries)} surse, {len(sub_queries)} query/queries.')
        yield chunk(done=True, result=result_obj)

    except Exception as e:
        import traceback
        traceback.print_exc()
        yield chunk(done=True, result={'success': False, 'summary': f'Web search error: {e}'})


@app.route('/api/websearch', methods=['POST'])
def websearch():
    data = request.get_json()
    if not data:
        return jsonify({'error': 'Body JSON invalid'}), 400
    return Response(_stream_websearch(data), content_type='application/x-ndjson')


# ── WEB SEARCH BACKGROUND TASKS ───────────────────────────────────────────────
_ws_tasks = {}       # {task_id: {'status': 'running'|'done'|'error', 'chunks': [...], 'result': None, 'lock': threading.Lock(), 'event': threading.Event()}}
_ws_tasks_lock = threading.Lock()


def _ws_task_runner(task_id, data):
    """Runs web search in background thread, stores all chunks."""
    task = _ws_tasks[task_id]
    try:
        for chunk in _stream_websearch(data):
            with task['lock']:
                task['chunks'].append(chunk)
            task['event'].set()
            task['event'].clear()
        with task['lock']:
            task['status'] = 'done'
    except Exception as e:
        err_chunk = (json.dumps({'done': True, 'result': {'success': False, 'summary': f'Error: {e}'}}, ensure_ascii=False) + '\n').encode('utf-8')
        with task['lock']:
            task['chunks'].append(err_chunk)
            task['status'] = 'error'
    task['event'].set()


@app.route('/api/websearch/start', methods=['POST'])
def websearch_start():
    data = request.get_json()
    if not data:
        return jsonify({'error': 'Body JSON invalid'}), 400
    task_id = _uuid.uuid4().hex
    task = {
        'status': 'running',
        'chunks': [],
        'result': None,
        'lock': threading.Lock(),
        'event': threading.Event(),
        'started_at': time.time(),
    }
    with _ws_tasks_lock:
        # Cleanup old done tasks (keep last 10)
        done_tasks = [(tid, t) for tid, t in _ws_tasks.items() if t['status'] != 'running']
        done_tasks.sort(key=lambda x: x[1].get('started_at', 0))
        for tid, _ in done_tasks[:-10]:
            del _ws_tasks[tid]
        _ws_tasks[task_id] = task
    thread = threading.Thread(target=_ws_task_runner, args=(task_id, data), daemon=True)
    thread.start()
    return jsonify({'task_id': task_id})


@app.route('/api/websearch/stream/<task_id>', methods=['GET'])
def websearch_stream(task_id):
    with _ws_tasks_lock:
        task = _ws_tasks.get(task_id)
    if not task:
        return jsonify({'error': 'Task not found'}), 404

    def generate():
        sent = 0
        while True:
            with task['lock']:
                chunks = task['chunks']
                new_chunks = chunks[sent:]
                is_done = task['status'] != 'running'
            for chunk in new_chunks:
                yield chunk
            sent += len(new_chunks)
            if is_done and sent >= len(task['chunks']):
                break
            task['event'].wait(timeout=2.0)

    return Response(generate(), content_type='application/x-ndjson')


@app.route('/api/websearch/status', methods=['GET'])
def websearch_status_list():
    """Returns list of active/recent web search tasks."""
    with _ws_tasks_lock:
        tasks = [{
            'task_id': tid,
            'status': t['status'],
            'chunks_count': len(t['chunks']),
            'started_at': t.get('started_at', 0),
        } for tid, t in _ws_tasks.items()]
    return jsonify({'tasks': tasks})


@app.route('/export_chat', methods=['POST'])
def export_chat():
    """Export selected chat messages as PDF or DOCX."""
    if not _chat_export:
        return jsonify({'error': 'chat_export module not available'}), 500
    try:
        from datetime import datetime
        import traceback
        data = request.get_json()
        messages   = data.get('messages', [])
        fmt        = data.get('format', 'pdf')
        theme      = data.get('theme', 'dark')
        if not messages:
            return jsonify({'error': 'No messages to export'}), 400
        if fmt not in ('pdf', 'docx'):
            return jsonify({'error': 'Invalid format'}), 400
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        if fmt == 'pdf':
            buf = _chat_export.generate_pdf(messages, theme)
            return send_file(buf, mimetype='application/pdf',
                             as_attachment=True, download_name=f'chat_export_{ts}.pdf')
        else:
            buf = _chat_export.generate_docx(messages, theme)
            return send_file(buf,
                             mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                             as_attachment=True, download_name=f'chat_export_{ts}.docx')
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/api/session/register', methods=['POST'])
def session_register():
    global _controller_id
    data = request.get_json() or {}
    session_id = data.get('session_id') or _uuid.uuid4().hex
    label = data.get('label', f'Session {session_id[:8]}')
    _cleanup_sessions()
    with _sessions_lock:
        is_ctrl = len(_sessions) == 0 or _controller_id is None
        if is_ctrl:
            _controller_id = session_id
        _sessions[session_id] = {'last_seen': time.time(), 'is_controller': is_ctrl, 'label': label}
    return jsonify({'session_id': session_id, 'is_controller': is_ctrl, 'sessions': _sessions_summary()})

@app.route('/api/session/heartbeat', methods=['POST'])
def session_heartbeat():
    global _controller_id
    data = request.get_json() or {}
    sid = data.get('session_id', '')
    _cleanup_sessions()
    with _sessions_lock:
        if sid in _sessions:
            _sessions[sid]['last_seen'] = time.time()
            is_ctrl = _sessions[sid]['is_controller']
        else:
            is_ctrl = len(_sessions) == 0 or _controller_id is None
            _sessions[sid] = {'last_seen': time.time(), 'is_controller': is_ctrl, 'label': f'Session {sid[:8]}'}
            if is_ctrl:
                _controller_id = sid
    return jsonify({'is_controller': is_ctrl, 'sessions': _sessions_summary()})

@app.route('/api/session/take-control', methods=['POST'])
def session_take_control():
    global _controller_id
    data = request.get_json() or {}
    sid = data.get('session_id', '')
    _cleanup_sessions()
    with _sessions_lock:
        if sid not in _sessions:
            return jsonify({'error': 'Session not found'}), 404
        if _controller_id and _controller_id in _sessions:
            _sessions[_controller_id]['is_controller'] = False
        _controller_id = sid
        _sessions[sid]['is_controller'] = True
    return jsonify({'ok': True, 'sessions': _sessions_summary()})

@app.route('/api/session/disconnect', methods=['POST'])
def session_disconnect():
    global _controller_id
    data = request.get_json() or {}
    sid = data.get('session_id', '')
    with _sessions_lock:
        if sid in _sessions:
            del _sessions[sid]
        if _controller_id == sid:
            _controller_id = None
            if _sessions:
                first = next(iter(_sessions))
                _controller_id = first
                _sessions[first]['is_controller'] = True
    return jsonify({'ok': True})

@app.route('/api/session/list', methods=['GET'])
def session_list():
    _cleanup_sessions()
    return jsonify({'sessions': _sessions_summary(), 'controller_id': _controller_id})


if __name__ == "__main__":
    port = int(os.environ.get("FLASK_PORT", os.environ.get("PORT", 5000)))
    host = os.environ.get("HOST", "0.0.0.0")
    print(f"\n{'='*52}")
    print(f"  AI Discussion v2")
    print(f"  Providers: Ollama · OpenAI · Gemini · Anthropic")
    print(f"  http://{host}:{port}")
    print(f"{'='*52}\n")
    app.run(host=host, port=port, debug=False)
