# Changelog

## 0.9.31 - beta

- `scout_model` is filled by default. The setup screen (and the daemon, when it checks the models) now fills an empty
  `scout_model` with Claude Haiku 5.5 when the configured gateway has it; a choice you made is never replaced, and a
  gateway without that model leaves the field empty to choose (the scout then returns the literal lines, as before).
- The other recommended models were re-measured by the same criterion (correctness first, price second; 3 rounds per
  model; docs/research/2026-10-24-modelos-do-scout-e-dos-outros-papeis.md) and changed for new fills: `router_model`
  Claude Haiku 5.5 (was GPT-4o-mini: it invented an answer to every request whose answer was not on the page),
  `scope_model` GPT-5.4 nano (same accuracy as GPT-5-mini at 29% of the price), `research_model` GPT-5.4 nano (was
  GPT-OSS 20B, which failed tool calls). Models already set are kept.
  - Scout with an index (16 questions x 3 rounds x 15 models): Haiku 5.5 21/36 files found (the index's ceiling, equal
    to no model), 12/12 on the questions with no answer, 0 failures; without an index, 20/36 against 12/36 for the
    keyword scan and at most 17/36 for the other models.
  - 8-question sessions with Sonnet: US$ 0.121 per session with Haiku 5.5 against 0.177 without a model, 73/80 against
    75/80, and the agent never left the tool to read files itself (GPT-4o-mini: 10 times).
- The client repeats a chat call once without `temperature` when the gateway answers 400 about it (Claude on Bedrock,
  the GPT-5 family) and remembers the model; a second refusal still fails.
- Tests: `tests/test_model_client.py`; probes `tests/scout_model_probe.py` and `tests/role_model_probe.py`;
  `tests/scout_probe.py --scout-model`.

## 0.9.30 - beta

- New MCP tool `smart_scout(project_root, question, terms?, max_files?)`: answers a question about a project's code with
  the lines that answer it, read inside Smart Tool so the files never enter the agent's context.
  - Candidates come from the `smart_search` pipeline: the index when it is ready, the index plus a keyword read of the
    files changed after it, or the keyword scan of the disk while the first index is built (answered at once, outside
    the per-folder queue). With no index, no saved scope and no model, the scan covers the whole project (dependency,
    hidden and generated folders excluded) instead of the root files only.
  - For each of up to `max_files` files (default 5, at most 10) it reads the current lines from disk and keeps the
    window with most of the question's words; the agent gets `path:lines`, the functions in it and those lines verbatim.
  - Optional `scout_model` (setup screen, "Code scout"; empty by default): a small model of the configured gateway
    reads the windows, writes up to 5 lines of answer and picks the line ranges to show, which are sliced from what
    was read on disk. Without an index it first chooses the terms and folders to scan (two calls; one with an index).
    A range outside what was read is clipped or dropped; an answer that cites no lines comes with the best keyword
    windows; a model failure returns the lines with the error in a warning. Model calls are metered under the
    purpose `scout` (`project_manage action=usage`).
  - The server instructions point to `smart_scout` for questions that would mean reading several files, and the tool
    loads upfront in Claude Code.
  - Measured in isolated sessions (clean HOME and config, no CLAUDE.md, hooks, skills or plugins; 8 questions per
    session, Sonnet, two repositories; docs/research/2026-10-24-smart-scout-construido-e-medido.md): with an index,
    `smart_scout` with a `scout_model` cost US$ 0.154 per session against US$ 0.250 for `smart_search` alone, 31/32
    right against 29/32, 90 s against 70 s; without a model, US$ 0.203, 29/32, 63 s. On a fresh install (no index)
    it answered in 43 s (no model) and 117 s (model) against 256 s.
- Tests: `tests/test_scout.py`; probe `tests/scout_probe.py`.

## 0.9.29 - beta

