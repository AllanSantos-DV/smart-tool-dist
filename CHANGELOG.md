# Changelog

## 0.9.0 - beta

First release for beta testers. Windows only.

- `smart_search`: semantic + lexical search (Weaviate-style relative score fusion), results split into code, tests and
  docs, likeliest functions per chunk, per-project language profile; asks the calling agent to define the scope when no
  scope model is configured.
- `web_search`: parallel sources (DuckDuckGo, Yahoo, Wikipedia, Exa, Parallel, Keenable, Google and Startpage through a
  resident browser), bilingual queries, cache shared across sessions with volatility checks, research mode.
- `web_fetch`: reads a known URL and returns only the answer to the prompt, 24 h page cache, browser rendering for
  JavaScript pages.
- Model gateway through adapters: built-in OpenAI-compatible adapter; user adapters as Python files in
  `~/.smart-tool/adapters`.
- Optional local embedder through the mcp-memory sidecar when the gateway has no embedding model.
- PreToolUse hook for Claude Code (HTTP to the daemon) and Codex (thin command), with Redirect, Advise only and Off modes.
- Setup screen: model gateway, models, agents (MCP registration, hook install, hook behavior), web provider keys,
  contact for public APIs, local models, start at logon.
- The daemon owns its port: falls back to a free port and rewrites the smart-tool URLs the clients keep.
- Install and update with `npx @allansantos-dev/smart-tool@latest install`: the package brings uv (checksum verified)
  and a private Python 3.12 in `~/.smart-tool/runtime`, so only Node.js is required. The tray offers the update command
  when npm has a newer version. Product page on GitHub Pages.
