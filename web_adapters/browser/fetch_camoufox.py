"""`camoufox fetch` for the installer: a stalled download fails after 60 s instead of hanging, and progress is printed
as plain lines (the rich bar stays invisible when the output is a pipe)."""
import socket

from camoufox import pkgman
from camoufox.__main__ import cli

socket.setdefaulttimeout(60)
REPORT_EVERY_MB = 50
_webdl = pkgman.webdl


def _reporting_webdl(url, desc=None, buffer=None, bar=True, progress_callback=None):
    reported = {"mb": -REPORT_EVERY_MB}

    def report(done, total):
        mb = done // 1048576
        if mb - reported["mb"] >= REPORT_EVERY_MB or done == total:
            reported["mb"] = mb
            print(f"downloaded {mb} of {total // 1048576} MB", flush=True)

    return _webdl(url, desc=desc, buffer=buffer, bar=False, progress_callback=progress_callback or report)


pkgman.webdl = _reporting_webdl

if __name__ == "__main__":
    cli(["fetch"])
