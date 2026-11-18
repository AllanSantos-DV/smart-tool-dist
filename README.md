# Smart Tool

A local MCP server that gives coding agents (Claude Code, Codex) cheaper, sharper tools than their built-in search:

| Tool | What it does |
|---|---|
| `smart_search` / `smart_search_result` | Semantic + lexical search over an indexed project, results split into code, tests and docs, with the likeliest functions per chunk |
| `smart_scout` | Answers a question about the code with the lines that answer it: reads the candidate files inside Smart Tool and returns `path:lines` with the literal text, so the files never enter the agent's context; with a `scout_model`, a small model also writes a short answer and keeps only the lines that support it; a question about why something is as it is (why, reason, decided) also lists up to three decision notes from the notes the project keeps about its intent, as ids with one excerpt each |
| `web_search` / `web_search_result` | Web search across several sources in parallel, in the query language and in English, with a cache shared across sessions; `depth="research"` runs a multi-round research loop |
| `web_fetch` | Reads a known URL and returns only the answer to your prompt, written by a small model; pages are cached for 24 h. Pages over 60,000 characters are not cut: the model reads the parts closest to the prompt (needs an `embedding_model`) |
| `read_document` | Reads a PDF, Word, PowerPoint, Excel, HTML, CSV or EPUB file as compact text, a range at a time: type, size, page/slide/sheet/part/chapter count and estimated tokens first, then only the units asked for (`range="3-5"`), so the file never enters the agent's context whole. Opt-in: the native Read is not redirected. Scanned PDFs fail with that reason (no OCR) |
| `project_manage` | Registers and indexes projects, shows coverage, code maps, duplicated functions and index storage; `graph` with `symbol` tells what changing a function touches: callers, calls, the tests that reach it and the files that import it |

Agents that must click, type or log in also get Microsoft's [Playwright MCP](https://github.com/microsoft/playwright-mcp),
registered as the `playwright` server where this machine lets a browser be driven (see Set up, step 3).

An optional hook routes the agent's native Grep/Glob/Read/Bash/WebSearch/WebFetch calls to these tools when they
are cheaper, or just tells the agent what Smart Tool would do.

**Status:** 0.9.81 beta. Windows only. What changed in each version: [CHANGELOG.md](CHANGELOG.md).

## How it works

