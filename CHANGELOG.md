# Changelog

## 0.9.11 - beta

- Documentation only, no change to the program. The README opens with "How it works": what each tool does behind the
  scenes, in plain words, for someone who has never used it (the index and the hybrid search, the web search sources,
  how `web_fetch` reads a page, the hook, what stays on the machine). Its status line said 0.9.0; a test now fails
  when the README status or the top of this changelog differs from `version.py`, as one already did for the product
  page. The product page credits Moli as the page renderer (Crawl4AI is its fallback), lists Moli among the browsers
  the installer downloads, links to "How it works" and has a favicon.

## 0.9.10 - beta

- Browser control for agents, on by default: registering smart-tool in Claude Code or Codex (and every update, for
  clients where it is registered) also registers Microsoft's Playwright MCP (`@playwright/mcp` 0.0.83, pinned in
  `web_adapters/node`) as the `playwright` server, driving the Chromium Smart Tool already installs, headless, with an
  isolated in-memory profile. A probe first starts the server and opens a blank page the way an agent would; where
  that fails (a corporate policy against remote debugging, a missing browser, the client CLI refusing) nothing is
  registered, agents keep the read-only browsers of `web_fetch` and `web_search`, and the setup screen shows the
  reason and a retry button instead of an error. A registration Smart Tool made is refreshed on update (Node and
  Chromium paths change) and removed when the probe stops passing; a `playwright` server you registered is kept.
- Block pattern proposals no longer stack one block per proposal (21 pending ones made the setup screen scroll
  forever): one approval panel shows 8 per page, ordered by how many logged calls each would have blocked, with a
  filter, a select-page toggle and bulk accept/reject in a single write (all ids or none); reason and example open on
  demand, and blocking patterns sit in a collapsed list.

## 0.9.9 - beta

- `web_fetch` renders JavaScript pages with [Moli](https://github.com/lexmount/moli) first (a headless browser built
  for agents: layout and paint only when a page needs them; 43 MB download, SHA-256 checked) and starts Chromium
  through Crawl4AI only when Moli fails or comes back thin. On 21 pages HTTP could not read: Moli 1.6 s and 90 MB
  peak (median) against 4.2 s and 581 MB; Moli read 14, Chromium 16, both together 17 (only Chromium read YouTube,
  Google Maps and Airbnb). With several agent sessions reading at once, measured on N simultaneous rendered reads:
  aggregate peak memory 448 → 35 MB (1), 1,788 → 194 MB (3), 2,901 → 634 MB (5), 4.4-4.7 GB → 1.0-1.4 GB (10), and
  the median read time halved (10 at once: 5.0 s → 2.5 s). Pages only Chromium reads now take Moli's 1-4 s more.
  The `web_fetch` metric records which engine read the page and Moli's error when it fell back.

## 0.9.8 - beta

- An update could keep running the previous code: npm dates every file in the package 1985-10-26, the installer kept
  that date when copying, and Python reuses a compiled `.pyc` when the source's date and size match, so a file changed
  without changing size (a version bump, a one-character fix) went on running the old bytecode. Reproduced on a real
  reinstall: `version.py` said 0.9.6 and the daemon served 0.9.8. Copied and restored files now get the current time,
  and the program's `__pycache__` folders are cleared after copying and after a rollback (two copies within the same
  second keep the same date). The update to this version already clears any stale bytecode left by earlier ones.
  Reported by the agent-launcher session, which had the same bug.

## 0.9.7 - beta

The hook decides code searches with a fixed rule instead of the router model.

- Deterministic redirect (`redirect_rule.py`): only the part of a Bash command that searches content is measured, with
  its own paths (`cd` carried, `$VAR` set in the command or the environment, `~` and globs expanded). A content search
  over a project folder (`grep -r`, `rg`, `git grep`, `git -C dir grep`, the Grep tool on a folder) or over more than
  20 listed files goes to `smart_search`; reading or searching known files, listings, Glob, commands that change files
  and paths it cannot resolve run. Replayed on 2,789 real decisions of the router model: the model redirected 315,
  the rule 463 (134 shared); in a sample of 40 redirected by the rule alone, 40 were content searches over a folder or
  many files; in a sample of 40 redirected by the model alone, ~36 were wrong. A decision takes 0.2 ms (median)
  instead of 1.5 s: the router model cost 119 min of agent waiting in 84 h. The same call always gets the same answer.
- Block patterns proposed by a model (`pattern_proposals`, on by default): bulk reads the rule let run (globs, xargs,
  -exec, loops, scripts walking folders) and paths it could not resolve are reviewed in the background by the scope
  model (else the router model); the agent never waits. A proposed regex is checked (it compiles, matches the call,
  does not match calls allowed on purpose, blocks at most 25% of the logged calls of that tool) and waits in the setup
  screen with how many logged calls it would have blocked; it redirects only after you accept it. With gpt-4o-mini, 17
  of 20 reviews proposed a pattern and none was usable; with gpt-5-mini, 2 of 20, one a real gap (grep over
  `$(git ls-files)` held in a variable).
