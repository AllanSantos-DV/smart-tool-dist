"""Child processes under a time cap and a memory cap whose whole tree dies with them."""
import os
import subprocess

_IS_WINDOWS = os.name == "nt"
JOB_OBJECT_LIMIT_JOB_MEMORY = 0x200
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
STDERR_TAIL_BYTES = 2000


def kill_on_close_job(job_memory_bytes=None):
    """A Windows job object that kills every process in it when its handle closes, and, with `job_memory_bytes`, caps
    the memory the whole tree may commit.

    Parameters: job_memory_bytes (cap for all processes of the job together, None for no cap). Returns: `(kernel32,
    job)`, the library and the job handle. Raises: OSError when the system refuses the job."""
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
    limits.basic.flags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if job_memory_bytes:
        limits.basic.flags |= JOB_OBJECT_LIMIT_JOB_MEMORY
        limits.job_memory = job_memory_bytes
    if not kernel32.SetInformationJobObject(wintypes.HANDLE(job), 9, ctypes.byref(limits), ctypes.sizeof(limits)):
        raise ctypes.WinError(ctypes.get_last_error())
    return kernel32, job


def _stderr_tail(path):
    with open(path, "rb") as stream:
        stream.seek(0, os.SEEK_END)
        stream.seek(max(0, stream.tell() - STDERR_TAIL_BYTES))
        return stream.read().decode("utf-8", "replace").strip()


def run_capped(argv, workdir, timeout, memory_bytes):
    """Runs `argv` in `workdir` with no window, no stdin, its output discarded and its error stream kept in a file there.
    At the deadline the whole process tree is killed; a process that passes `memory_bytes` is killed by the system
    (Windows job memory limit; elsewhere an address-space limit).

    Parameters: argv, workdir (an existing folder), timeout (seconds), memory_bytes (cap for the tree). Returns:
    `(returncode, stderr_tail)`, the tail being the last 2,000 bytes of the error stream as text. Raises:
    subprocess.TimeoutExpired after killing the tree; OSError when the program cannot start."""
    error_path = os.path.join(workdir, "stderr.txt")
    with open(error_path, "wb") as errors:
        if _IS_WINDOWS:
            returncode = _run_in_job(argv, workdir, timeout, memory_bytes, errors)
        else:
            returncode = _run_in_group(argv, workdir, timeout, memory_bytes, errors)
    return returncode, _stderr_tail(error_path)


def _run_in_job(argv, workdir, timeout, memory_bytes, errors):
    import ctypes
    from ctypes import wintypes
    kernel32, job = kill_on_close_job(memory_bytes)
    try:
        proc = subprocess.Popen(argv, cwd=workdir, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=errors,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        if not kernel32.AssignProcessToJobObject(wintypes.HANDLE(job), wintypes.HANDLE(proc._handle)):
            proc.kill()
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return proc.wait(timeout)
        except subprocess.TimeoutExpired:
            kernel32.TerminateJobObject(wintypes.HANDLE(job), 1)
            proc.wait()
            raise
    finally:
        kernel32.CloseHandle(wintypes.HANDLE(job))


def _run_in_group(argv, workdir, timeout, memory_bytes, errors):
    import resource
    import signal

    def limit_memory():
        resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))

    proc = subprocess.Popen(argv, cwd=workdir, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=errors,
                            start_new_session=True, preexec_fn=limit_memory)
    try:
        return proc.wait(timeout)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
        raise
