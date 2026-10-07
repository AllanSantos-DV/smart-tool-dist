"""Cliente do navegador residente (web_adapters/browser/browser_service.py): um processo para todos os consumidores."""
import concurrent.futures
import json
import os
import queue
import subprocess
import threading
import time
import uuid

START_TIMEOUT_S = 45
START_FAILURE_COOLDOWN_S = 60
SERVICE_IDLE_S = 300
# Restart instead of writing to a service that may be exiting on its own idle timer.
IDLE_RESTART_MARGIN_S = 15
_IS_WINDOWS = os.name == "nt"


class BrowserCaptcha(RuntimeError):
    pass


class BrowserSkipped(RuntimeError):
    """Self-imposed limit (queue, pacing, pause, startup): not a failure of the search engine."""


class BrowserExited(RuntimeError):
    pass


def _kill_on_close_job():
    import ctypes
    from ctypes import wintypes

    class IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in
                    ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]

    class BasicLimits(ctypes.Structure):
        _fields_ = [("per_process_time", ctypes.c_longlong), ("per_job_time", ctypes.c_longlong),
                    ("flags", wintypes.DWORD), ("min_ws", ctypes.c_size_t), ("max_ws", ctypes.c_size_t),
                    ("active_processes", wintypes.DWORD), ("affinity", ctypes.c_size_t),
                    ("priority", wintypes.DWORD), ("scheduling", wintypes.DWORD)]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [("basic", BasicLimits), ("io", IoCounters), ("process_memory", ctypes.c_size_t),
                    ("job_memory", ctypes.c_size_t), ("peak_process_memory", ctypes.c_size_t),
                    ("peak_job_memory", ctypes.c_size_t)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    limits = ExtendedLimits()
    limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel32.SetInformationJobObject(wintypes.HANDLE(job), 9, ctypes.byref(limits), ctypes.sizeof(limits)):
        raise ctypes.WinError(ctypes.get_last_error())
    return kernel32, job


class BrowserService:
    def __init__(self, executable, script):
        self.executable = executable
        self.script = script
        self.lock = threading.Lock()
        self.start_lock = threading.Lock()
        self.proc = None
        self.writes = None
        self.pending = {}
        self.stderr_tail = ""
        self.last_used = 0.0
        self.start_failure = None
        self.job = None

    def _alive(self):
        return self.proc is not None and self.proc.poll() is None

    def _ensure_started(self, deadline):
        with self.lock:
            if self._alive() and time.monotonic() - self.last_used < SERVICE_IDLE_S - IDLE_RESTART_MARGIN_S:
                return self.proc, self.writes
        if not self.start_lock.acquire(timeout=max(0, deadline - time.monotonic())):
            raise BrowserSkipped("resident browser still opening at the deadline")
        try:
            with self.lock:
                if self._alive() and time.monotonic() - self.last_used < SERVICE_IDLE_S - IDLE_RESTART_MARGIN_S:
                    return self.proc, self.writes
                stale, self.proc = self.proc, None
            if stale is not None:
                self._kill(stale)
            if self.start_failure and time.monotonic() - self.start_failure[0] < START_FAILURE_COOLDOWN_S:
                raise RuntimeError(self.start_failure[1])
            try:
                proc, writes = self._start()
            except RuntimeError as exc:
                self.start_failure = (time.monotonic(), str(exc))
                raise
            self.start_failure = None
            with self.lock:
                self.proc, self.writes, self.last_used = proc, writes, time.monotonic()
            return proc, writes
        finally:
            self.start_lock.release()

    def _start(self):
        flags = (subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW) if _IS_WINDOWS else 0
        proc = subprocess.Popen([self.executable, self.script], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                                creationflags=flags)
        if _IS_WINDOWS:
            self._bind_to_daemon(proc)
        ready = concurrent.futures.Future()
        writes = queue.Queue()
        threading.Thread(target=self._read_stderr, args=(proc,), daemon=True).start()
        threading.Thread(target=self._read_stdout, args=(proc, ready), daemon=True).start()
        threading.Thread(target=self._write_stdin, args=(proc, writes), daemon=True).start()
        try:
            ready.result(timeout=START_TIMEOUT_S)
        except Exception as exc:
            self._kill(proc)
            raise RuntimeError(f"resident browser failed to start: {exc}; {self.stderr_tail[-500:]}") from None
        return proc, writes

    def _bind_to_daemon(self, proc):
        # Kill-on-close job: the browser tree dies with the daemon even when the daemon is killed hard.
        try:
            if self.job is None:
                self.job = _kill_on_close_job()
            kernel32, job = self.job
            import ctypes
            from ctypes import wintypes
            if not kernel32.AssignProcessToJobObject(wintypes.HANDLE(job), wintypes.HANDLE(proc._handle)):
                raise ctypes.WinError(ctypes.get_last_error())
        except OSError as exc:
            self.stderr_tail += f"\nprocess outside the job object: {exc}\n"

    def _write_stdin(self, proc, writes):
        while True:
            line = writes.get()
            if line is None:
                return
            try:
                proc.stdin.write(line)
                proc.stdin.flush()
            except (OSError, ValueError):
                return

    def _read_stdout(self, proc, ready):
        for line in proc.stdout:
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if not isinstance(message, dict):
                continue
            if message.get("ready"):
                if not ready.done():
                    ready.set_result(True)
                continue
            with self.lock:
                _owner, future = self.pending.pop(message.get("id"), (None, None))
            if future and not future.done():
                future.set_result(message)
        reason = f"resident browser exited (code {proc.wait()}): {self.stderr_tail[-500:]}"
        if not ready.done():
            ready.set_exception(RuntimeError(reason))
        with self.lock:
            orphans = [key for key, (owner, _) in self.pending.items() if owner is proc]
            futures = [self.pending.pop(key)[1] for key in orphans]
        for future in futures:
            if not future.done():
                future.set_exception(BrowserExited(reason))

    def _read_stderr(self, proc):
        for line in proc.stderr:
            self.stderr_tail = (self.stderr_tail + line)[-4000:]

    def _kill(self, proc):
        if proc.poll() is None:
            if _IS_WINDOWS:
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True,
                               creationflags=subprocess.CREATE_NO_WINDOW)
            else:
                proc.kill()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            try:
                stream.close()
            except (OSError, ValueError):
                pass

    def search(self, engine, query, num, timeout_s):
        deadline = time.monotonic() + timeout_s
        proc, writes = self._ensure_started(deadline)
        left = deadline - time.monotonic()
        if left < 1:
            raise BrowserSkipped(f"{engine}: deadline used up while opening the browser")
        request_id = uuid.uuid4().hex
        future = concurrent.futures.Future()
        with self.lock:
            self.pending[request_id] = (proc, future)
            self.last_used = time.monotonic()
        writes.put(json.dumps({"id": request_id, "engine": engine, "query": query, "num": num,
                               "expires_at": time.time() + left - 0.5}) + "\n")
        try:
            message = future.result(timeout=left + 2)
        except concurrent.futures.TimeoutError:
            with self.lock:
                self.pending.pop(request_id, None)
            raise TimeoutError(f"{engine}: resident browser did not respond in time") from None
        finally:
            with self.lock:
                self.last_used = time.monotonic()
        if message.get("captcha"):
            raise BrowserCaptcha(message["error"])
        if message.get("skipped"):
            raise BrowserSkipped(message["error"])
        if "error" in message:
            raise RuntimeError(message["error"])
        return message["results"]

    def close(self):
        with self.lock:
            proc, writes, self.proc = self.proc, self.writes, None
        if writes is not None:
            writes.put(None)
        if proc is not None:
            self._kill(proc)
