# Smart Tool

A local MCP server that gives coding agents (Claude Code, Codex) cheaper, sharper tools than their built-in search:

| Tool | What it does |
|---|---|
| `smart_search` / `smart_search_result` | Semantic + lexical search over an indexed project, results split into code, tests and docs, with the likeliest functions per chunk |
| `web_search` / `web_search_result` | Web search across several sources in parallel, in the query language and in English, with a cache shared across sessions; `depth="research"` runs a multi-round research loop |
| `web_fetch` | Reads a known URL and returns only the answer to your prompt, written by a small model; pages are cached for 24 h. Pages over 60,000 characters are not cut: the model reads the parts closest to the prompt (needs an `embedding_model`) |
| `project_manage` | Registers and indexes projects, shows coverage, code maps, duplicated functions and index storage; `graph` with `symbol` tells what changing a function touches: callers, calls, the tests that reach it and the files that import it |

Agents that must click, type or log in also get Microsoft's [Playwright MCP](https://github.com/microsoft/playwright-mcp),
registered as the `playwright` server where this machine lets a browser be driven (see Set up, step 3).

An optional hook routes the agent's native Grep/Glob/Read/Bash/WebSearch/WebFetch calls to these tools when they
are cheaper, or just tells the agent what Smart Tool would do.

**Status:** 0.9.11 beta. Windows only. What changed in each version: [CHANGELOG.md](CHANGELOG.md).

## How it works

