"""`camoufox fetch` for the installer. camoufox 0.5.6 prints a failed download and still exits 0, so the error is
caught here and turned into a failing exit, and the browser executable must exist afterwards. Its downloads have no
timeout (requests passes None, which also overrides socket.setdefaulttimeout), so every request gets one here: a
stalled download fails after 60 s instead of hanging. Progress is printed as plain lines (the rich bar stays
invisible in a pipe)."""
import sys
from pathlib import Path

import camoufox.__main__ as camoufox_cli
import requests
from camoufox import pkgman

TIMEOUT_S = (30, 60)
REPORT_EVERY_MB = 50
_webdl = pkgman.webdl
_update = camoufox_cli.CamoufoxUpdate.update
_get = requests.get
failures = []


def _reporting_webdl(url, desc=None, buffer=None, bar=True, progress_callback=None):
    reported = {"mb": -REPORT_EVERY_MB}

    def report(done, total):
        mb = done // 1048576
        if mb - reported["mb"] >= REPORT_EVERY_MB or done == total:
            reported["mb"] = mb
            print(f"downloaded {mb} of {total // 1048576} MB", flush=True)

    return _webdl(url, desc=desc, buffer=buffer, bar=False, progress_callback=progress_callback or report)


def _get_with_timeout(*args, **kwargs):
    kwargs.setdefault("timeout", TIMEOUT_S)
    return _get(*args, **kwargs)


def _recording_update(self, *args, **kwargs):
    try:
        return _update(self, *args, **kwargs)
    except Exception as exc:
        failures.append(exc)
        raise


def installed_executable():
    """The browser executable, without the download that launch_path() starts when it is missing."""
    executable = Path(pkgman.camoufox_path(download_if_missing=False)) / pkgman.LAUNCH_FILE[pkgman.OS_NAME]
    if not executable.is_file():
        raise FileNotFoundError(executable)
    return executable


def main():
    requests.get = _get_with_timeout
    pkgman.webdl = _reporting_webdl
    camoufox_cli.CamoufoxUpdate.update = _recording_update
    camoufox_cli.cli.main(["fetch"], standalone_mode=False)
    if failures:
        sys.exit(f"Camoufox download failed: {failures[-1]}")
    try:
        executable = installed_executable()
    except Exception as exc:
        sys.exit(f"Camoufox is not installed after the download: {exc}")
    print(f"Camoufox {pkgman.installed_verstr()} ready: {executable}", flush=True)


if __name__ == "__main__":
    main()
