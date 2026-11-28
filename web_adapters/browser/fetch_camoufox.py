"""`camoufox fetch` for the installer. camoufox 0.5.6 prints a failed download and still exits 0, so the error is
caught here and turned into a failing exit, and the browser executable must exist afterwards. The 1.3 GB download
goes through ranged_download (Range requests that resume after a dropped or stalled connection; camoufox still checks
the sha256), its other requests get a timeout (requests passes None, which also overrides socket.setdefaulttimeout),
and progress is printed as plain lines (the rich bar stays invisible in a pipe). The download has a hard time limit,
and its lifecycle (start, progress, completion, failure) goes to `browser-fetch.log`, the Windows notification and the
state file the setup audit reads. A release lookup that fails (camoufox prints it and goes on) counts as a failed refresh. A failed refresh while a browser build is already on disk keeps that build and ends
as "degraded"; with no build it fails. requests trusts only certifi's bundle, so on
a network that inspects TLS with a corporate root CA its release lookups fail; the Windows certificate store is used
instead (truststore), unless the user already set a CA bundle variable."""
import os
import sys
import time
from io import BytesIO
from pathlib import Path

import camoufox.__main__ as camoufox_cli
import requests
from camoufox import pkgman
from camoufox.exceptions import UnsupportedVersion

sys.path.append(str(Path(__file__).resolve().parents[2]))
import browser_fetch_state as fetch_state
import i18n
import ranged_download
import toast_notice

TIMEOUT_S = (30, 60)
TOTAL_TIMEOUT_S = 45 * 60
CA_VARIABLES = ("REQUESTS_CA_BUNDLE", "SSL_CERT_FILE", "CURL_CA_BUNDLE")
REPORT_EVERY_MB = 50
_update = camoufox_cli.CamoufoxUpdate.update
_list_versions = camoufox_cli.list_available_versions
_get = requests.get
failures = []
_download = {"started_at": None, "deadline": None}


def _notify(tag, title, lines):
    """Windows toast for a download event; a refused toast is logged and never stops the download."""
    try:
        toast_notice.show(title, lines, [], tag=tag)
    except Exception as exc:
        fetch_state.log("notification_failed", tag=tag, error=f"{type(exc).__name__}: {exc}")


