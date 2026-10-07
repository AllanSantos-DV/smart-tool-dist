"""Newer Smart Tool release on npm, for the tray notice. Updating is running the install command again."""
import json
import urllib.request

import version

PACKAGE = "@allansantos-dev/smart-tool"
REGISTRY_URL = "https://registry.npmjs.org/@allansantos-dev%2Fsmart-tool/latest"
UPDATE_COMMAND = f"npx {PACKAGE}@latest install"


def _parts(value):
    return tuple(int(part) for part in str(value).split("-")[0].split("."))


def newer_version(timeout=10):
    """The latest version on npm when it is newer than the installed one, else None."""
    request = urllib.request.Request(REGISTRY_URL, headers={"User-Agent": version.user_agent(),
                                                            "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        latest = json.load(response)["version"]
    return latest if _parts(latest) > _parts(version.VERSION) else None
