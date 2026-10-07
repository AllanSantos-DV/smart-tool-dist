# Changelog

## 0.9.2 - beta

- Installer reported success after a broken Camoufox download: camoufox 0.5.6 prints the error of `fetch` and exits 0.
  The installer now catches that error and fails the step (retried 3 times), every request of the download has a
  60 s read timeout (a stalled download used to wait until the 3-minute idle limit), and the final check requires the
  Camoufox and Chromium executables on disk, so an incomplete browser can never end in "Smart Tool ready".
- Setup screen: text, model and adapter fields were unstyled boxes (inputs without a type), selects and model fields
  now look like comboboxes; the hook change opens inside the client's card in a scrollable block with Confirm and
  Cancel instead of overflowing the page; "Save behavior" works without registering the MCP first; web search keys
  are set one provider at a time from a provider selector instead of a stack of key fields.
- Product page: the display font was a family that Google Fonts does not serve (titles fell back to Arial Narrow);
  it now uses Big Shoulders Display. The page shows the current version (a test keeps it in sync with version.py),
  links to the release notes and says what the installer does on failures and older installations. A failed Pages
  deploy is retried once, the run fails unless the live page shows the version, and every release republishes it.

## 0.9.1 - beta

- Installer on a regular Windows code page (cp1252): the steps' output was garbled or crashed the step; every child
  process now writes UTF-8.
- Camoufox download (1.3 GB) looked stuck: its progress is shown every 50 MB, a stalled connection fails after 60 s and
  is retried (3 attempts), and a step is stopped only after a stretch without any output instead of a fixed 10 minutes.
  Steps run with closed stdin, so an unexpected prompt fails at once instead of waiting unseen.
- An older installation in another folder (found through its running tray or daemon, logon task, Startup script or
  agent hooks; only folders made by the installer) is stopped, its hooks are pointed at the new installation and its
  program folder is removed. Its state folders are left in place.
- Requirements now state about 2 GB of downloads and 4 GB of disk.

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