def _reporter():
    reported = {"mb": -REPORT_EVERY_MB}

    def report(done, total):
        mb = done // 1048576
        if mb - reported["mb"] >= REPORT_EVERY_MB or done == total:
            reported["mb"] = mb
            fetch_state.log("progress", mb=mb, of_mb=total // 1048576)
            print(f"downloaded {mb} of {total // 1048576} MB", flush=True)

    return report


def _ranged_webdl(url, desc=None, buffer=None, bar=True, progress_callback=None):
    buffer = BytesIO() if buffer is None else buffer
    if _download["started_at"] is None:
        _download["started_at"] = time.monotonic()
        _download["deadline"] = _download["started_at"] + TOTAL_TIMEOUT_S
        fetch_state.log("start", url=url, limit_minutes=TOTAL_TIMEOUT_S // 60)
        _notify("browser-fetch-start", i18n.t("Smart Tool is downloading its browser"),
                [i18n.t("About 1.3 GB, in the background; nothing to do.")])
    ranged_download.download(url, buffer, progress=progress_callback or _reporter(), deadline=_download["deadline"])
    return buffer


def _get_with_timeout(*args, **kwargs):
    kwargs.setdefault("timeout", TIMEOUT_S)
    return _get(*args, **kwargs)


def _recording_lookup(*args, **kwargs):
    """The release lookup of `camoufox sync` catches and prints its own errors; recording the failure of the repository
    that `camoufox fetch` installs from by default lets the refresh end as degraded instead of looking like a run that found
    nothing newer. A failure of the other repositories does not matter to the build that gets installed."""
    try:
        return _list_versions(*args, **kwargs)
    except Exception as exc:
        name = getattr(kwargs.get("repo_config"), "name", None)
        if name is None or name == camoufox_cli.RepoConfig.get_default_name():
            failures.append(exc)
        raise


def _recording_update(self, *args, **kwargs):
    try:
        return _update(self, *args, **kwargs)
    except Exception as exc:
        failures.append(exc)
        raise


def installed_executable():
    """The browser executable, without the download that launch_path() starts when it is missing. A build the library
    no longer supports counts as missing: it cannot be launched and a fetch replaces it."""
    try:
        executable = Path(pkgman.camoufox_path(download_if_missing=False)) / pkgman.LAUNCH_FILE[pkgman.OS_NAME]
    except UnsupportedVersion as exc:
        raise FileNotFoundError(f"the build on disk is not supported ({exc})") from exc
    if not executable.is_file():
        raise FileNotFoundError(executable)
    return executable


def _installed_or_none():
    try:
        return installed_executable()
    except FileNotFoundError:
        return None


def require_installed():
    """The browser executable for the resident browser, which must never start a 1.3 GB download of its own: without
    a copy on disk it records a failed state and ends with the step to take."""
    try:
        return installed_executable()
    except FileNotFoundError as exc:
        reason = f"the browser is not on disk ({exc})"
        next_action = i18n.t("Run the Smart Tool update with a network connection: it downloads the browser.")
        fetch_state.write_state("failed", reason, next_action)
        fetch_state.log("failed", reason=reason, next=next_action)
        sys.exit(f"Camoufox browser not on disk: {exc}. {next_action}")


def _report_failure(exc):
    """Ends a failed fetch: with a build already on disk it keeps it (state "degraded", returns); with none it exits
    failing. Either way the reason and the next step go to the log, the state file and a notification."""
    reason = f"{type(exc).__name__}: {exc}"
    copy = _installed_or_none()
    if copy:
        build = pkgman.installed_verstr()
        next_action = i18n.t("The browser already on disk is used; the next update looks for a newer one again.")
        fetch_state.write_state("degraded", reason, next_action, build)
        fetch_state.log("degraded", reason=reason, build=build, next=next_action)
        _notify("browser-fetch-degraded", i18n.t("Smart Tool kept its current browser"), [reason[:160], next_action])
        print(f"Camoufox not refreshed ({reason}); using {build}: {copy}", flush=True)
        return
    next_action = i18n.t("Check the internet connection and run the Smart Tool update again; web searches that need "
                         "the browser stay off until then.")
    fetch_state.write_state("failed", reason, next_action)
    fetch_state.log("failed", reason=reason, next=next_action)
    _notify("browser-fetch-failed", i18n.t("Smart Tool could not download its browser"), [reason[:160], next_action])
    sys.exit(f"Camoufox download failed: {reason}")


def trust_system_certificates():
    """Makes ssl (and so requests) verify against the Windows certificate store; False and untouched when the user set
    REQUESTS_CA_BUNDLE, SSL_CERT_FILE or CURL_CA_BUNDLE."""
    if any(os.environ.get(name) for name in CA_VARIABLES):
        return False
    import truststore
    truststore.inject_into_ssl()
    return True


def main():
    trust_system_certificates()
    requests.get = _get_with_timeout
    pkgman.webdl = _ranged_webdl
    camoufox_cli.CamoufoxUpdate.update = _recording_update
    camoufox_cli.list_available_versions = _recording_lookup
    fetch_state.log("check", copy=str(_installed_or_none() or "none"))
    camoufox_cli.cli.main(["fetch"], standalone_mode=False)
    if failures:
        return _report_failure(failures[-1])
    try:
        executable = installed_executable()
    except Exception as exc:
        _report_failure(RuntimeError(f"Camoufox is not installed after the download: {exc}"))
        sys.exit(f"Camoufox is not installed after the download: {exc}")
    build = pkgman.installed_verstr()
    fetch_state.write_state("ok", build=build)
    if _download["started_at"] is not None:
        seconds = round(time.monotonic() - _download["started_at"])
        fetch_state.log("done", build=build, seconds=seconds)
        _notify("browser-fetch-done", i18n.t("Smart Tool browser is ready"), [build])
    print(f"Camoufox {build} ready: {executable}", flush=True)


if __name__ == "__main__":
    main()