A coding agent normally finds code by running `grep` over the whole project and reads the web by downloading whole
pages. Both fill its context with text it does not need, and every token of it is paid for. Smart Tool is a small
program that runs on your machine and offers the agent better tools for the same jobs, through
[MCP](https://modelcontextprotocol.io), the standard way agents call outside tools.

```
Claude Code / Codex ──MCP──▶ Smart Tool daemon (127.0.0.1, one per machine)
                               ├─ smart_search ──▶ local index of your project (SQLite)
                               ├─ web_search ───▶ several search engines at once, shared cache
                               ├─ web_fetch ────▶ the page, read by a small model
                               └─ project_manage
        your model gateway (OpenAI, OpenRouter, Ollama...) ◀── embeddings and small chat calls
```

**Searching code.** When a project is registered, a small model looks at its folder structure once and decides what
is worth indexing (source, tests, docs; not build output or dependencies). Each file is cut into chunks of up to 200
lines, and each chunk gets an embedding, a list of numbers that captures its meaning. A search runs two ways at
once, by meaning (embeddings) and by exact words (SQLite full-text search), merges the two lists and, if you set a
rerank model, reorders the best candidates. The agent gets back a few chunks split into code, tests and docs, with
the functions most likely to matter, instead of every line that contains a word. The index follows the project:
only files whose content changed are processed again, and each Git branch keeps its own view.

**Searching the web.** `web_search` asks several free search sources in parallel (DuckDuckGo, Wikipedia, Yahoo,
Exa, Google and others), in the language of the question and in English, and merges what comes back. Search APIs with a
free quota (Tavily, Firecrawl) and a real browser are tried only when those results are not enough. Answers are cached and shared by every agent
session on the machine, so the same question asked twice costs nothing the second time.

**Reading a page.** `web_fetch` takes a URL and a question. It downloads the page over plain HTTP; when the page only
shows its content with JavaScript, it opens it in a headless browser ([Moli](https://github.com/lexmount/moli)
first, Chromium as the fallback). A small model then reads the page and returns only the answer, with the source.
The agent receives a paragraph instead of the whole page.

**Steering the agent (optional hook).** Agents keep reaching for their built-in tools out of habit. The hook sees
each call before it runs: a content search over a project folder is stopped and the agent is told the exact Smart
Tool call to make instead; reading a known file, or anything else, runs as usual. The same hook can remind the agent
to document functions it edits and warn when it writes a function that already exists in the project.

**What stays local.** The daemon, the indexes, the caches and the logs live on your machine. Text leaves it only to
reach the model gateway you chose (for embeddings and the small model calls) and the web search sources. With a local
gateway such as Ollama, nothing about your code leaves the machine.

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

Updates install themselves: when the tray starts (at logon) and npm has a newer version, it runs this same command in
a visible console, once per version; the tray menu has "Update to X" (or "Check for updates") on demand and an
"Update automatically" switch. Running the command by hand also updates; `~/.smart-tool` is kept. It downloads [uv](https://docs.astral.sh/uv/) (checksum
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
   reasoning: web search tier order and result checks), the index classification model (also reviews block patterns) and the research model. Any id the gateway
   accepts works; the catalog is shown as suggestions.
3. **Agents**: register the MCP server in Claude Code or Codex (runs their official `mcp add` command), install the
   hook and choose its behavior.

   **Browser control** comes with the registration, and with every update for clients where smart-tool is registered:
   the official Playwright MCP (pinned in `web_adapters/node`) is registered as the `playwright` server, driving the
   Chromium Smart Tool installs, headless, with an isolated in-memory profile (no logins of yours) and its files in
   `~/.smart-tool/data/playwright-mcp`. Before registering, Smart Tool starts that server and opens a blank page as an
   agent would; where that fails (a corporate policy that forbids remote debugging, a missing browser) nothing is
   registered, agents keep reading pages through `web_fetch`, and the setup screen shows the reason with a retry
   button. A `playwright` server you registered yourself is never touched. Remove it with
   `claude mcp remove playwright -s user` / `codex mcp remove playwright`.

   The hook behaviors:
   - **Redirect** (default): the native tool is denied with the reason and the Smart Tool tool to use. The decision
     is a fixed rule, under a millisecond and the same every time: a content search over a project folder (`grep -r`,
     `rg`, `git grep`, the Grep tool on a folder) or over more than 20 listed files goes to `smart_search`; reading or
     searching known files, listings, Glob and commands that change files run. With **Block patterns proposed by a
     model** on (default), the scope model reviews, in the background, bulk reads the rule could not measure and
     proposes regex patterns, each checked against the logged calls; a pattern blocks only after you accept it.
   - **Advise only**: the native tool runs; the agent receives the same advice next to the result and decides.
   - **Off**: no routing.

   **Documentation on edit** checks the public functions an edit touches (Python docstrings, JSDoc, Javadoc), in
   Claude Code edits and Codex patches. Private (`_name`, `#name`, `private`), nested and override (`@Override`,
   `@override`) functions are exempt, as in pydocstyle, eslint-plugin-jsdoc and Checkstyle, and a JSDoc above a set of
   TypeScript overloads documents the implementation:
   - **Remind** (default): the edit runs and the agent is told which touched functions have no docstring, and which
     documented ones changed their signature, with their callers.
   - **Require**: an edit that leaves a touched function without a docstring is blocked until it adds one.
   - **Off**: no checks.

   A documented function edited only in its body is not mentioned. `project_manage` with `action=docs` lists the
   public functions still without a docstring, for documenting a project that started without them.

   **Block patterns waiting for you** are shown in one approval panel: 8 per page, the ones that would have blocked
   the most logged calls first, a filter over pattern, reason and example, and bulk accept or reject of the selected
   rows; each row opens to its reason and example.

   **Duplicate functions on edit** compares the functions an edit writes with the indexed code. **Warn** (default)
   tells the agent when a written function has the same body as an existing one (comments, spacing and docstrings
   ignored) or a near-identical one (only local names, strings and numbers changed), with the path and lines of the
   original to reuse; it never blocks. Editing a function in place, overloads, tests, generated files and small
   functions (under 5 lines or 50 tokens) are not checked. **Off** disables it.

   After editing, `project_manage` with `action=affected_tests` lists the tests to run: every test file that imports a
   changed file, directly or through other files (git diff against `base`, default `HEAD`, untracked files included),
   plus changed tests and tests using a pytest fixture whose `conftest.py` imports the changed code, ordered with the
   likeliest failures first, and the command to run them. Changed files are read from disk, so new files and imports
   count before the project is reindexed. `run_all` is true, with
   the reason, when a configuration or lockfile changed or a changed code file is outside the import graph.
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

Page rendering is powered by [Moli](https://github.com/lexmount/moli) (Apache-2.0 / MIT), with [Crawl4AI](https://github.com/unclecode/crawl4ai) (Apache-2.0) as its fallback.