- A new branch or worktree starts from the project's latest view: the scope is inherited while the folder structure
  and root docs still match (no scope or profile model call), and the index starts as a copy that only reprocesses the
  files whose content differs. Measured on a branch at the same commit: the first search took 52 s (scope 11 s,
  profile 20.5 s, rebuilding the index 17 s), seen in real use as the p90 of 64 s over 219 searches.
- `smart_search_result` waits up to 15 s for the job before answering pending: answering at once had an agent poll the
  same job 7 times in 16 s and give up on Smart Tool.
- Code redirects no longer need a router model: `smart_search` answers with the lexical index when no embedding model
  is configured.

## 0.9.6 - beta

Reported by the claude-code-boss session validating 0.9.5.

- `project_manage action=duplicates` reads the files the index does not reflect yet (working tree, files changed on
  disk, commits after the last indexing) and says how many it read: it kept reporting a function removed in a commit
  made after the last indexing, with no sign that the index was behind.
- In `graph` impact results, a test or caller that is an anonymous callback registered with `test('title', fn)`,
  `it()` or `describe()` shows its title (`test "embedder (Q47): ..."`) instead of `callback`.
- `graph` with `symbol` accepts a partial path before `::` (`brain-embedder.js::loadConfig`), matching the files whose
  path ends with it; it needed the full path from the project root.

Automatic updates.

- When the tray starts (at logon, before agent sessions use the daemon) and npm has a newer version, it runs the
  install command in a visible console that closes on success and stays open on failure. Each version is installed
  automatically once: a failed update restores the previous version and restarts the tray, which then only offers it.
- The tray menu has "Update to X" (or "Check for updates", which says when the installed version is the latest) and an
  "Update automatically" switch (`auto_update`, on by default). Later daily checks only notify.

Agents that ignored the hook's redirect: only 44 of 368 blocked calls (12%) were followed by a `smart_search` within
30 s; the rest rewrote the search in Bash, node or python.

- The block message names the exact tool and arguments (`mcp__smart-tool__smart_search` with the project root filled
  in, `web_fetch` with the URL), says it is a routing rule and not a failure, that redoing the search through Bash,
  python, node or PowerShell bypasses it, and, in Claude Code, how to load the tool with ToolSearch.
- The MCP server sends instructions on what to do when a call is blocked, and `smart_search`, `web_fetch`,
  `web_search` and their `_result` tools load upfront (`anthropic/alwaysLoad`): Claude Code defers MCP tools, so a
  blocked agent often did not have the replacement in its tool list. In real `claude -p` sessions, an agent asked to
  read a page went straight to `web_fetch`, and one asked to run a broad `grep -rn` switched to `smart_search` after
  the block.
- `cd dir; grep -n x file.py` was measured as a search over the whole tree (the `;` stuck to the directory name hid
  the file): 45 of the 92 logged Bash redirects with `cd dir;` were reads of one file. A command with a recursive
  search after a file read (`sed -n 1,9p a.py; grep -rn x src`) is still measured by the directory it searches.
- A subagent whose tool list has no Smart Tool tool (claude-code-guide, statusline-setup, or an agent in
  `.claude/agents` with a `tools` list without it) is not redirected: it had nothing to switch to and gave up.
- Bash commands that change files, the repository or dependencies (`git checkout`, `rm`, `sed -i`, `writeFileSync`,
  `npm run`, ...) are never redirected and skip the router model: 12 of 283 Bash redirects were such commands.

## 0.9.5 - beta

Reported by the claude-code-boss session on its first day using 0.9.4.

- `affected_tests` answered "no tests" for a module covered by a 1.4 MB test file (1,427 cases): the file was over the
  index size limit and the working-tree reading skipped files over 1 MB, so it fell out of the import graph silently.
  Files up to 5 MB are now analyzed for the graph, and any tracked test file still outside it is selected ("may cover
  the change") instead of disappearing.
- Tests that run a script by path (`spawnSync('node', [path.join(SCRIPTS, 'hook.js')])`, `subprocess.run(["python",
  "tools/x.py"])`) now depend on that script: a file name in a string literal of a test, resolved against the test's
  folder, the folders above it and the project root, links them.
- A test is left out of the list only when it has no test cases (`conftest.py`, fixtures, helpers). The former rule
  ("imported by another test") dropped real Java tests that share test types: on gson it caught 19 of 28 injected
  faults; now 28 of 28. JUnit 3 suites (`@RunWith(AllTests.class)`, `static Test suite()`) count as tests.
- Test files named in `package.json` scripts (`"test": "node scripts/test-units.js"`) get a `node <file>` command.
- An anonymous callback among the touched functions is named after the function that contains it.
- `project_manage register` and `status` return the project with its last jobs summarized and without the scope's
  folder structure and profile groups (`scope_summary` keeps include/exclude): they returned the full result of every
  past search (108-128 thousand characters on one project, now 3.3 thousand). The projects screen keeps the full
  payload.
- A project is no longer marked `degraded` because the per-chunk function analysis was still being prepared: that note
  is shown with the search results but is not a degradation.

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
