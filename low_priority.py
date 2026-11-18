"""Lowers the scheduling priority of the calling thread for a block, so a long index build on a machine without a GPU
leaves the processor to the user's own programs. Only Windows has a per-thread priority that can be put back; on any
other system the block runs unchanged (`current()` returns None)."""
import contextlib
import sys

BELOW_NORMAL = -1


def _kernel():
    import ctypes
    kernel = ctypes.windll.kernel32
    kernel.GetCurrentThread.restype = ctypes.c_void_p
    kernel.GetThreadPriority.argtypes = [ctypes.c_void_p]
    kernel.SetThreadPriority.argtypes = [ctypes.c_void_p, ctypes.c_int]
    return kernel


def current():
    """Priority of the calling thread (0 is normal, -1 below normal); None where it cannot be read."""
    if sys.platform != "win32":
        return None
    kernel = _kernel()
    return kernel.GetThreadPriority(kernel.GetCurrentThread())


@contextlib.contextmanager
def background():
    """Runs the block with the calling thread below normal priority and restores the previous one afterwards."""
    if sys.platform != "win32":
        yield
        return
    kernel = _kernel()
    thread = kernel.GetCurrentThread()
    previous = kernel.GetThreadPriority(thread)
    if not kernel.SetThreadPriority(thread, BELOW_NORMAL):
        raise OSError("SetThreadPriority failed; the index build cannot run at low priority.")
    try:
        yield
    finally:
        kernel.SetThreadPriority(thread, previous)