- Registering the MCP server in Claude Code or Codex is idempotent. `claude mcp add` refuses a name that exists
  ("MCP server smart-tool already exists in user"), so a second registration used to fail. Now the same URL runs
  nothing and succeeds, another URL (the port changed) is removed with `claude mcp remove smart-tool --scope user`
  (`codex mcp remove smart-tool`) and added again, and no entry only adds. The result is read back from the client's
  configuration, as before.
- The client CLI is resolved before it runs and never left hanging:
  - Only `.exe`, `.cmd` and `.bat` are candidates, never a `.ps1` or the extensionless script npm leaves beside its
    shims.
  - A `.exe` that does not start with `MZ`, or an npm shim whose `claude-code` package holds such a file (Claude Code
    installed by npm 11 with its postinstall blocked: a ~500-byte text `claude.exe`, which makes Windows open a modal
    "not compatible" box and hang the command), fails at once with the reason and, for Claude Code,
    `npm install -g @anthropic-ai/claude-code@latest --include=optional --allow-scripts=@anthropic-ai/claude-code`.
  - The limit is 120 s (it was 60) and `SMART_TOOL_CLI_TIMEOUT_S` sets it.
- The CLI is searched in the PATH as Windows stores it now (machine and user, from the registry), not the one the daemon
  started with, and then in `%USERPROFILE%\.local\bin`, `%LOCALAPPDATA%\Programs\OpenAI\Codex\bin` and `%APPDATA%\npm`.
- Networks that inspect TLS with a corporate root CA:
  - `npx ... install` shows the real error code (`SELF_SIGNED_CERT_IN_CHAIN`, kept by Node in `error.cause`, not
    "fetch failed") and, on Node 22.15+/23.8+/24, runs itself again once with `NODE_USE_SYSTEM_CA=1`; otherwise it says
    to set `NODE_EXTRA_CA_CERTS`. uv gets one retry with `UV_NATIVE_TLS=1` on a certificate error. The installer steps
    (npm, Playwright) run with `NODE_USE_SYSTEM_CA=1`, and the Camoufox download uses the Windows store through
    `truststore`. `NODE_EXTRA_CA_CERTS`, `SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE`, `UV_NATIVE_TLS` and
    `NODE_USE_SYSTEM_CA` are never overwritten.
  - The bootstrap ends by setting `process.exitCode` instead of calling `process.exit()` while a fetch is closing (Node
    24 on Windows aborts with `Assertion failed: !(handle->flags & UV_HANDLE_CLOSING)`).
- Tests: `tests/test_cli_registration.py`, `tests/test_bootstrap_network.py`, and cases in `test_fetch_camoufox.py` and
  `test_install_runtime.py`.

## 0.9.28 - beta

- Node, npx and npm are looked up when they are needed, never remembered. A process keeps the PATH it was born with:
  the tray started at logon never saw a Node installed, moved or removed afterwards, so the automatic update
  (`npx --yes ... install`, run from the tray) stopped working once Node changed place, and everything the update then
  started (installer, restarted tray) inherited the same stale PATH. New `node_locator.py`: the process PATH, then the
  machine and user PATH stored in the registry, then the usual install folders (Program Files, nvm, Volta, fnm, Scoop,
  Chocolatey); the Node inside Smart Tool (`web_adapters/node/.node-runtime`) is used only when the machine has none.
  - The updater launches the install with that fresh PATH and the folder of the `npx` it found first in it.
  - The web runtime installer takes the machine's Node from the PATH first (it preferred the one inside Smart Tool).
  - Search adapters and the code analyzers resolve Node at each launch, so a Node changed while the daemon runs is
    followed without a restart.
  - The Playwright MCP registration writes the absolute Node path into the client's configuration; the daemon now
    registers it again at start when that executable no longer exists (`browser_control.heal`).
- Tests: `tests/test_node_locator.py` (Node in an unusual folder, only in the registry PATH, only in a known folder,
  nothing found, the updater without npx in the tray PATH, a registration whose Node is gone).

## 0.9.27 - beta

