"""Windows toast with buttons, no dependency: PowerShell 5.1 (always present) shows it through the WinRT toast API and
each button is a protocol link that Windows hands to `notice_action.py`.

An unpackaged app has no COM activator, so a button cannot call back into a running process. The protocol route needs
only HKCU keys (no admin): one AppUserModelId key (name and icon of the toast) and one URL protocol whose command runs
`pythonw notice_action.py "<uri>"` without a window. Actions are therefore small independent commands that read and
write the config file, never state held by the tray.
"""
import base64
import os
import subprocess
import sys
import urllib.parse
from xml.sax.saxutils import quoteattr, escape

APP_ID = "SmartTool.Notifications"
PROTOCOL = "smart-tool-action"
ACTION_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "notice_action.py")

_SHOW_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml($env:SMART_TOOL_TOAST_XML)
$toast = New-Object Windows.UI.Notifications.ToastNotification $xml
if ($env:SMART_TOOL_TOAST_TAG) { $toast.Tag = $env:SMART_TOOL_TOAST_TAG; $toast.Group = 'smart-tool' }
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($env:SMART_TOOL_TOAST_APPID).Show($toast)
"""


def _pythonw():
    candidate = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return candidate if os.path.isfile(candidate) else sys.executable


def action_command():
    """The command the protocol runs; `%1` is the clicked link."""
    return f'"{_pythonw()}" "{ACTION_SCRIPT}" "%1"'


def register():
    """Writes the HKCU keys (idempotent). Raises on any failure: a toast whose buttons do nothing is worse than none."""
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, rf"Software\Classes\AppUserModelId\{APP_ID}") as key:
        winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, "Smart Tool")
    root = rf"Software\Classes\{PROTOCOL}"
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, root) as key:
        winreg.SetValueEx(key, None, 0, winreg.REG_SZ, "URL:Smart Tool action")
        winreg.SetValueEx(key, "URL Protocol", 0, winreg.REG_SZ, "")
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, root + r"\shell\open\command") as key:
        winreg.SetValueEx(key, None, 0, winreg.REG_SZ, action_command())


def unregister():
    """Deletes the HKCU keys `register` wrote, deepest first; returns how many existed (missing keys are not an error)."""
    import winreg
    count = 0
    for path in (rf"Software\Classes\{PROTOCOL}\shell\open\command", rf"Software\Classes\{PROTOCOL}\shell\open",
                 rf"Software\Classes\{PROTOCOL}\shell", rf"Software\Classes\{PROTOCOL}",
                 rf"Software\Classes\AppUserModelId\{APP_ID}"):
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
            count += 1
        except FileNotFoundError:
            pass
    return count


def action_uri(action, **params):
    """The protocol link a button carries: `smart-tool-action:<action>?<params>`; returns the link as text."""
    query = urllib.parse.urlencode(params)
    return f"{PROTOCOL}:{action}" + (f"?{query}" if query else "")


def toast_xml(title, lines, buttons):
    """`buttons` is a list of (label, uri); each one launches its protocol link and closes the toast."""
    text = "".join(f"<text>{escape(line)}</text>" for line in [title, *lines])
    actions = "".join(
        f'<action content={quoteattr(label)} arguments={quoteattr(uri)} activationType="protocol"/>'
        for label, uri in buttons)
    return (f'<toast><visual><binding template="ToastGeneric">{text}</binding></visual>'
            f'<actions>{actions}</actions></toast>')


def show(title, lines, buttons, tag=None):
    """Shows the toast; raises RuntimeError with PowerShell's message when Windows refuses it."""
    if sys.platform != "win32":
        raise RuntimeError("Toast notifications are only available on Windows.")
    register()
    script = base64.b64encode(_SHOW_SCRIPT.encode("utf-16-le")).decode("ascii")
    env = {**os.environ, "SMART_TOOL_TOAST_XML": toast_xml(title, lines, buttons),
           "SMART_TOOL_TOAST_APPID": APP_ID, "SMART_TOOL_TOAST_TAG": tag or ""}
    done = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", script],
        env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        creationflags=subprocess.CREATE_NO_WINDOW)
    if done.returncode:
        raise RuntimeError(f"Windows refused the notification: {(done.stderr or done.stdout).strip()[:500]}")
