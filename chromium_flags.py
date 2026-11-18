"""Chromium switches carried by every automation browser launch of Smart Tool.

`WebUsbDeviceDetection` makes Chromium enumerate USB devices a few seconds after launch, which freezes a USB
microphone that is recording. Chromium keeps only the last `--disable-features` switch, and Playwright puts its own
list first and appends the caller's arguments, so a lone switch would drop Playwright's list: the switch built here
repeats the list of the Playwright bundle the browser is launched by and adds the feature to it.
"""
import re
from pathlib import Path

WEB_USB_FEATURE = "WebUsbDeviceDetection"
_FEATURE_LIST = re.compile(r"\bdisabledFeatures = \[(.*?)\]\.filter\(", re.S)
_FEATURE_NAME = re.compile(r'^\s*"([A-Za-z0-9]+)"', re.M)


def disable_features_arg(bundle):
    """The single `--disable-features` switch for a launch through the Playwright driver bundle `bundle`
    (coreBundle.js): that bundle's own list plus WebUsbDeviceDetection. RuntimeError when the list is not found, so a
    Playwright upgrade that moves it fails loudly instead of launching without the switch."""
    found = _FEATURE_LIST.search(Path(bundle).read_text(encoding="utf-8"))
    names = _FEATURE_NAME.findall(found.group(1)) if found else []
    if not names:
        raise RuntimeError(f"No disabledFeatures list in {bundle}; chromium_flags must follow this Playwright version.")
    return "--disable-features=" + ",".join([*names, WEB_USB_FEATURE])


def python_chromium_args():
    """Launch arguments for Chromium started by the Python Playwright package that is imported here."""
    import playwright
    return [disable_features_arg(Path(playwright.__file__).parent / "driver" / "package" / "lib" / "coreBundle.js")]