- No folder is read before it is classified, and a tool's hidden folder (`.crowd`, `.tokenguard`, `.vscode`, any name
  starting with a dot) is never indexed, whatever the scope says:
  - An empty or missing `include` used to mean "the whole project" (the saved scopes of `sistema` and a Claude memory
    folder had `include: []`), and a search of a folder with no index and no scope read every file of it from disk (one
    registered folder had 715 427). An empty `include` now indexes only the loose files at the root, and that first
    keyword search reads only those.
  - The `scope` action without a saved scope defaulted to `include: ["."]`; it now requires `include`, and refuses an
    include inside a hidden folder.
  - A model that answered with `docs/**`, `*` or `**/*` (the cause of the `error` status of two registered folders)
    now has the entry reduced to the folder it names; an entry that is absolute, climbs out with `..`, sits in a hidden
    folder or keeps a wildcard in the middle is dropped and kept in `dropped_include` of the saved scope.
  - Hidden files at the root and inside any included folder are no longer listed (they were listed and then skipped
    when read).
  - The mechanical exclusion grows: `site-packages`, `bower_components`, `jspm_packages` everywhere, and `target`
    (Cargo.toml or pom.xml at the root) and `Pods` (Podfile). These are never offered to the scope model.
- Tests for each rule: `tests/test_scope_rules.py`.

## 0.9.26 - beta

