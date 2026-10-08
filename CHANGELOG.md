# Changelog

## 0.9.4 - beta

Found by using 0.9.3 on its own repository right after installing it.

- Impact (`graph` with `symbol`), docstring coverage (`docs`), `affected_tests` and the duplicate warning on edit now see
  every file the index does not reflect yet: working-tree changes, indexed files changed on disk and files committed
  after the last indexing. In the default `on_search` mode the index only updates on a search, so a function added
  earlier in the session was "not found" by `graph`, and a copy of it was not flagged by the duplicate warning. Only
  the current view gets these files; a pinned or older view is read as indexed.
- The code graph analysis is saved next to the index (`graph-cache/`, about 0.3 MB per view) and reloaded after a
  daemon restart or when a project left the in-memory cache: 0.03-0.12 s instead of 7-11 s of analysis on remeda
  and gson. An indexing job warms the graph when it ends. The duplicate warning, which never waits for an analysis,
  used to skip the first edit after a restart or a reindex; it now checks it.
- Hook routing decides more Bash commands without the router model: a search verb after a single `|` filters the
  output of the command before it (`python x.py | tail -5`, `npm test | grep FAIL`) and no longer sends the call to
  the model, unless that command reads code or pages (`git grep`, `git ls-files`, `curl`, `gh api`, ...). Replayed on
  2,489 real Bash decisions that went to the model (median 1.5 s, 7.8% redirected): 438 become mechanical, 13 minutes
  of waiting saved in 58 hours, 1 of 155 redirects lost. Search verbs after `do`/`then`/`else` and inside a quoted
  `bash -c "..."` / `powershell -Command "..."` are now recognized.
- `affected_tests` no longer asks for the whole suite when only CI configuration or repository metadata changed
  (`.github/`, `.gitlab-ci.yml`, `Jenkinsfile`, `.gitignore`, `.gitattributes`, `LICENSE`, ...).
- The docstring reminder on edit only applies to code files of registered projects: test files (whose test functions
  normally have no docstring) and files outside any project are left out, as in the `docs` coverage.

## 0.9.3 - beta

- Camoufox download (1.3 GB) on networks that cut long transfers (seen stopping at 500-530 MB on every attempt): it is
  now fetched in 32 MB Range requests, each on a new connection, resuming from the last byte received after a dropped
  or stalled connection, and gives up only after 6 attempts in a row without a new byte. camoufox still verifies the
  sha256 published for the release before extracting. The downloader uses the standard library, which on Windows
  trusts the system certificate store (including a corporate TLS inspection CA) and honors HTTPS_PROXY.
- `project_manage graph` with `symbol` (function, `Class.method`, class, or `path::name` when the name repeats): where
  it is defined, its callers with line, what it calls, the tests that reach it through static calls (`depth` hops,
  default 3) and the files importing its module, so an agent knows what to update before an edit. With `file_path`, the graph caps its lists by `limit` and drops display-only data.
- Documentation on edit (setup, Agents): the hook checks the functions a Claude Code Edit/MultiEdit/Write or a Codex
  apply_patch touches. Remind (default) tells the agent which ones have no docstring and which documented ones changed
  their signature, with callers; Require blocks an edit that leaves a touched function without a
  docstring; Off disables it. Body-only edits of documented functions stay silent. The hook matcher now includes the
  edit tools: reinstall the hook from the setup screen (it shows as outdated).
- Documentation on edit asks only for public functions: private (`_name`, `#name`, `private`), nested, callback and
  override (`@Override`, `@override`) functions are exempt, following pydocstyle, eslint-plugin-jsdoc and Checkstyle
  defaults, and a JSDoc above TypeScript overload signatures documents the implementation. On remeda, gson, express
  and click the former rule asked for docstrings on every overload implementation, callback and `@Override` method.
  TypeScript, JavaScript and Java edits were never checked on Windows (the file was looked up by its absolute path in
  the analyzer output, which normalizes it); they are now.
- Duplicate functions on edit (setup, Agents; Warn by default, Off): when an edit writes a function whose body is
  identical (comments, spacing, docstrings ignored) or near-identical (only local names, strings and numbers changed)
  to an indexed one, the agent is told where the original is so it can reuse it; the edit is never blocked. Measured
  on click, express, remeda and gson: every exact and locally renamed copy found, no warning when each real function
  is written as it is.
- `project_manage action=affected_tests`: tests to run for the uncommitted changes (or against `base`): every test
  file importing a changed file, directly or transitively, plus changed tests, likeliest failures first (tests calling
  a touched function, test files named after the function or module, import distance), with the command per runner
  (pytest, unittest, vitest, jest, mocha, node --test, Maven, Gradle). `run_all` with the reason when configuration,
  lockfiles or code outside the import graph changed. In fault-injection runs (30 faults each), selection by imports
  caught every failing test file in express and all but timing-flaky ones in remeda; static calls alone, as the
  `graph` impact lists them, found 2 to 37%.
  Changed files are read from the working tree, so a new file or a new import counts before the project is reindexed (in on_search mode the index only updates on a search), and a test that takes a pytest fixture from a
  conftest.py depends on what that conftest imports (a change in click's `testing.py` selected 4 of the 20 failing
  test files; now all 20).
- Near-duplicate detection (`action=duplicates` and the edit hook) keeps keywords, called names, attributes and types
  and abstracts only local names, strings and numbers, compares functions of the same language only and skips
  overloads and language idioms: parallel functions such as `get_text_stdin`/`get_text_stdout` or
  `rotateLeft`/`rotateRight` are no longer reported (near pairs fell from 5-14% of functions to 0-1.6%).
- Code graph: `require('../')` resolved to a wrong absolute path, and workspace packages imported by name
  (`import { x } from "my-lib"` in a monorepo) were treated as external; both now resolve to the package source. Vitest
  type tests (`*.test-d.ts`, `*.spec-d.ts`) are classified as tests. The TypeScript and Java analyzers retry with a
  4 GB heap when 1 GB is not enough (larger projects ended with no symbols).
- `web_fetch` on pages over 60,000 characters (14.6% of cached pages, 10.4% of real calls) no longer keeps only the
  start: the whole page is cached (up to 1,000,000 characters) and the model reads the 2,000-character pieces most
  similar to the prompt, up to 15,000 characters, in page order. Measured on 18 real pages and 41 questions: answers
  about text after the old cut 0/28 → 13/28. Pieces are embedded once per page and embedding model (first read of a
  75-256k page adds 2.6-6.8 s, later questions about 1 s); without a working `embedding_model` such a page fails
  with the reason instead of being cut silently. Pages up to 60,000 characters are read whole, as before (selection
  there tied on quality and added latency).
- Hook metrics: concurrent hook calls from Claude Code and Codex could be logged with each other's client name.
- Web search health log tells a provider 429 (`rate_limited`), a captcha and a self-imposed quota pause (`paused`, not
  counted against the provider) apart from an ordinary failure.
- `project_manage action=docs`: docstring coverage of the indexed code (public functions without a docstring per file,
  coverage percent; tests only with `include_tests`), for documenting a project that started without them.
- Impact results (`graph` with `symbol`) carry the first docstring line of the definition, callers, calls and tests.
- `project_manage` for agents: every action takes `project_root` instead of `project_id`; `list` returns one short line
  per project (it returned the full jobs, scope and preview of every project, about 1 MB with 14 projects, more than
  an agent can read; the projects screen still gets the full list); an index job's result is no longer repeated as
  `stats`.

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
