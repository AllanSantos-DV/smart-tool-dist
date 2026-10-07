# Smart Tool

A local MCP server that gives coding agents (Claude Code, Codex) cheaper, sharper tools than their built-in search:

| Tool | What it does |
|---|---|
| `smart_search` / `smart_search_result` | Semantic + lexical search over an indexed project, results split into code, tests and docs, with the likeliest functions per chunk |
| `web_search` / `web_search_result` | Web search across several sources in parallel, in the query language and in English, with a cache shared across sessions; `depth="research"` runs a multi-round research loop |
| `web_fetch` | Reads a known URL and returns only the answer to your prompt, written by a small model; pages are cached for 24 h |
| `project_manage` | Registers and indexes projects, shows coverage, code maps, duplicated functions and index storage |

An optional hook routes the agent's native Grep/Glob/Read/Bash/WebSearch/WebFetch calls to these tools when they
are cheaper, or just tells the agent what Smart Tool would do.

**Status:** 0.9.0 beta. Windows only.

## Requirements

- Windows 10 or 11 (x64) and Node.js 18 or newer for `npx`. Python is not needed: the installer brings its own.
- About 2 GB of downloads and 4 GB of disk on first install: Python 3.12 and its packages, Node.js when no
  supported version is installed, and the browsers for pages that need JavaScript (Camoufox alone is a 1.3 GB
  download; the installer shows its progress).
- An OpenAI-compatible model API for embeddings and small chat calls (OpenAI, OpenRouter, a LiteLLM proxy, Ollama...).
  Web search works without it; smart_search falls back to lexical search.

## Install

```powershell
npx @allansantos-dev/smart-tool@latest install
```

Run the same command to update; `~/.smart-tool` is kept. It downloads [uv](https://docs.astral.sh/uv/) (checksum
verified) and a private Python 3.12 into `~/.smart-tool/runtime`, with nothing added to `PATH` or the registry, then
copies Smart Tool to `%LOCALAPPDATA%\Programs\SmartTool` with its own virtual environment,
installs the web runtime, registers a per-user Scheduled Task that starts the tray at logon and starts the daemon on
`http://127.0.0.1:8765` (another free port is used, and announced, if 8765 is taken).

State lives in `~/.smart-tool`: `config.json`, your model adapters in `adapters/` and indexes, caches and logs in
`data/`. Secrets (API keys) are encrypted with Windows DPAPI for your user.

## Set up

Open the setup screen from the tray icon ("Open settings") or at `http://127.0.0.1:8765/setup`.

1. **Model gateway**: pick an adapter and fill its fields. The built-in **OpenAI-compatible** adapter takes a base
   URL ending where the API starts, like the OpenAI SDK `base_url` (`https://api.openai.com/v1`,
   `https://openrouter.ai/api/v1`, `http://localhost:11434/v1`), and an optional API key. Saving tests `/models` first.
2. **Models**: embedding, rerank (optional; without it results use the hybrid order), the router model (small, no
   reasoning: it decides before tool calls), the index classification model and the research model. Any id the gateway
   accepts works; the catalog is shown as suggestions.
3. **Agents**: register the MCP server in Claude Code or Codex (runs their official `mcp add` command), install the
   hook and choose its behavior:
   - **Redirect** (default): the native tool is denied with the reason and the Smart Tool tool to use.
   - **Advise only**: the native tool runs; the agent receives the same advice next to the result and decides.
   - **Off**: no routing.
4. **Local models** (optional): if the mcp-memory embedding sidecar is installed on the machine, its local embedder
   can serve as a fallback (or preferred) embedding model.
5. **Web search providers** work without keys on their free public tiers; add your own key for more volume. Set your
   own contact (e-mail or URL) if you want public APIs such as Wikimedia to see it in the User-Agent.

## Writing a model adapter

An adapter is a Python file in `~/.smart-tool/adapters/`. It appears in the setup screen next to the built-in one.

```python
import json
import urllib.request

NAME = "my_gateway"            # unique id
LABEL = "My gateway"           # shown in the setup screen
FIELDS = (                     # rendered as inputs; secret values are stored with DPAPI
    {"name": "url", "label": "Address", "required": True, "placeholder": "http://10.0.0.5"},
    {"name": "token", "label": "Token", "secret": True},
)


def request(options, route, body=None, timeout=15):
    """route is OpenAI-style: models, model/info, embeddings, chat/completions, rerank. body None means GET.
    Return (parsed JSON, response headers); let urllib.error.HTTPError propagate."""
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if options.get("token"):
        headers["Authorization"] = "Bearer " + options["token"]
    req = urllib.request.Request(f"{options['url']}/v1/{route}", data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.load(response), response.headers


def verify(options):
    """Raise RuntimeError with a reason a person can act on."""
    request(options, "models", timeout=10)
```

Embeddings and chat use the OpenAI request and response formats; rerank uses the Cohere/Jina format served by
LiteLLM (`{"model", "query", "documents", "top_n"}` returning `results[].index` and `relevance_score`). A file that
fails to load is listed in the setup screen with its error and does not affect the others.

## Data and privacy

- Indexed code stays on your machine. Chunks are sent to your model gateway to compute embeddings, and small excerpts
  go to its chat models (scope, profile, routing, web_fetch answers). Use a local gateway (Ollama, LiteLLM) to keep
  everything local.
- Web queries go to the search sources listed in `NOTICE`. Their free tiers are rate limited per IP address; respect
  each service's terms of use.
- Logs and metrics are written to `~/.smart-tool/data` and never leave the machine.

## Uninstall

Turn off "Start automatically" in the setup screen, quit from the tray, remove the hook entries from
`~/.claude/settings.json` / `~/.codex/hooks.json` (the setup screen shows them), run `claude mcp remove smart-tool -s
user` / `codex mcp remove smart-tool`, then delete `%LOCALAPPDATA%\Programs\SmartTool` and `~/.smart-tool`.

## License

Apache License 2.0. See `LICENSE` and `NOTICE` for third-party components.

Page rendering is powered by [Crawl4AI](https://github.com/unclecode/crawl4ai) (Apache-2.0).