- The daemon no longer re-reads and re-hashes every watched folder in a loop. Measured on the owner's machine: one
  daemon process read 181 GB and wrote 29.7 GB in 15 h (3.6 MB/s read, 2.2 MB/s write while idle), which kept the disk
  busy and made Defender scan the files again and again. Causes, all fixed:
  - A file-event buffer overflow, or more than 1 000 changed paths, raised a full check that opened and hashed every
    file of the scope; the flag was cleared only when no event arrived during the job, so under constant events (the
    `~/.claude` session logs, a state folder) every job hashed everything again, and the stuck scope flag also asked the
    scope model to decide the scope again on every job. An overflow now asks for a stat comparison (mtime and size) of
    the scope, and a finished job clears the full-check and scope flags raised before it started.
  - An event under a folder the scope leaves out (`plugins/cache`, `projects/*.jsonl` of `~/.claude`) marked the project
    dirty and queued an index job. Events are now filtered by the saved scope, which is reloaded only when its file
    changes.
  - A folder growing inside an excluded one counted as a structure change and called the scope model again. The
    structure kept in the scope and the one compared against it skip excluded folders.
  - The runtime folders of a `.claude` home (`plugins`, `projects`, `sessions`, `shell-snapshots`, `file-history`,
    `cache`, `debug`, `telemetry`, ...) are excluded mechanically, whatever the scope says.
  - The monitor decrypted up to 50 job payloads per watched folder every second just to learn whether a job was
    pending; it now asks the `status` column once per tick. A Git folder is identified again (four git processes)
    only when `HEAD`, the index or the refs moved, not every 5 s. With a live watcher the stat reconciliation runs
    every 15 min instead of 3; the tick sleeps 5 s instead of 1 s while nothing is pending.
  - A burst of events cost two reads and two writes of the project registry per batch; it is one of each, and the
    registry schema is created once per process instead of per connection.
  - Every save of a job (several per index job) ordered the whole `jobs` table, payloads included, to prune the history:
    1.7 MB read per save. The pruning now takes its threshold from an `updated_at` index (50 saves: 85.7 MB read before,
    5.0 MB after, `tests/save_job_io_probe.py`).
  - The pending-job query of the monitor tick scanned the whole `jobs` table (2.7 MB) every second; it now reads the
    covering index `jobs_status`.
  - A folder that changes all the time (the orchestrator's state folder queued an index job every 8 s) is indexed by the
    monitor at most once a minute; the changes accumulate and one job covers them.
  - Measured with `tests/monitor_io_probe.py` on a copy of the real registry (14 watched folders, 100 event batches
    under excluded folders with a buffer overflow every 10th): 149 MB read and 45.7 MB written before, 28 MB read and
    5.4 MB written after; folders left with a pending full check 14 before, 0 after.
## 0.9.25 - beta

- A redirected search made while the first index of a folder is still being built no longer waits in the folder's queue
  behind the build (22 s measured, past the hook's 15 s, so the agent got the native result with no notice): it is
  answered at once by the keyword search of the disk, with the redirect notice and the index-loading warning.
- The first search of a cold folder also gets the index-loading sentence in the notice (it was read from a job field that
  the answer text does not carry; it is now read from the answer's own warning).

## 0.9.24 - beta

- A redirected search now tells the agent what happened: the answer opens with a line saying the search was redirected to
  `smart_search` (index, related code, dependency map), that the index of the project is still loading when the answer is
  a keyword search of the disk (the next searches will have it ready), and to keep using `smart_search`. Before, the
  answer carried no word of the redirect.
- The deny text of a search that cannot be redirected no longer says that reading or searching inside one known file is
  never blocked: no message the agent receives offers a way around the redirect (file by file, narrower scope).
  `NoWayAroundTheRedirectTest` fails if any of those texts (deny, advice, redirect notice, cold-index and stale-index
  warnings) suggests one.

## 0.9.23 - beta

- The hook no longer blocks. A content search over a project folder (the Grep tool on a folder, `grep -r`, `rg`,
  `git grep`, `findstr /s`, `Select-String` over a listing, an inline python/node script that walks a folder) and
  native WebFetch/WebSearch are answered by Smart Tool, and the answer comes back as the output of the call the agent
  made, with no block message: before, the deny pushed agents to retry the same search through Bash and the Smart Tool
  went unused. The query is the search pattern of the call (regex syntax dropped); the project is the registered one
  holding the folder, else the session folder.
  - Claude Code: a new PostToolUse entry (`/hooks/posttool`, 60 s) puts the answer in the tool's own output, in the
    shape that tool returns. A shell search whose answer is ready within 10 s runs as a no-op (`true`, `$null` in
    PowerShell), so a grep with no match no longer ends in exit 1; a later answer replaces the native output when the
    command succeeds. WebFetch and WebSearch still run natively and their result is replaced (measured on Claude Code
    2.1.300: a PreToolUse cannot skip them, and a WebFetch pointed at the daemon fails because http turns into https).
    WebSearch with `allowed_domains`/`blocked_domains` is left alone.
  - Codex: the shell search is rewritten to `cat` a file holding the answer, approved with `allow` (Codex applies a
    rewrite only with it); the daemon lets `allow` through for that command and nothing else.
  - A Smart Tool call that fails or does not answer in time leaves the native call and its output as they were. A deny,
    with the exact call to make, remains only for a search whose pattern cannot be read from the command, and for a
    client that sends no `tool_use_id`.
  - A redirected shell search waits up to 10 s for its answer in the PreToolUse (a warm `smart_search` takes 4 to
    13 s with the reranker), so the PreToolUse entry's timeout goes back from 6 s to 15 s (Codex's thin command 5 s
    to 14 s); every other decision keeps its 4 s budget in the daemon.
  - The setup screen counts a redirect answered this way as `served`. The hook entry of an older version shows as
    outdated until you accept the update (preview and backup first).
- `smart_search` with `mode=dossier` answers a narrow keyword query over `docs/research`, the backlog, `CLAUDE.md` and
  `AGENTS.md` with ids, short excerpts and a time budget, without an index.

## 0.9.22 - beta

- The review of an Edit or Write (docstring reminder, copied-function check) is about twice as fast on a warm daemon
  (a whole-file Write of the 130 KB `smart_tool_daemon.py`: 1.7 s to 0.8 s), so it stays within the hook's 4 s on large
  files instead of being skipped: the scan of pending changes runs once per review instead of once per use, the
  functions of each changed file are remembered until the file is written again, and the impact lookup reads the cached
  graph instead of copying it. The first review after the daemon starts still waits for the graph to load and may be
  skipped; the work finishes in the background and the next one is fast.

## 0.9.21 - beta

- The PreToolUse hook can no longer hold a tool call for long. Its decision has 4 s; past that it answers without one
  (the tool runs) and the daemon log gets one line, `PreToolUse hook over 4 s for Write <file>: answered without a
  decision`; an answer slower than 1 s is logged too. Before, Write and Edit reviews (docstring reminder, copied
  function check) could take longer than the client's 15 s timeout on large TypeScript or Java files and large
  repositories (75 cancelled hook calls in 9 days, 62 of them Write or Edit) and Bash calls of other sessions waited
  behind them.
- Edit and Write reviews run on their own two workers: a third simultaneous edit goes through unreviewed (logged) instead
  of queueing, and Bash, Read and Grep never wait for them.
- The Node code analyzer called by the edit hook stops after 10 s, and an analysis that failed or timed out is no
  longer remembered as "this file has no functions".
- The timeout written in the client hook entry drops from 15 s to 6 s (Codex's thin command 14 s to 5 s). An entry
  written by an older version shows as outdated on the setup screen until you accept the hook update (preview and
  backup first).

## 0.9.20 - beta

- The first search of a folder that has no index yet no longer waits for the index. It answers at once with the files
  that hold a query term, read from disk and ranked by keyword (up to 8 s of scanning, and it says how many files it
  reached), and builds the index in the background; the answer says "Indexing in progress: this folder has no index
  yet". A folder with no saved scope and no model to decide it still asks the agent for the scope, as before.
- `smart_search_result` hands the agent the result text itself when the job is done, the same text a search that
  finishes within the wait returns. Before, it was a JSON wrapper that escaped the text and repeated the index stats
  and the git view (~1.5 thousand characters of metadata per answer). A job still pending or failed comes without
  those fields too.
- Playwright MCP starts with `--caps=` so a `PLAYWRIGHT_MCP_CAPS=network` in the user's environment cannot turn on
  `browser_route` (measured on 0.0.83: with the variable set, 29 tools including `browser_route`; with `--caps=`, 25
  and no `browser_route`).
- `CODEX_HOME` is followed everywhere the Codex config is read or written: the MCP registration check, the endpoint
  sync after a port change and the Codex hook file (before, only the browser control and the setup audit followed it).
- `affected_tests` links a Python test to the modules it imports inside a string, as in
  `subprocess.run([sys.executable, "-c", "import click"])`, so a change to that module selects the test.

## 0.9.19 - beta

- A search no longer waits for the index to catch up. It answers from the existing index at once, searches the files
  edited since the last index by keyword straight from disk, and says "Indexing in progress: N file(s) changed..." in
  the answer while the update runs in the background. An index that does not exist yet, or was built by another model,
  is still built first.
- A search waits up to 25 s for its own result before answering with a `job_id` (it was 3 s, below the 3.3 s a warm
  search takes at best, so nearly every call needed a second `smart_search_result`).
- The scope and the language profile are no longer asked of the model inside a search that has an index: the saved ones
  are used and refreshed in the background.
- New folders are registered watched and eager: the index follows edits by itself. A PreToolUse Edit/Write of an eager
  folder without a running watcher queues one debounced update with the edited paths. Folders already registered keep
  their policy.
- A `top_k` whose total is above 20 is scaled down in proportion instead of rejecting the call (the rejection cost the
  agent a turn on its first search).
- A search made right after an edit keeps the just-edited chunk in the answer: the reranker can no longer push it out.
- An edit whose path comes in the Windows 8.3 short form (`ALLANS~1`-style folder names) is matched to its registered
  project; before, the project lookup compared it with the long form and found nothing.
- The daemon log prints the seconds spent in each phase of a job (`phases=queued:0.02,auth:0.01,...`).

## 0.9.18 - beta

- Updates are no longer applied only at logon. The tray checks npm at start and every 24 h and installs a newer
  version as soon as it sees it (on by default); a version that failed to install is tried again 24 h later instead of
  never. `SMART_TOOL_AUTO_UPDATE=off` turns automatic updates off, like the tray switch.
- Nothing about updates is hidden. The tray notifies when a newer version is available, when it starts updating, when
  Smart Tool was updated (from which version to which) and when an update failed. Agent sessions get the same line once
  on their first `smart_search`, with the link to the changes, and the SessionStart report (when on) shows it to you
  at session start.

## 0.9.17 - beta

- The agent learns the setup audit result without asking for it. On its first `smart_search` of a session, once per
  folder, it gets one short line when the stored audit has errors or warnings (nothing when clean, nothing is run).
  `project_manage action=setup_audit mode=last` returns the stored report without auditing again.
- Optional **Report at session start (Claude Code)** on the Setup audit page: off by default; it previews the exact
  SessionStart entry, writes it to Claude Code's settings after you confirm (backup first), and **Turn off** removes
  only that entry. While it is on, the search no longer repeats the line.

## 0.9.16 - beta

- New setup screen. It opens on an **Overview** that checks everything Smart Tool needs on this machine (model
  gateway, each agent's connection and hook, block patterns waiting, the service, web search, the setup audit) and
  lists what needs you first, each with a button that takes you to the fix. A side menu replaces the long scroll:
  Connect (Agents, Models, Web search), Behavior (Agent rules, Redirects) and Maintenance (Setup audit, Startup),
  with a count of open items next to each. Every section says what it is for; choices apply as soon as they are
  picked, and typed values keep a save button next to them. Light and dark follow the system; under 860 px the menu
  becomes a row of tabs.
- An outdated hook says so. The setup screen compares each agent's Smart Tool hook entry with the one this version
  writes and lists what the old one misses (tools not covered, an old address or timeout). **Update hook** shows the
  exact entry first and, after you confirm, rewrites only Smart Tool's entry (other hooks stay, the file is backed up).
  The tray notice also names an outdated hook.
- Agent rules (redirect behavior, documentation and duplicate checks on edit, block pattern proposals) moved off the
  Agents section into their own, with the recommended choice marked.

## 0.9.15 - beta

- The hook redirects the folder searches that slipped past it in a shell: wrapped (`bash -c`, `sh -lc`,
  `powershell -Command`, `cmd /c`, `timeout 60`, `env X=1`, `time`, `nice`, a `( ... )` subshell), looped
  (`for f in src/*.py; do grep x "$f"`), fed by a listing (`find -exec grep|cat`, `find | xargs -0 grep`,
  `grep x $(find src -name '*.py')`, `find | while read f`, `Get-ChildItem -Recurse | Select-String` or
  `| % { Get-Content $_ }`), `git --no-pager grep`, `cat` over more than 20 files, and inline python/node scripts
  (`-c`, `-e`, heredoc) that walk a project folder, read the files and match their text. A listing capped by `head`,
  `find -name` of one exact file and scripts that write files still run. Replayed over 3 days of real transcripts
  (14,580 Grep and shell calls), 37 more calls are redirected, all folder searches; none of the previous redirects
  changed.
- Claude Code's PowerShell tool goes through the same rule as Bash; it was not in the hook matcher. Reinstall the
  hook from the setup screen (it shows as outdated): the matcher now has `PowerShell` and `smart_search`.
- Setup screen, Agents: the redirects of the last 7 days by tool and reason, and what the same session did next
  (called `smart_search`, ran an allowed native call, was blocked again, or nothing within 15 minutes). The hook log
  now records the session, and `smart_search` calls pass through the hook only to be counted. Next to it, the
  measured context per call: a native call of the kinds redirected ~510 tokens, a `smart_search` round trip ~2,160
  tokens; a redirect saves context only when one search replaces several native ones, which is not measured yet.

## 0.9.14 - beta

- The setup audit runs on its own: the daemon audits every registered project two minutes after it starts and then
  every 30 minutes, one at a time. MCP servers several projects share are contacted once and a successful result is
  reused for 6 hours (a failure never is, so a server fixed meanwhile shows at once; Run now always contacts them
  again). A project's `.mcp.json` servers are only counted and started when Claude Code approved them
  (`enableAllProjectMcpServers`, `enabledMcpjsonServers`, `disabledMcpjsonServers`): registering a cloned repository
  for search never runs the commands it declares. The summary per project is in
  `~/.smart-tool/data/setup-audit/index.json`; projects no longer registered are dropped from it.
- **Setup audit** panel on the setup screen: pick a registered project (the one with the most errors opens first) and
  see its last report, scheduled or on request: findings by severity with their documentation link, what is new,
  what was resolved, the tokens each MCP server's tools cost and what was not verified; Run now audits it again.
  Everything taken from config files is shown as text.

## 0.9.13 - beta

- Setup audit: `project_manage` with `action=setup_audit` and a `project_root` (or `python setup_audit.py --project
  <folder>`) reports what Claude Code and Codex actually load for that folder (settings layers, enabled plugins with
  their hooks, MCP servers and skills, CLAUDE.md and AGENTS.md, Codex `config.toml` and `hooks.json`) and where they
  collide: several PreToolUse hooks deciding on the same tool in parallel, hook scripts that do not exist or discard
  their errors, hooks calling an MCP server that is not configured, hook endpoints down, MCP servers that do not
  start (each is started or called with `initialize` and `tools/list`, as a session would) or offer the same tools
  twice (a gateway re-exposing a server also registered directly), skills sharing a name or over the description
  budget, an AGENTS.md only Codex reads, broken `@imports` and a Codex instruction chain over 32 KiB. Each finding
  links the documentation it relies on; nothing is edited; env and header values never enter the report; what could
  not be checked is listed as not verified, and a second run shows what is new and what was resolved. On the
  author's machine the first run found four hooks deciding on every Bash call in parallel and a gateway offering six
  tools twice.
- Updating while agents work no longer fails with "Another Smart Tool daemon is still active": a Codex hook running
  during the update started the daemon again from the half-copied folder and held the port. The installer now marks
  the update so hooks and the tray do not start the daemon until it ends, and closes again a daemon a previous
  version's hook brought back meanwhile.

## 0.9.12 - beta

- Security: `web_fetch` rendering with Moli (0.9.9 to 0.9.11) followed redirects and page requests to private
  addresses, so a public page could redirect it to `127.0.0.1` or the local network and the agent received what was
  there. Moli now runs with `--block-private-networks`, the same rule as the HTTP reader and the Chromium fallback.
- Security, browser control: pages the agent opens are untrusted input. Playwright MCP is registered with
  `--no-webmcp` (tools a page registers are never offered to the agent); every request of its browser carries
  `X-Smart-Tool-Agent-Browser`, and the daemon refuses any request with it, so a page cannot steer the agent into the
  setup page and its token, redirects included; `browser_run_code_unsafe` (arbitrary JavaScript in the server
  process, outside any client sandbox) is denied in the client (`permissions.deny` in Claude Code, `disabled_tools` in
  Codex), and the server is not registered when that fails. Existing registrations are refreshed on update.
- Security hardening: block pattern proposals with lookarounds or backreferences, or that take over 5 s on a long
  command, are discarded; an accepted pattern applies only to calls whose reach the rule cannot measure (a single-file
  search stays allowed, as documented) and the block message no longer repeats the model's text; client CLIs that are
  `.cmd` shims never receive arguments cmd.exe would interpret; the Moli archive extraction refuses drive-relative and
  alternate-stream names.
- `web_fetch` reads PDFs (pypdf, page by page, up to 300 pages, in a subprocess stopped at the request's deadline; a
  PDF with only an owner password opens, one that needs a password to open is refused with that reason). The hook sends the native WebFetch here, and a PDF
  was a dead end: 9 times in 10 days of sessions.
- Agents are told to use `smart_search` first to explore code, not only after a block: in 10 days, 89 searches were
  started by the agent against about 5,600 folder searches with the built-in tools. The server instructions and the
  `smart_search` description now say when to use it, as `web_search`/`web_fetch` already did.

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