A coding agent normally finds code by running `grep` over the whole project and reads the web by downloading whole
pages. Both fill its context with text it does not need, and every token of it is paid for. Smart Tool is a small
program that runs on your machine and offers the agent better tools for the same jobs, through
[MCP](https://modelcontextprotocol.io), the standard way agents call outside tools.

```
Claude Code / Codex ──MCP──▶ Smart Tool daemon (127.0.0.1, one per machine)
                               ├─ smart_search ──▶ local index of your project (SQLite)
                               ├─ smart_scout ───▶ the files behind a search, read on disk (small model optional)
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

**Reading code to answer a question.** `smart_scout` takes a question in plain language. It finds the candidates the
same way as `smart_search` (or by keyword on disk while there is no index yet), reads the current lines of each one
and keeps, per file, the window with most of the question's words. The agent receives `path:lines` and those lines
verbatim. With a `scout_model` (the setup screen fills it with Claude Haiku 5.5 when the gateway has it), a small model reads those windows, writes a few lines of answer
and picks which lines to show; without an index it first picks the words and folders to scan (two calls; one with an
index). What the model writes never replaces the literal lines, and when it fails the lines come back with the error.

**Searching the web.** `web_search` asks several free search sources in parallel (DuckDuckGo, Wikipedia, Yahoo,
Exa, Google and others), in the language of the question and in English, and merges what comes back. Search APIs with a
free quota (Tavily, Firecrawl) and a real browser are tried only when those results are not enough. Answers are cached and shared by every agent
session on the machine, so the same question asked twice costs nothing the second time.

**Reading a page.** `web_fetch` takes a URL and a question. It downloads the page over plain HTTP; when the page only
shows its content with JavaScript, it opens it in a headless browser ([Moli](https://github.com/lexmount/moli)
first, Chromium as the fallback), which never reaches this machine or your local network. PDFs are read as text, page
by page, and so are Word, PowerPoint and Excel files (docx, pptx, xlsx), by content type or by the extension of the URL
when the server sends a generic binary type. A small model then reads the page and returns only the answer, with the source.
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
npx @allansantos-dev/smart-tool@0.9.81 install
```

Updates install themselves, on by default: the tray checks npm when it starts (at logon) and every 24 h, and when there
is a newer version it runs this command with that version in place of the one above and `--yes --prefer-online` before
the package name (npm asks the registry instead of its cached version list, which misses a release published minutes
ago), behind its own small update
window, with no console window (once per
version; a failed version is tried again after 24 h, with the previous version kept). The window has no buttons: it shows
the version change, the current step and the result, and closes by itself; the command's output is kept in
`~/.smart-tool/data/update.log`. You are told each time: a Windows notification from the tray (newer version,
updating, updated, update failed), and agent sessions get the same line on their first `smart_search` (and at session
start when the SessionStart report is on, shown to you too) with the link to the changes. The tray menu has "Update to
X" (or "Check for updates") on demand and an "Update automatically" switch; `SMART_TOOL_AUTO_UPDATE=off` also turns
automatic updates off, and the newer-version notice then carries the command to run. The interface (tray, notifications,
settings and projects pages, update window) is in Brazilian Portuguese or English: it follows the Windows display
language until you pick one with the globe button (PT/EN) at the top of the pages or the tray's "Idioma / Language"
submenu. Running the command by hand with a newer version also updates (add `--prefer-online` before the package name when that
version came out minutes ago); `~/.smart-tool` is kept. It downloads [uv](https://docs.astral.sh/uv/) (checksum
verified) and a private Python 3.12 into `~/.smart-tool/runtime`, with nothing added to `PATH` and, in the registry, only two per-user (HKCU) keys that make the notification buttons work, then
copies Smart Tool to `%LOCALAPPDATA%\Programs\SmartTool` with its own virtual environment,
installs the web runtime, registers a per-user Scheduled Task that starts the tray at logon and starts the daemon on
`http://127.0.0.1:8765`. A busy port is retried for a few seconds (the old daemon may still be closing) and the port
the clients already hold is kept; another free port is used, and announced in `~/.smart-tool/data/daemon.json`, only
when a program keeps the port.

State lives in `~/.smart-tool`: `config.json`, your model adapters in `adapters/` and indexes, caches and logs in
`data/`. Secrets (API keys) are encrypted with Windows DPAPI for your user.

The model calls that must return JSON (the deep-research steps, `smart_scout`, the freshness check of cached
searches, the router's tier and sufficiency decisions, the index profile and the block-pattern review) use the model's structured output instead of a forced tool call: the request carries the JSON Schema, never
`temperature`, and the answer is validated against the schema before use. A stop other than a normal one (cut at the
token limit, refusal), an empty answer, a missing field or a value outside an enum fails the call with its reason
instead of passing as an empty result. The `structured_output` object of `config.json` chooses the format:
`mode` is `auto` (default: `output_config` for the Claude models whose route, the id prefix before the first dot, is
in `output_config_routes`, which is `["vertex_ai"]` by default; `response_format`, applied on every gateway route, for
all others, Bedrock and Gemini on Vertex included), `response_format` or `output_config`; `output_config` for a model
that is not Claude or for a route outside the list is refused. `unsupported_keywords` lists the schema keywords the gateway does not apply (`maxItems`, `minLength`...); they
are left out of the request and the code that reads the answer keeps enforcing them.

## Set up

Open the setup screen from the tray icon ("Open settings") or at `/setup` on the daemon address in `~/.smart-tool/data/daemon.json` (`http://127.0.0.1:8765/setup` by default). It opens on
**Overview**: everything Smart Tool needs, checked on this machine, with what needs you first and a button that takes
you to the fix. The side menu groups the rest under Connect (Agents, Models, Web search), Behavior (Agent rules,
Redirects) and Maintenance (Setup audit, Startup).

1. **Model gateway**: pick an adapter and fill its fields. The built-in **OpenAI-compatible** adapter takes a base
   URL ending where the API starts, like the OpenAI SDK `base_url` (`https://api.openai.com/v1`,
   `https://openrouter.ai/api/v1`, `http://localhost:11434/v1`), and an optional API key. Saving tests `/models` first.
2. **Models**: embedding, rerank (optional; without it results use the hybrid order), the router model (small, no
   reasoning: web search tier order and result checks), the index classification model (also reviews block patterns), the
   scout model (the short answer of `smart_scout`), the research model and the docs model (the prose of the generated
   documentation). Any id the gateway accepts works; the catalog is
   shown as suggestions. Empty roles are filled with the recommended models the gateway offers.

   When a new version recommends other models than the ones you use, a Windows notification (**Apply now**, **Don't
   show again**) and the **New recommended models** card on this page offer them: nothing changes until you apply, the
   models you had are kept, and **Switch back to my previous models** undoes it. The **Model failures (last 24 h)** table
   groups the failed gateway calls by type (429, 503, other 5xx, timeout, connection, authentication) and role, with the count, the first and last time and one sample, and shows the calls per purpose; a model that fails half of its calls (5 or more) also raises a
   notification and a tray alert. The **Model use (last 30 days)** card adds the calls, failures and, when the gateway
   reports them, tokens and cost per purpose over every project, and flags a purpose whose calls in one hour pass twice
   its own busiest hour of the previous week (and at least 100); the flag has **Mute for 7 days** and never raises a
   notification. A failure inside a tool call also comes back as a warning line in the tool answer;
   calls made by a hook or by background work (index scope and profile, block-pattern proposals) have no answer to
   carry it, so they show only in the table, the log and the notification. The embeddings home records its calls and failures there like any other model, with no price; its
   state, and the reason when it cannot identify its model, is the **Embedder** section.
3. **Agents**: register the MCP server in Claude Code or Codex (runs their official `mcp add` command), install the
   hook and choose its behavior under **Agent rules**. A hook written by an older Smart Tool shows as outdated, with
   what an update changes; updating previews the entry and rewrites only Smart Tool's, after a backup. Every
   command hook it writes (the Codex redirect hook and the session-start hook) ends with
   `--hook-origin=smart-tool`, so its command line names Smart Tool as its owner; an entry written before that shows
   as outdated, and updating replaces it instead of adding a second one. Claude Code's redirect hooks are HTTP
   entries with no command line. In Codex, installing or updating a hook replaces Smart Tool's entry in place and
   records its approval in `~/.codex/config.toml`, as `/hooks` would, so Codex runs it with no approval step; the
   other hooks keep theirs.

   **Browser control** comes with the registration, and with every update for clients where smart-tool is registered:
   the official Playwright MCP (pinned in `web_adapters/node`) is registered as the `playwright` server, driving the
   Chromium Smart Tool installs, headless, with an isolated in-memory profile (no logins of yours) and its files in
   `~/.smart-tool/data/playwright-mcp`. Before registering, Smart Tool starts that server and opens a blank page as an
   agent would; where that fails (a corporate policy that forbids remote debugging, a missing browser) nothing is
   registered, agents keep reading pages through `web_fetch`, and the setup screen shows the reason with a retry
   button. Pages the agent opens are treated as untrusted: tools a page registers (WebMCP) are never exposed, the
   browser cannot open Smart Tool's own pages (every request it makes carries a header the daemon refuses), and
   `browser_run_code_unsafe`, which runs arbitrary JavaScript in the server process, is denied in the client
   (`permissions.deny` in `~/.claude/settings.json`, `disabled_tools` in `~/.codex/config.toml`); if it cannot be
   denied, the server is not registered. A `playwright` server you registered yourself is never touched. Remove it with
   `claude mcp remove playwright -s user` / `codex mcp remove playwright`. The registered command is a small stdio
   proxy (`lazy_mcp_proxy.py`, standard library only) that answers the handshake and the tool list from a cache the
   probe writes and starts the Node server on the first tool call, so a session that never opens a browser does not
   keep a 97 MB Node process; it ends with the session.

   **Who asked for a web search**: every provider call in `~/.smart-tool/data/web-search-health.jsonl` and every
   record of `research-metrics.jsonl` made for a `web_search` carries `requester`: `client`, `mcp_session` (first 12
   characters of the daemon's session id), `session` and `project` (the Claude Code session id and folder) and `source`
   (`hook`: the PreToolUse hook saw this call; `mcp-session`: an earlier call of the same MCP session was attributed;
   `none`: only the client name is known, which is what Codex gives, since its hook does not cover MCP tools). The
   query text is not stored there. The folder and session come from the installed hook: after an update, "Update hook"
   on the setup page adds `web_search` and `web_fetch` to its matcher. A native WebSearch or WebFetch that the hook
   redirects is attributed from the hook payload itself (`mcp_session` empty). `integration-metrics.jsonl` carries the
   same `requester` on the web events. A `deep` crawl job writes no provider record, so it has none. The hook's note
   waits up to 60 s for its MCP call; an identical query from two sessions at once can swap their notes.

   The hook behaviors:
   - **Redirect** (default): the call is not denied. Smart Tool answers it and the answer comes back as the output
     of the call the agent made: `smart_search` for a search, `web_fetch` for WebFetch, `web_search` for WebSearch
     (without `allowed_domains`/`blocked_domains`). In Claude Code a PostToolUse entry swaps the output (WebFetch and
     WebSearch still run natively, their result is replaced); a shell search whose answer is ready within 10 s runs as a
     no-op instead, so a grep with no match does not end in exit 1. In Codex the shell search is rewritten to print the
     answer from a file. Coexistence with other PreToolUse hooks on Bash: they run in parallel on the same original
     input and the host documents no order between their `updatedInput`. The Smart Tool rewrite is a constant (`true` /
     `$null`, whatever the search), carries no permission decision (a rival `deny`/`ask` still wins) and only applies to a
     content search, so a hook that rewrites other commands never meets it; if a rival rewrite of the same search wins,
     its command runs and the PostToolUse still swaps the output for the answer. Two other hooks rewriting the same command
     must be resolved in the plugin that owns them (the setup audit lists them). When the Smart Tool call fails or is late, the native call and its output stay as they are; a
     deny, with the exact call to make, is left only for a search whose query cannot be read from the command. The
     decision is a fixed rule, under a millisecond and the same every time: a content search over a project folder (`grep -r`,
     `rg`, `git grep`, the Grep tool on a folder) or over more than 20 listed files goes to `smart_search`, in Bash and
     PowerShell alike and also when it is wrapped (`bash -c`, `powershell -Command`, `timeout`, a subshell), looped
     (`for f in src/*.py`), fed by a listing (`find -exec grep`, `xargs`, `Get-ChildItem | Select-String`) or written
     as an inline python/node script that walks a folder and matches text; reading or searching known files,
     listings, Glob and commands that change files run. The setup screen counts the redirects and what each session
     did next. With **Block patterns proposed by a
     model** on (default), the scope model reviews, in the background, bulk reads the rule could not measure and
     proposes regex patterns, each checked against the logged calls; a pattern acts only after it is accepted, by you
     on the setup screen or by an agent through `project_manage action=redirect_decide`. An accepted pattern either
     redirects or, as **Tip only**, lets the call run and gives the agent the `smart_search` call that answers it.
   - **Advise only**: the native tool runs; the agent receives the same advice next to the result and decides.
   - **Off**: no routing.

   **Documentation on edit** checks the public functions an edit touches (Python docstrings, JSDoc, Javadoc, and Go
   doc comments, which must start with the declared name as the revive `exported` rule asks), in Claude Code edits and
   Codex patches. Private (`_name`, `#name`, `private`), nested and override (`@Override`,
   `@override`) functions are exempt, as in pydocstyle, eslint-plugin-jsdoc and Checkstyle, and a JSDoc above a set of
   TypeScript overloads documents the implementation:
   - **Require** (default): an edit that leaves a touched function without a docstring is blocked until it adds one.
   - **Remind**: the edit runs and the agent is told which touched functions have no docstring, and which
     documented ones changed their signature, with their callers, the tests calling them and the command that runs those tests
  (from the code graph, only when it is already cached: the hook never waits for an analysis).
   - **Off**: no checks.

   **Comments in the wrong place** keeps the documentation in the docstring, where `action=docs` reads it. Of the
   comments an edit adds or changes in a code file (not tests), it blocks four kinds: commented-out code (the ruff
   `ERA001` rule), a ticket, version or history tag (`PEDIDO 1304`, `JIRA-12`, `added in v2.1`), a loose comment block
   right above a function instead of its docstring, and a block of more than two lines inside a function body. A
   short comment that says why (`// The gateway drops the first frame after a reconnect.`) passes, as do directives
   (`# noqa`, `//go:build`, `// eslint-disable`), dividers and comments the edit did not touch. `comment_mode` in
   `config.json`: `require` (default, the edit is blocked until the comment is fixed), `remind` (the edit runs and the
   agent is told) or `off`. To turn the whole rule off set `"doc_mode": "off"` and `"comment_mode": "off"` in
   `~/.smart-tool/config.json`, or pick Off on both cards of the setup screen. A setting you already saved stays as
   saved; only a config without the key follows the new default.

   A documented function edited only in its body is not mentioned. `project_manage` with `action=docs` lists the
   public functions still without a docstring, for documenting a project that started without them.

   **Documentation scope**: `action=layers` declares the project's layers, highest first (entry points, services,
   domain, data access), each by path globs and annotations, as import-linter's layers contract, ArchUnit and
   dependency-cruiser define them. Without a rule it suggests one from folder names (`cmd`, `handler`, `service`,
   `repository`...) and annotations (`@RestController`, `@Service`, `@app.route`...), with the calls between layers;
   passing `layers` previews a rule and the same `layers` with `confirm=true` saves it to `.smart-tool/layers.json`.
   `action=docs generate=true` writes `docs/scope` in the project from the code graph, without a model: a technical page
   per folder (a page per module when the folder has more than 150 public functions) with an entry per public function
   (anchor `path::name`, signature, first doc line, layer, callers, calls and the tests that reach it) and a Mermaid flow of the folders by layer; `functional.md` with each entry point (the
   public functions of the highest layer and every `main`) and the functions its calls reach, by layer; and
   `simple.md`. `action=docs write_prose=true` writes the empty prose blocks with the docs model (Claude Haiku 5.5 by
   default): each block gets an instruction for its kind (module, entry point, plain-language entry, overview) and a
   context read from the code, and a paragraph is written only when every name, option and version it cites exists in
   the code and it is in English; a rejected one is asked again once with the reason, then reported with its fix. A call stops after about
   40 s and says how many blocks are left. The prose can also be written by hand between the markers; regenerating keeps it, reports blocks whose module
   or entry point is gone, and never overwrites a file that Smart Tool did not write. Each anchor records the signature
   it was generated from: `action=docs check=true` lists anchors whose function changed signature or is gone, links
   to missing anchors and entry paragraphs written for an older body of their function (the `code="..."` stamp of the
   block; delete it after rewriting the paragraph by hand), `action=docs validate=true` checks every claim of `docs/scope` against the code and writes `docs/scope/validation.md`, `action=docs symbol=NAME` tells where a function is documented, and the documentation hook warns
   when an edit makes a documented entry out of date. The link lives in the documents, never inside docstrings.
   The same run writes fifteen optional documents under `docs/scope/extras`, all from the graph and the git log: folder
   structure, docstring coverage per folder, module Mermaid, tests README, configuration read by the code, dependencies
   with the licenses of installed packages, decision index with an ADR template and the decision candidates found in the
   git log, a sequence per entry point, the entry points × tests matrix, hotspots (commits × lines), hidden change
   coupling (files that change together without an import), dead-code candidates, an onboarding tour of the entry
   points and a glossary of the words the code uses (which also feeds `simple.md`). Each is on by default; turn one
   off with `"docs_extras": {"modules": false}` in `config.json` (keys: `structure`, `coverage`, `modules`, `tests`,
   `configuration`, `dependencies`, `adr`, `flows`, `features_tests`, `hotspots`, `coupling`, `dead_code`, `tour`,
   `glossary`, `index`). The history pages say so on the page when the folder has no readable git history. `index`
   keeps a short index of the documentation between `smart-tool:index` markers in an existing `AGENTS.md` or
   `llms.txt`; it never creates the file and rewrites only the block.

   **Impact of a change**: `project_manage action=pr_impact [base=main]` returns a Markdown report of the change set
   against `base` (default `HEAD`): the touched functions with their callers and the tests that reach them (the ones no
   test reaches are listed apart), the documentation anchors left out of date, the decision notes and commits that name
   a touched function (a single-word name counts only as a code reference), the files that usually change in the same
   commits and are missing from the diff, the notes the edit hook would have added to the whole diff (comments, copies,
   dependency advisories, credentials) and the tests to run with their commands. It reads
   only. `project_manage action=pr_impact_comment pr=<number> base=<revision>` posts the same report as a comment on that
   pull request through the `gh` CLI, from the project folder, and returns the comment URL. It writes to GitHub with your
   own `gh` login, so it is off until you turn on "Impact report as a pull request comment" in the setup screen (Agent
   rules; `"pr_comment": "on"` in `config.json`); with it off, or with `gh` missing or not logged in, it refuses and
   posts nothing. A rerun edits the last Smart Tool comment (marked by a hidden HTML comment) instead of adding another,
   and the body shows no path of your machine.

   **Shape of the project**: `project_manage action=hotspots` ranks the code files by commits times lines (the
   riskiest to change), `dead_code` lists the functions no code calls and those only tests call (static call graph,
   candidates to read before deleting), `import_cycles` lists the groups of files that import each other, and
   `comments` lists comments in the wrong place across the code files (commented-out code, history tags, loose docs,
   long inline blocks), for code written before the edit hook existed. Each takes `limit` and writes nothing.

   **Layers on edit**: when the project has confirmed layers (`.smart-tool/layers.json`), an edit that adds a call from
   a lower layer to a function of a higher one is flagged to the agent; it never blocks. `layer_mode` in
   `config.json`: `warn` (default) or `off`, also on the setup screen.

   **Broken links on edit**: an edit of an HTML or Markdown file that adds or changes a relative link (`href`, `src`,
   a Markdown link target) to a file that does not exist is flagged to the agent, with the links it could not check
   (a path from the site root, a template expression, a `<base>` element); it never blocks. `link_mode` in
   `config.json`: `warn` (default) or `off`, also on the setup screen.
   **Recorded decisions on edit**: for the first three functions an edit touches in a code file, the hook names up to two
   notes of the project (`docs/research`, the backlog, `CLAUDE.md`, `AGENTS.md`) that name the function, as `path:line` and
   the title, so the agent reads what was decided before undoing it. A single-word function name counts only when a note
   qualifies it as `module.name` or `Class.name`, a compound name as the bare identifier; the search reads files only (no
   network, no model, no git history, 300 ms per function), says when it fails or is cut, and never blocks.
   `decision_mode` in `config.json`: `warn` (default) or `off`, also on the setup screen. The measurement behind the rule is
   in `docs/research/2026-11-18-decision-notes-keyword-quality.md`.

   **Folder and layer in a search**: `smart_search folder="<path>"` (or a list) and `layer="<name>"` restrict the
   search to a folder of the project or to one of its confirmed layers, by their path globs. The filter runs before
   the ranking, so the results are the best inside the scope, not a cut of the global list.

   **Credential check on edit**: the lines an edit adds or changes in any file are checked for credential shapes that cannot be
   anything else (GitHub, Slack, Stripe live, Anthropic and npm tokens, Google API keys, AWS access key ids and secret key lines, a
   private-key header at the start of a line); the note names the file and line, never repeats the value and never blocks, and a
   line that says it is an example is skipped.

   **Dependency check on edit**: an edit of `package.json`, `requirements*.txt`, `pyproject.toml` (`project.dependencies` and `project.optional-dependencies`; Poetry tables are not read), `go.mod`, `Cargo.toml` (crates.io registry dependencies) or `pom.xml` (Maven, plain or property-resolved versions) that adds or changes a dependency lists the OSV advisory IDs of up to 5 of them (exact version, or the lower bound of `^`, `~`, `~=` and `>=`), answered from a 6 h disk cache or one 2.5 s batch call that sends only package names and versions; when OSV cannot answer, the reminder says the check was skipped and the edit goes on. `dependency_mode` in `config.json`: `warn` (default) or `off`.

   **PDFs** inside a project are indexed as text with `[page N]` markers (20 MiB, 300 pages and 20 s per file; a scanned, password-protected or corrupt PDF is skipped with the reason), so `smart_search` returns the page that holds a passage. **Presentations and workbooks** (`.pptx`, `.xlsx`) are indexed the
   same way: text of each slide under `[slide N]` with the speaker notes under `[notes N]`, each sheet under `[sheet NAME]` with one
   tab-separated line per row (cached formula values; 100,000 rows or 1 MiB of text at most). A macro-enabled `.xlsm` is read
   like an `.xlsx` (its XML parts only, no macro runs). Older formats (`.ppt`, `.xls`, `.xlsb`, `.pptm`) are not read.

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
   the reason, when a configuration or lockfile changed or a changed code file is outside the import graph. Go is read
   by package: a file that imports a package depends on every file of it, so a changed package selects the tests of every
   package that imports it (`go test ./pkg ...`); the Rust documentation and duplicate hooks stay silent.

   **Which folder is a project**: only the folder where the agent session started (or the Git repository that holds
   it). A drive root, a personal folder (home, Desktop, Documents), a system folder, application data, a hidden tool
   folder in the home folder, `bin`, `obj`, `node_modules` and a Java home are refused with the reason and nothing is
   indexed; a subfolder of a project or of a Git repository resolves to it; when the client sends its session folder, a folder the agent only navigated to is never registered (Copilot sends none, so only the folder rules apply).
   `project_manage action=junk` lists the registered projects that break these rules; with `confirm=true` it
   unregisters them and deletes their index files (never the project's own files) and writes a manifest in
   `junk-manifests`. The daemon applies the same test at start.

   `project_manage action=deps project_root="<project>"` lists the known OSV advisories of every dependency the manifests
   of the project declare (`package.json`, `requirements*.txt`, `pyproject.toml`, `go.mod`, `Cargo.toml`, `pom.xml`), not only the ones an edit changed;
   a `package.json` with a `package-lock.json` beside it is checked at the locked version when it satisfies the range. Only package
   names and versions leave the machine, in batches of 100 per ecosystem. The setup audit runs the same check for every
   registered project (a warning per manifest with advisories; the 6 h cache keeps it from asking OSV on every run).

   `smart_search mode=dossier history=true` also searches the messages of the commits that have a body, and
   `smart_search decision_notes=true` appends the decision notes of the project (research notes, backlog, CLAUDE.md,
   AGENTS.md and commit messages) to a search answer; both are off by default, and the first read of a repository's
   history can take longer than the dossier budget, which the answer then says.

   `project_manage` with `action=setup_audit` and a `project_root` (or `python setup_audit.py --project <folder>`)
   reports what Claude Code and Codex actually load for that folder (settings layers, enabled plugins of both,
   hooks, MCP servers, skills, CLAUDE.md and AGENTS.md) and where they collide: several hooks deciding on the same tool in
   parallel, hook scripts that do not exist or hide their errors, hooks calling an MCP server that is not configured,
   MCP servers that do not start or offer the same tools twice (a gateway re-exposing a server also registered
   directly), skills sharing a name or over the description budget, an AGENTS.md only Codex reads, broken `@imports`
   and a Codex instruction chain over 32 KiB. Each finding links the documentation it relies on; nothing is edited,
   env and header values never enter the report, and what could not be checked is listed as not verified. A second
   run shows what is new and what was resolved. The daemon also runs it on its own for every registered project,
   two minutes after it starts and then every 30 minutes (MCP servers shared by several projects are contacted once
   and their result reused for 6 hours; Run now always contacts them again), and the setup screen's **Setup audit**
   panel shows each project's last report: findings by severity, what is new, what was resolved, the tokens each MCP
   server's tools cost and what was not verified. It never raises a Windows notification: a project's problems are
   told to the agent working in it.

   The agent hears about it without asking for it. The first Smart Tool call of a session in a project
   (`smart_search`, `smart_scout` or `project_manage` with that `project_root`) carries, after its answer, a short
   note for the agent only: the project, its error and warning counts, the checks that found them and the call that
   shows each one with its fix (`project_manage action=setup_audit mode=last project_root="<project>"`), with the rule
   that a fix is applied only when you ask. It comes once per session and project (a subfolder counts as its project),
   a project with a clean report or no report adds nothing, and nothing is fixed on its own. `mode=last` returns the
   stored report for the folder (or the nearest audited parent) without auditing again, and fails with what to do
   when none is stored. Optionally, **Report at session start** on the Setup audit page (Claude Code or Codex) starts
   every new session with the update notices and the redirect proposals waiting for a decision: it previews the exact
   SessionStart entry, adds it to `~/.claude/settings.json` or `~/.codex/hooks.json` only after you confirm (backup
   first), and **Turn off** removes only that entry. In Codex the same step records the approval of the new
   definition in `~/.codex/config.toml`, as `/hooks` would, so no `/hooks` step is left; an entry Codex does not trust
   shows "Not approved in Codex", and **Update** records it. It is off by default. The hook script
   (`hooks/session_audit.py`) only asks the running service and prints nothing when the service is down.
4. **Embedder**: the embeddings home (casa-de-embeddings, the embedding service of this machine, found through
   `~/.casa-de-embeddings/run/daemon.json`) is the default; the gateway's `embedding_model` and `rerank_model` are used
   only when you choose **Gateway** here, never in place of a stopped home. The home's embedder is identified by the
   `model_sha256` and `dimension` of its `/info`; a home that answers without them is reported as an error, never
   replaced silently by lexical search. Rerank goes through the home's connector to the rerank host configured in it;
   without one, results keep the hybrid order and say so. Switching the embedder re-embeds each project in the
   background, one at a time: the old index keeps answering, the folder switches to the new index when it is complete,
   and the vectors of the old model are then deleted. While the home is not running, a folder indexed with another model
   keeps answering from that index and is not updated.
5. **Web search providers** work without keys on their free public tiers; add your own key for more volume. Set your
   own contact (e-mail or URL) if you want public APIs such as Wikimedia to see it in the User-Agent. The same page lists
   who asked the recent searches (client and project folder, never the text of a search).
6. **Automatic updates** (Startup page): the switch the tray menu also has, with the installed version, the last npm check,
   the last update and a failed automatic try; no network call is made to draw it.

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
- Logs and metrics are written to `~/.smart-tool/data` and never leave the machine. That includes the model failure log
  (model, role, tool and the error text, up to 300 characters, which can carry the gateway's error body; never the request).

## Uninstall

```powershell
npx @allansantos-dev/smart-tool@0.9.81 uninstall [--target DIR] [--keep-data]
```

It asks nothing and opens no window. It stops the tray and the daemon, removes the logon task, the hook entries of Smart
Tool in `~/.claude/settings.json` and `~/.codex/hooks.json` (other hooks stay), the `smart-tool` MCP entry and the
`playwright` entry Smart Tool registered (one you registered yourself stays) in Claude Code and Codex, the program
folder (`%LOCALAPPDATA%\Programs\SmartTool`), the old copies beside it, and `~/.smart-tool` (settings, indexes, saved
keys; `--keep-data` keeps it). Your projects are never touched. The Camoufox browser cache in
`%LOCALAPPDATA%\camoufox` is left in place: other tools use it too. The exit code is 0 when everything is gone (or was
never there) and 1 when a step failed, with each failure named on stderr; running it again retries. Turning off "Start
automatically" in the setup screen also removes the two per-user registry keys of the notification buttons
(`HKCU\Software\Classes\smart-tool-action` and `HKCU\Software\Classes\AppUserModelId\SmartTool.Notifications`); the
uninstall removes them too, and they come back the next time a notification is shown.

## License

Apache License 2.0. See `LICENSE` and `NOTICE` for third-party components.

Page rendering is powered by [Moli](https://github.com/lexmount/moli) (Apache-2.0 / MIT), with [Crawl4AI](https://github.com/unclecode/crawl4ai) (Apache-2.0) as its fallback.
