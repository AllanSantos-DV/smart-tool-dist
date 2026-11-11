"""Fila justa entre raízes; jobs aguardando a mesma pasta não ocupam workers. Um job de prioridade maior passa à frente
dos de prioridade menor ainda na fila (o que já está rodando não é interrompido)."""
import collections
import concurrent.futures
import threading

import project_identity


class ProjectScheduler:
    def __init__(self, workers=3):
        self.workers = workers
        self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="project")
        self._lock = threading.RLock()
        self._queues = collections.OrderedDict()
        self._running = set()
        self._closed = False

    def submit(self, root, function, *args, priority=0):
        key = project_identity.canonical_root(root)
        future = concurrent.futures.Future()
        with self._lock:
            if self._closed:
                raise RuntimeError("Project queue closed.")
            queue = self._queues.setdefault(key, collections.deque())
            at = next((index for index, item in enumerate(queue) if item[3] < priority), len(queue))
            queue.insert(at, (future, function, args, priority))
            self._dispatch()
        return future

    def _dispatch(self):
        while len(self._running) < self.workers:
            waiting = [root for root in self._queues if root not in self._running]
            if not waiting:
                break
            key = max(waiting, key=lambda root: self._queues[root][0][3])
            task = self._queues[key].popleft()
            if not self._queues[key]:
                self._queues.pop(key)
            else:
                self._queues.move_to_end(key)
            self._running.add(key)
            self._pool.submit(self._run, key, task)

    def _run(self, key, task):
        future, function, args, _priority = task
        try:
            if future.set_running_or_notify_cancel():
                try:
                    future.set_result(function(*args))
                except BaseException as exc:
                    future.set_exception(exc)
        finally:
            with self._lock:
                self._running.discard(key)
                self._dispatch()

    def shutdown(self, wait=True):
        with self._lock:
            self._closed = True
            for queue in self._queues.values():
                for future, _fn, _args, _priority in queue:
                    future.cancel()
            self._queues.clear()
        self._pool.shutdown(wait=wait, cancel_futures=True)
