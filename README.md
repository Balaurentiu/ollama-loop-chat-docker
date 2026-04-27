# 🤖 Ollama Loop Chat

A multi-agent AI debate platform with web search, built with Flask and a modern Web UI. Two AI agents engage in structured discussions on any topic, supported by real-time web search, persistent sessions, and full export capabilities.

---

## ✨ Features

### Multi-Agent Debate
- **Agent A vs Agent B** — two independent LLM agents debate any topic you provide
- **Synthesis Agent** — a third agent summarizes the discussion on demand
- **Human-in-Loop** — optionally pause after each agent turn to inject your own messages
- **Auto-stop** — agents signal discussion completion with `[STOP_DISCUSSION]`
- **Context tracking** — live token usage bar with auto-summarize warning at 90%

### Web Search Integration
- Real-time **DuckDuckGo** search with query optimization
- Full pipeline: query optimization → search → ranking → page fetch → overview → relevance check → summarization
- **SQLite persistent cache** with configurable TTL (no repeated fetches)
- Multi-source aggregation — up to N relevant pages summarized separately
- All 5 pipeline prompts editable from the Web UI with fullscreen editor

### Multi-Provider LLM Support
| Provider | Models |
|---|---|
| **Ollama** | Any locally running model |
| **OpenAI** | GPT-4o, GPT-4 Turbo, GPT-3.5 Turbo, etc. |
| **Google Gemini** | Gemini 2.5 Pro/Flash, Gemini 1.5 Pro/Flash |
| **Anthropic** | Claude Opus/Sonnet/Haiku (3.5, 4, 4.5, 4.6) |

Each agent (A, B, Synthesis, Web Search) can use a **different provider and model**.

### Session & Export
- **Auto-save** — discussion persists to disk after every turn (restored on refresh)
- **Export** — save/load full sessions as `.zip` (settings + discussion)
- **Export formats** — PDF, DOCX, Markdown, plain text
- **Clear** — one-click conversation reset

### UI
- Dark theme with customizable agent colors
- Mermaid diagram rendering with auto-wrap for long labels
- KaTeX math rendering
- Markdown with syntax highlighting
- Streaming responses
- Fullscreen prompt editor for all LLM prompts

---

## 🚀 Quick Start

### Prerequisites
- [Docker](https://docs.docker.com/get-docker/) and Docker Compose
- [Ollama](https://ollama.com) running locally (or API keys for cloud providers)

### Run with Docker

```bash
git clone https://github.com/Balaurentiu/ollama-loop-chat-docker.git
cd ollama-loop-chat-docker
docker compose up -d
```

Open **http://localhost:5050** in your browser.

### Run without Docker

```bash
git clone https://github.com/Balaurentiu/ollama-loop-chat-docker.git
cd ollama-loop-chat-docker
pip install -r requirements.txt
python3 server.py
```

Open **http://localhost:5000** in your browser.

---

## ⚙️ Configuration

All settings are configurable from the **Settings** panel in the Web UI. Click the ⚙ button in the toolbar.

### Agent Settings (per agent: A, B, Synthesis)
| Field | Description |
|---|---|
| Provider | ollama / openai / gemini / anthropic |
| Server URL | Ollama endpoint (e.g. `http://localhost:11434`) |
| API Key | Required for cloud providers |
| Model | Model name or ID |
| Temperature | Creativity level (0.0 – 1.0) |
| Context Size | Max tokens in context window |
| System Prompt | Agent personality and instructions |
| Thinking Mode | Enable extended reasoning (for supported models) |

### Discussion Settings
| Field | Description |
|---|---|
| Max Steps | Maximum debate turns (0 = unlimited) |
| Start Agent | Which agent speaks first (A or B) |
| Auto-summarize | Trigger synthesis agent at 90% context |
| Human-in-Loop | Pause for user input after each agent turn |

### Web Search Settings
| Field | Description |
|---|---|
| Enabled | Toggle web search on/off |
| Max Results | DDG results to consider |
| Max Fetch | Pages to download and analyze |
| Max Store Results | Number of relevant sources to include |
| Max Page Size | Characters extracted per page |
| Cache TTL | Seconds to keep cached results |
| Region | Search region (e.g. `ro-ro`, `us-en`) |
| Overview | Generate page summary before relevance check |

### Editable Prompts (Web Search Pipeline)
- **Query Optimization** — refines the user's query for better search results
- **Ranking** — ranks DDG results by relevance
- **Relevance Check** — decides if a page is worth including
- **Overview** — generates a structured page summary before relevance check
- **Result Summarization** — extracts relevant information from each page

---

## 🔍 Web Search Pipeline

```
User query
   │
   ▼
[1] Query Optimization LLM
   │  Refines query + extracts search reason
   ▼
[2] DuckDuckGo Search
   │  Returns up to N results
   ▼
[3] Ranking LLM
   │  Scores and selects top candidates
   ▼
[4] Page Fetch + Overview LLM
   │  Downloads full page, generates bullet summary
   ▼
[5] Relevance Check LLM
   │  Accepts/rejects based on overview
   ▼
[6] Summarization LLM (per source)
   │  Extracts relevant facts from full page
   ▼
Final result injected into agent context
```

Results are **cached in SQLite** (`ws_cache.db`) with configurable TTL to avoid redundant fetches across sessions.

---

## 📁 Project Structure

```
ollama-loop-chat-docker/
├── server.py           # Flask backend, LLM routing, web search pipeline
├── index.html          # Single-page Web UI (vanilla JS)
├── requirements.txt    # Python dependencies
├── Dockerfile          # Container definition
├── docker-compose.yml  # Docker Compose config (port 5050)
├── settings.json       # Persisted UI settings (volume-mounted)
├── chat.json           # Auto-saved discussion (volume-mounted)
└── chat_export.py      # Export utility script
```

---

## 🐳 Docker Details

The container exposes port `5000` internally, mapped to `5050` on the host.

Two files are volume-mounted for persistence:
- `settings.json` — survives container rebuilds
- `chat.json` — discussion auto-saved after every turn

To change the host port, edit `docker-compose.yml`:
```yaml
ports:
  - "YOUR_PORT:5000"
```

---

## 📦 Dependencies

| Package | Purpose |
|---|---|
| `flask` | Web server |
| `requests` | HTTP client for LLM APIs and page fetching |
| `ddgs` | DuckDuckGo search |
| `trafilatura` | Web page text extraction |
| `beautifulsoup4` | HTML parsing fallback |
| `markdown` | Markdown to HTML conversion |
| `xhtml2pdf` | PDF export |
| `python-docx` | DOCX export |

---

## 🛠 Development

To run in development mode with live reload:

```bash
pip install -r requirements.txt
FLASK_ENV=development python3 server.py
```

The Web UI is a single `index.html` file with no build step required. Edit and refresh.

---

## 📄 License

MIT License — free to use, modify and distribute.
