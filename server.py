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
# sys.path.insert(0, '/home') (Commented out for Docker)
try:
    import chat_export as _chat_export
except ImportError:
    _chat_export = None

app = Flask(__name__)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ── WS SQLITE CACHE ───────────────────────────────────────────────────────────

WS_DB_PATH = os.path.join(BASE_DIR, 'ws_cache.db')
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
    return send_from_directory(BASE_DIR, "index.html")


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
    temp    = data.get("temperature", 0.7)
    ctx     = data.get("contextSize", 4096)
    think   = data.get("thinkEnabled", False)

    if system:
        msgs = [{"role": "system", "content": system}] + msgs

    payload = {
        "model": model,
        "messages": msgs,
        "stream": True,
        "think": think,
        "options": {"num_ctx": ctx, "temperature": temp},
    }

    try:
        with requests.post(
            f"{server}/api/chat", json=payload, stream=True, timeout=300
        ) as resp:
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
            headers=headers, json=payload, stream=True, timeout=300,
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
        with requests.post(url, json=payload, stream=True, timeout=300) as resp:
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
            headers=headers, json=payload, stream=True, timeout=300,
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

SETTINGS_FILE = os.path.join(BASE_DIR, "settings.json")


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

CHAT_FILE = os.path.join(BASE_DIR, "chat.json")


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
            text = trafilatura.extract(resp.text, include_links=False, include_tables=True)
            if text and len(text.strip()) > 100:
                return text[:max_size], 'html'
        except ImportError:
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
    "4. If the query is about recent versions or releases, add the relevant year(s).\n"
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
    server_url  = config.get('serverUrl', 'http://192.168.0.17:11434')
    max_results       = int(config.get('maxResults', 5))
    max_fetch         = int(config.get('maxFetch', 3))
    max_page          = int(config.get('maxPageSize', 30000))
    brief_thr         = int(config.get('briefThreshold', 3000))
    region            = config.get('region', 'wt-wt')
    summ_content_size  = max_page  # folosim maxPageSize — pagina completă la sumarizare
    max_store_results  = int(config.get('maxStoreResults', 1))
    overview_enabled   = bool(config.get('overviewEnabled', True))
    opt_prompt_tpl     = config.get('optPrompt', '')      or _DEFAULT_WS_OPT_PROMPT
    rank_prompt_tpl    = config.get('rankPrompt', '')     or _DEFAULT_WS_RANK_PROMPT
    rel_prompt_tpl     = config.get('relPrompt', '')      or _DEFAULT_WS_REL_PROMPT
    overview_prompt_tpl= config.get('overviewPrompt', '') or _DEFAULT_WS_OVERVIEW_PROMPT
    summ_prompt_tpl    = config.get('summPrompt', '')     or _DEFAULT_WS_SUMM_PROMPT
    cache_ttl          = int(config.get('cacheTTL', 7200))  # secunde; 0 = dezactivat
    ctx_size           = int(config.get('contextSize', 8192))

    def chunk(step=None, step_status=None, log=None, done=False, result=None):
        obj = {'done': done}
        if step is not None:
            obj['step'] = step
        if step_status:
            obj['stepStatus'] = step_status
        if log:
            obj['log'] = log
        if result is not None:
            obj['result'] = result
        return (json.dumps(obj, ensure_ascii=False) + '\n').encode('utf-8')

    try:
        # ── STEP 0: Optimize query ──
        yield chunk(step=0, step_status='active', log=f'Reason: {reason}')
        yield chunk(log=f'Original query: {query}')

        # Check SQLite cache first
        cache_key = query.lower().strip()
        if cache_ttl > 0:
            cached_row = _cache_get(cache_key, cache_ttl)
            if cached_row:
                age = int(time.time() - cached_row['created_at'])
                cached_result = json.loads(cached_row['summary']) if cached_row['summary'] and cached_row['summary'].startswith('{') else {
                    'success': bool(cached_row['success']),
                    'summary': cached_row['summary'],
                    'sourceUrl': cached_row['url'],
                    'sourceTitle': cached_row['title'],
                    'contentSize': cached_row['content_size'],
                }
                yield chunk(log=f'Cache hit (age {age}s, sursa: {(cached_row["title"] or "")[:50]}), returning cached result')
                yield chunk(step=0, step_status='done')
                yield chunk(step=1, step_status='done')
                yield chunk(step=2, step_status='done')
                yield chunk(step=3, step_status='done')
                yield chunk(step=4, step_status='done', log='Done (cached).')
                yield chunk(done=True, result=cached_result)
                return

        import datetime as _dt
        _today = _dt.date.today().isoformat()

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
            yield chunk(log=f'Query optimization failed ({e}), using original')
            optimized = query
        yield chunk(step=0, step_status='done')

        # ── STEP 1: DuckDuckGo search ──
        yield chunk(step=1, step_status='active', log=f'Searching DuckDuckGo (region={region}, max={max_results})...')
        try:
            results = _search_ddg(optimized, max_results=max_results, region=region)
        except Exception as e:
            yield chunk(log=f'Search failed: {e}')
            results = []

        if not results and optimized != query:
            yield chunk(log=f'No results, retrying with original query...')
            try:
                results = _search_ddg(query, max_results=max_results, region=region)
            except Exception as e:
                yield chunk(log=f'Retry also failed: {e}')
                results = []

        if not results:
            yield chunk(step=1, step_status='done', log='No results found.')
            yield chunk(done=True, result={'success': False, 'summary': f"No results for '{query}'."})
            return

        yield chunk(log=f'Found {len(results)} results')
        for i, r in enumerate(results, 1):
            yield chunk(log=f'  #{i}: {r["title"][:60]} — {r["url"][:70]}')
        yield chunk(step=1, step_status='done')

        # ── STEP 2: Rank results ──
        yield chunk(step=2, step_status='active', log='Ranking results by relevance...')
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
            yield chunk(log=f'Ranked order: {[i+1 for i in ranked_indices]}')
        except Exception as e:
            yield chunk(log=f'Ranking failed ({e}), using top {max_fetch}')
        yield chunk(step=2, step_status='done')

        # ── STEP 3: Fetch pages ──
        yield chunk(step=3, step_status='active', log=f'Fetching pages (target: {max_store_results} sursă/surse relevante)...')
        relevant_pages = []  # [{content, title, url}]

        for idx in ranked_indices:
            if len(relevant_pages) >= max_store_results:
                break
            r = results[idx]
            url   = r['url']
            title = r['title']

            # Verifică cache URL
            if cache_ttl > 0:
                url_cached = _cache_get_by_url(url, cache_ttl)
                if url_cached and url_cached['full_content']:
                    yield chunk(log=f'URL în cache: {title[:55]}')
                    relevant_pages.append({
                        'content': url_cached['full_content'],
                        'title': url_cached['title'] or title,
                        'url': url,
                    })
                    yield chunk(log=f'Selected din cache ({len(relevant_pages)}/{max_store_results}): {title[:55]}')
                    continue

            yield chunk(log=f'Fetching: {url[:80]}')
            try:
                content, ctype = _fetch_page_content(url, max_size=max_page)
                yield chunk(log=f'Fetched {len(content)} chars ({ctype})')
                if len(content.strip()) < 100:
                    yield chunk(log='Content too short, skipping')
                    continue

                # Overview LLM (opțional) — rezumă pagina completă înainte de relevance check
                overview_text = content[:3000]
                if overview_enabled:
                    try:
                        ov_prompt = overview_prompt_tpl.replace('{content}', content)
                        overview_text = _call_llm_simple(provider, model, ov_prompt,
                                                         api_key=api_key, server_url=server_url,
                                                         temp=0.1, ctx=ctx_size).strip()
                        yield chunk(log=f'Overview: {overview_text[:150]}')
                    except Exception as e:
                        yield chunk(log=f'Overview failed ({e}), using content sample')

                # Relevance check pe overview
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
                    yield chunk(log=f'Relevance: {rel_resp[:100]}')
                except Exception as e:
                    yield chunk(log=f'Relevance check failed ({e}), accepting')
                    is_relevant = True

                if is_relevant:
                    relevant_pages.append({'content': content, 'title': title, 'url': url})
                    yield chunk(log=f'Selected ({len(relevant_pages)}/{max_store_results}): {title[:55]}')
                else:
                    yield chunk(log='Not relevant, trying next...')
            except Exception as e:
                yield chunk(log=f'Failed to fetch {url[:60]}: {e}')
                continue

        yield chunk(step=3, step_status='done')

        if not relevant_pages:
            snippets = '\n'.join(f'- {r["title"]}: {r["snippet"]}' for r in results[:3])
            fallback = (
                f'--- WEB SEARCH PARTIAL RESULT ---\n'
                f'Query: {query}\n'
                f'Note: Search found results but no page could be fully fetched.\n\n'
                f'{snippets}\n'
                f'--- END WEB SEARCH ---'
            )
            fallback_obj = {'success': True, 'summary': fallback, 'sourceUrl': None, 'sourceTitle': None, 'contentSize': len(snippets), 'sourceCount': 0}
            _cache_store(cache_key, query, reason, None, None, fallback, None, len(snippets), success=1)
            yield chunk(step=4, step_status='done', log='No page fetched — using search snippets')
            yield chunk(done=True, result=fallback_obj)
            return

        # ── STEP 4: Summarize ──
        yield chunk(step=4, step_status='active', log=f'Generating summaries for {len(relevant_pages)} sursă/surse...')
        summaries = []

        for i, page in enumerate(relevant_pages, 1):
            yield chunk(log=f'Summarizing {i}/{len(relevant_pages)}: {page["title"][:55]}')
            summ_prompt = (
                summ_prompt_tpl
                .replace('{date}', _today)
                .replace('{reason}', reason)
                .replace('{query}', query)
                .replace('{source_title}', page['title'] or '')
                .replace('{source_url}', page['url'] or '')
                .replace('{content}', page['content'][:summ_content_size])
            )
            try:
                summary = _call_llm_simple(provider, model, summ_prompt,
                                           api_key=api_key, server_url=server_url, temp=0.3, ctx=ctx_size).strip()
                yield chunk(log=f'Summary {i} generated ({len(summary)} chars)')
            except Exception as e:
                summary = f'Found content from {page["title"]} ({len(page["content"])} chars). URL: {page["url"]}'
                yield chunk(log=f'Summary {i} failed ({e}), using fallback')
            summaries.append({'summary': summary, 'title': page['title'], 'url': page['url'], 'content_size': len(page['content'])})

        # Formatare rezultat final
        if len(summaries) == 1:
            s = summaries[0]
            pg = relevant_pages[0]
            if s['content_size'] <= brief_thr:
                formatted = (
                    f'--- WEB SEARCH RESULT ---\n'
                    f'Query: {query}\n'
                    f'Source: {s["title"]}\nURL: {s["url"]}\n\n'
                    f'{s["summary"]}\n\n'
                    f'Full content:\n{pg["content"]}\n'
                    f'--- END WEB SEARCH ---'
                )
            else:
                formatted = (
                    f'--- WEB SEARCH RESULT ---\n'
                    f'Query: {query}\n'
                    f'Source: {s["title"]}\nURL: {s["url"]}\n\n'
                    f'{s["summary"]}\n'
                    f'--- END WEB SEARCH ---'
                )
        else:
            parts = [f'--- WEB SEARCH RESULT ---\nQuery: {query}\nSources found: {len(summaries)}\n']
            for i, s in enumerate(summaries, 1):
                parts.append(f'\n--- SOURCE {i}: {s["title"]} ---\nURL: {s["url"]}\n\n{s["summary"]}\n')
            parts.append('\n--- END WEB SEARCH ---')
            formatted = ''.join(parts)

        total_size = sum(s['content_size'] for s in summaries)
        primary = summaries[0]
        result_obj = {
            'success': True,
            'summary': formatted,
            'sourceUrl': primary['url'],
            'sourceTitle': primary['title'],
            'contentSize': total_size,
            'sourceCount': len(summaries),
        }
        _cache_store(cache_key, query, reason, primary['url'], primary['title'], formatted,
                     relevant_pages[0]['content'], total_size, success=1)
        yield chunk(step=4, step_status='done', log=f'Done. {len(summaries)} sursă/surse procesate. Stocat în cache (TTL {cache_ttl}s).')
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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    host = os.environ.get("HOST", "0.0.0.0")
    print(f"\n{'='*52}")
    print(f"  AI Discussion v2")
    print(f"  Providers: Ollama · OpenAI · Gemini · Anthropic")
    print(f"  http://{host}:{port}")
    print(f"{'='*52}\n")
    app.run(host=host, port=port, debug=False)
