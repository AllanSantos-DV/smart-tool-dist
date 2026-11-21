#!/usr/bin/env python3
"""Autostart do Smart Tool no logon do Windows — Tarefa Agendada por usuário (sem
admin). `schtasks /Create` pode ser bloqueado por política de grupo corporativa
(máquina AD-joined); nesse caso cai automaticamente para um script na pasta Startup
do usuário. Os dois rodam o mesmo VBScript, que sobe o `tray.py` no Python base com
a venv (`venv_launch`): a Tarefa Agendada não passa variável de ambiente, e o
redirecionador da venv deixaria dois processos por bandeja. O loop de polling do
próprio `tray.py` cobre "se o daemon cair, sobe de novo".
"""
import os
import subprocess
import sys
import tempfile

import paths
import toast_notice
import venv_launch

TASK_NAME = os.environ.get("SMART_TOOL_TASK_NAME", "SmartTool-Tray")
# The tray runs under pythonw (no console): schtasks without this flag flashed a console each time its menu was drawn.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class AutostartError(Exception):
    """Mensagem já pronta pra exibir ao usuário — sem stack trace nem caminho interno."""


def _xml_escape(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _tool_dir():
    return os.path.dirname(os.path.abspath(__file__))


def _tray_path():
    return os.path.join(_tool_dir(), "tray.py")


def _pythonw_path():
    exe = sys.executable
    base, name = os.path.split(exe)
    if name.lower() == "python.exe":
        candidate = os.path.join(base, "pythonw.exe")
        if os.path.isfile(candidate):
            return candidate
    return exe


def _current_user_id():
    domain = os.environ.get("USERDOMAIN", "")
    user = os.environ.get("USERNAME", "")
    return f"{domain}\\{user}" if domain else user


def _task_xml():
    user_id = _xml_escape(_current_user_id())
    wscript = _xml_escape(_wscript_path())
    script = _xml_escape(_write_launch_script(_task_script_path()))
    workdir = _xml_escape(_tool_dir())
    return (
        '<?xml version="1.0" encoding="UTF-16"?>\r\n'
        '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\r\n'
        '  <RegistrationInfo>\r\n'
        '    <Description>Smart Tool - MCP daemon + tray icon, starts at user sign-in.</Description>\r\n'
        '  </RegistrationInfo>\r\n'
        '  <Triggers>\r\n'
        '    <LogonTrigger>\r\n'
        '      <Enabled>true</Enabled>\r\n'
        f'      <UserId>{user_id}</UserId>\r\n'
        '    </LogonTrigger>\r\n'
        '  </Triggers>\r\n'
        '  <Principals>\r\n'
        '    <Principal id="Author">\r\n'
        f'      <UserId>{user_id}</UserId>\r\n'
        '      <LogonType>InteractiveToken</LogonType>\r\n'
        '      <RunLevel>LeastPrivilege</RunLevel>\r\n'
        '    </Principal>\r\n'
        '  </Principals>\r\n'
        '  <Settings>\r\n'
        '    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\r\n'
        '    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\r\n'
        '    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\r\n'
        '    <AllowHardTerminate>true</AllowHardTerminate>\r\n'
        '    <StartWhenAvailable>true</StartWhenAvailable>\r\n'
        '    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>\r\n'
        '    <Enabled>true</Enabled>\r\n'
        '    <Hidden>false</Hidden>\r\n'
        '    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>\r\n'
        '  </Settings>\r\n'
        '  <Actions Context="Author">\r\n'
        '    <Exec>\r\n'
        f'      <Command>{wscript}</Command>\r\n'
        f'      <Arguments>//B //NoLogo "{script}"</Arguments>\r\n'
        f'      <WorkingDirectory>{workdir}</WorkingDirectory>\r\n'
        '    </Exec>\r\n'
        '  </Actions>\r\n'
        '</Task>\r\n'
    )


def _startup_dir():
    appdata = os.environ.get("APPDATA", "")
    if not appdata:
        raise AutostartError("APPDATA environment variable not set; could not find the user Startup folder.")
    return os.path.join(appdata, "Microsoft", "Windows", "Start Menu", "Programs", "Startup")


def _startup_script_path():
    return os.path.join(_startup_dir(), "SmartTool-Tray.vbs")


def _task_script_path():
    return os.path.join(paths.DATA_DIR, "tray-start.vbs")


def _wscript_path():
    return os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "wscript.exe")


def _write_launch_script(path):
    """Writes the VBScript that starts the tray hidden in one process and returns its path."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-16") as f:
        f.write(venv_launch.hidden_run_vbs(_pythonw_path(), _tray_path(), _tool_dir()))
    return path


def _install_startup_shortcut():
    _write_launch_script(_startup_script_path())
    return {"ok": True, "method": "startup_folder"}


def _remove_startup_shortcut():
    try:
        os.remove(_startup_script_path())
        return True
    except OSError:
        return False


def _task_installed():
    try:
        result = subprocess.run(
            ["schtasks", "/Query", "/TN", TASK_NAME],
            capture_output=True, text=True, timeout=20, creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def install():
    """Tenta a Tarefa Agendada primeiro; se
    `schtasks /Create` falhar por qualquer motivo (ex.: bloqueado por politica de
    grupo corporativa, `schtasks` fora do PATH, timeout), cai para o atalho de
    Startup em vez de propagar o erro."""
    fallback_reason = None
    try:
        xml = _task_xml()
        fd, tmp_path = tempfile.mkstemp(suffix=".xml")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(xml.encode("utf-16"))
            result = subprocess.run(
                ["schtasks", "/Create", "/TN", TASK_NAME, "/XML", tmp_path, "/F"],
                capture_output=True, text=True, timeout=20, creationflags=_NO_WINDOW,
            )
        finally:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        if result.returncode == 0:
            return {"ok": True, "method": "task"}
        fallback_reason = (result.stderr or result.stdout or "").strip()
    except (OSError, subprocess.SubprocessError) as exc:
        fallback_reason = str(exc)

    fallback = _install_startup_shortcut()
    fallback["fallback_reason"] = fallback_reason
    return fallback


def remove():
    removed = []
    try:
        result = subprocess.run(
            ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"],
            capture_output=True, text=True, timeout=20, creationflags=_NO_WINDOW,
        )
        if result.returncode == 0:
            removed.append("task")
    except (OSError, subprocess.SubprocessError):
        pass
    if _remove_startup_shortcut():
        removed.append("startup_folder")
    try:
        os.remove(_task_script_path())
    except FileNotFoundError:
        pass
    if sys.platform == "win32" and toast_notice.unregister():
        removed.append("toast_registry")
    return {"ok": True, "removed": removed}


def status():
    if _task_installed():
        return {"installed": True, "method": "task"}
    if os.path.isfile(_startup_script_path()):
        return {"installed": True, "method": "startup_folder"}
    return {"installed": False, "method": None}
