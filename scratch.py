"""Where Smart Tool's tests, probes and evaluation scripts keep their temporary folders.

Every run lives in `%LOCALAPPDATA%\\tmp\\smart-tool\\<run>`, never in the root of the system's temporary folder, is
removed when it ends, and a run that was killed is removed by the next one once it is older than `MAX_AGE_S`.
`SMART_TOOL_SCRATCH` replaces the base folder. Pure stdlib so any script can import it."""
import contextlib
import os
import shutil
import tempfile
import time

MAX_AGE_S = 24 * 3600
ENV_BASE = "SMART_TOOL_SCRATCH"
_swept = set()


def base():
    """The folder that holds every run of this tool, created and swept of its own leftovers once per process.

    Returns: str, the absolute path. Raises RuntimeError when neither SMART_TOOL_SCRATCH nor LOCALAPPDATA is set."""
    folder = os.environ.get(ENV_BASE)
    if not folder:
        local = os.environ.get("LOCALAPPDATA")
        if not local:
            raise RuntimeError(f"Set {ENV_BASE} or LOCALAPPDATA: there is no per-user folder for temporary runs.")
        folder = os.path.join(local, "tmp", "smart-tool")
    folder = os.path.abspath(folder)
    os.makedirs(folder, exist_ok=True)
    if folder not in _swept:
        _swept.add(folder)
        sweep(folder)
    return folder


def remove(path):
    """Deletes a folder tree including read-only files (Git objects), retrying while another process lets go of it.

    Raises OSError when the folder is still there after the retries."""
    def writable(function, target, _error):
        os.chmod(target, 0o700)
        function(target)

    for attempt in range(5):
        if os.path.isdir(path):
            shutil.rmtree(path, onerror=writable)
        if not os.path.exists(path):
            return
        time.sleep(attempt + 1)
    raise OSError(f"Could not remove {path}")


def sweep(folder, max_age_s=MAX_AGE_S):
    """Deletes the runs directly under folder that were last touched more than max_age_s ago; a run that is still in
    use is left for the next sweep.

    Returns: int, how many runs were deleted."""
    cutoff, deleted = time.time() - max_age_s, 0
    for entry in os.scandir(folder):
        if entry.stat().st_mtime >= cutoff:
            continue
        try:
            if entry.is_dir():
                remove(entry.path)
            else:
                os.remove(entry.path)
            deleted += 1
        except OSError:
            continue
    return deleted


def make(prefix):
    """Creates an empty run folder named prefix plus a random suffix under base() and returns its path."""
    return tempfile.mkdtemp(prefix=prefix, dir=base())


@contextlib.contextmanager
def run(prefix):
    """A run folder that is removed when the block ends, whether it succeeded or failed."""
    path = make(prefix)
    try:
        yield path
    finally:
        remove(path)
