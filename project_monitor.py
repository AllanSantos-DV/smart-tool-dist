"""Eventos Windows + reconciliação: fila apenas para projetos habilitados pelo usuário."""
import os
import threading
import time

import config
import directory_watch
import indexer
import local_embedder
import index_scope
import project_store
import index_views

DEBOUNCE_S = 5
RECONCILE_S = 180
FULL_CHECK_S = 86400
MAX_EVENT_PATHS = 1000


def relevant_path(path):
    parts = path.replace("\\", "/").strip("/").split("/")
    return bool(parts and all(part not in ("", ".", "..") and not part.startswith(".")
                              and not index_scope.always_excluded(part) for part in parts))


def snapshot(root, scope):
    result = {}
    for rel in index_scope.resolve_included_files(root, scope):
        stat, _reason = indexer._stat_candidate(root, rel)
        if stat is not None:
            result[rel] = stat
    return result



def index_model(current=None):
    return local_embedder.resolve(config.load_config().get("embedding_model"), current)


def needs_model_rebuild(meta, configured_model):
    return meta.get("model_id") != configured_model or not meta.get("exists")

class ProjectMonitor:
    def __init__(self, enqueue, watcher_factory=directory_watch.DirectoryWatcher):
        self.enqueue = enqueue
        self.watcher_factory = watcher_factory
        self._watchers = {}
        self._retry_watch = {}
        self._checked = {}
        self._git_seen = {}
        self._git_checked = {}
        self._pending = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run, name="project-monitor", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        with self._lock:
            watchers = list(self._watchers.values())
            self._watchers.clear()
        for watcher in watchers:
            watcher.stop()

    def refresh(self):
        self._checked.clear()

    def events(self, project_id, rows, overflow=False):
        project = project_store.get(project_id)
        if not project or project.get("paused") or not project.get("watch") or not project.get("enabled"):
            return
        paths = {row["path"] for row in rows if relevant_path(row.get("path", ""))}
        if not paths and not overflow:
            return
        # Um diretório existente também recebe MODIFIED ao mudar um arquivo filho.
        # Só uma estrutura nova ou documentação da raiz exige reavaliar o escopo.
        scope = index_scope.load_scope(project["root"]) or {}
        known_dirs = {p.casefold() for p in scope.get("structure", [])}
        docs = {name.casefold() for name in index_scope._ROOT_DOC_NAMES}
        force_scope = any(path.casefold() in docs for path in paths) or any(
            row.get("action") in (1, 5) and row.get("path") in paths
            and row["path"].casefold() not in known_dirs
            and os.path.isdir(os.path.join(project["root"], row["path"])) for row in rows)
        force_scope = force_scope and not scope.get("manual", False)
        with self._lock:
            first = project_id not in self._pending
            state = self._pending.setdefault(project_id, {"paths": set(), "full": False, "force_scope": False, "revision": 0})
            state["revision"] = state.get("revision", 0) + 1
            state["paths"].update(paths)
            state["last"] = time.monotonic()
            state["full"] |= overflow or len(state["paths"]) > MAX_EVENT_PATHS
            state["force_scope"] |= force_scope
            if state["full"]:
                state["paths"].clear()
        project_store.mark_dirty(project_id, "Changes detected by the monitor.")
        project_store.record_changes(project_id, paths, full=overflow, scope=force_scope)

    def _watch_error(self, project_id, message):
        project_store.update(project_id, watch_backend="reconciliation", watch_error=message[:300])
        self._retry_watch[project_id] = time.monotonic() + RECONCILE_S
        self.events(project_id, [], overflow=True)

    def _sync_watchers(self, projects, now):
        wanted = {p["id"]: p for p in projects if p.get("watch") and p.get("enabled")
                  and not p.get("paused") and os.path.isdir(p["root"])}
        for key in list(self._watchers):
            if key not in wanted:
                watcher = self._watchers.pop(key)
                watcher.stop()
                project_store.update(key, watch_backend="off")
        for key, project in wanted.items():
            current = self._watchers.get(key)
            if current and current.alive:
                continue
            if now < self._retry_watch.get(key, 0):
                continue
            watcher = self.watcher_factory(
                project["root"], lambda rows, overflow, k=key: self.events(k, rows, overflow),
                lambda error, k=key: self._watch_error(k, error),
            )
            self._watchers[key] = watcher
            project_store.update(key, watch_backend="windows_events", watch_error="")
            watcher.start()

    def tick(self):
        now = time.monotonic()
        projects = project_store.all_projects()
        self._sync_watchers(projects, now)
        for project in projects:
            key = project["id"]
            if not project.get("watch") or not project.get("enabled") or project.get("paused"):
                continue
            if not os.path.isdir(project["root"]):
                project_store.update(key, status="unavailable", last_error="The folder is not accessible.")
                continue
            if any(job.get("status") == "pending" for job in project_store.jobs(key, limit=50)):
                continue  # Não planejar outra geração com metadados de uma ainda em curso.
            if now - self._git_checked.get(key, -5) >= 5:
                view = index_views.describe(project['root'], fresh=True)
                signature = (view['view_id'], view.get('commit'))
                if key in self._git_seen and signature != self._git_seen[key]:
                    self.events(key, [], overflow=True)
                self._git_seen[key] = signature
                self._git_checked[key] = now
            if now - self._checked.get(key, -RECONCILE_S) >= RECONCILE_S:
                self._checked[key] = now
                scope = index_scope.load_scope(project["root"])
                meta = indexer.info(project["root"])
                rescope = scope is None or index_scope.needs_rescan(project["root"], scope)
                changed = rescope
                if scope is not None:
                    changed |= snapshot(project["root"], scope) != indexer.manifest_snapshot(project["root"])
                full = time.time() - meta.get("full_scan_at", 0) >= FULL_CHECK_S
                changed |= needs_model_rebuild(meta, index_model(meta.get("model_id")))
                project_store.update(key, last_checked=time.time())
                if changed or full or project.get("dirty_seq", 0) > project.get("indexed_seq", 0):
                    with self._lock:
                        first = key not in self._pending
                        state = self._pending.setdefault(key, {"paths": set(), "last": now - DEBOUNCE_S,
                                                               "full": False, "force_scope": False, "revision": 0})
                        state["full"] |= full
                        state["force_scope"] |= rescope
                    if first:
                        project_store.mark_dirty(key, "Reconciliation found a pending update.")
                    project_store.record_changes(key, full=full, scope=rescope)
            # Monitoramento local é independente do gasto com o modelo. O padrão
            # somente registra pendência; a próxima busca executa o diff e embeddings.
            if project.get('update_mode', 'on_search') != 'eager':
                with self._lock:
                    self._pending.pop(key, None)
                continue
            with self._lock:
                pending = self._pending.get(key)
                if not pending or now - pending["last"] < DEBOUNCE_S:
                    continue
                options = {"changed_paths": sorted(pending["paths"]), "full_check": pending["full"],
                           "force_scope": pending["force_scope"]}
                revision = pending.get("revision", 0)
            current = project_store.get(key)
            if time.time() < current.get("next_retry_at", 0):
                continue
            try:
                self.enqueue(key, **options)
            except Exception as exc:
                project_store.update(key, status="error", last_error=str(exc)[:300], next_retry_at=time.time() + 60)
                continue
            with self._lock:
                # Um evento recebido durante o submit continua pendente para a próxima geração.
                if self._pending.get(key) is pending and pending.get("revision", 0) == revision:
                    self._pending.pop(key, None)

    def _run(self):
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                pass  # Falha opcional do monitor não derruba o daemon MCP.
            self._stop.wait(1)
