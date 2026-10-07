#!/usr/bin/env python3
"""Autostart do Smart Tool no logon do Windows — Tarefa Agendada por usuário (sem
admin), com política `RestartOnFailure` cobrindo o SO reiniciar o processo se ele
morrer. `schtasks /Create` pode ser bloqueado por política de grupo corporativa
(máquina AD-joined); nesse caso cai automaticamente para um atalho na pasta Startup
do usuário — sem a política de restart do Task Scheduler, mas o loop de polling do
próprio `tray.py` já cobre "se cair, sobe de novo" independente do mecanismo de boot.
"""
import os
import subprocess
import sys
import tempfile

TASK_NAME = os.environ.get("SMART_TOOL_TASK_NAME", "SmartTool-Tray")


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
    pythonw = _xml_escape(_pythonw_path())
    tray = _xml_escape(_tray_path())
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
        '    <RestartOnFailure>\r\n'
        '      <Interval>PT1M</Interval>\r\n'
        '      <Count>999</Count>\r\n'
        '    </RestartOnFailure>\r\n'
        '  </Settings>\r\n'
        '  <Actions Context="Author">\r\n'
        '    <Exec>\r\n'
        f'      <Command>{pythonw}</Command>\r\n'
        f'      <Arguments>"{tray}"</Arguments>\r\n'
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


def _vbs_quoted_run(pythonw, tray):
    # WshShell.Run monta a linha de comando com Chr(34) em vez de aspas literais —
    # evita ter que escapar aspas dentro de aspas tanto em Python quanto em VBScript.
    return f'Chr(34) & "{pythonw}" & Chr(34) & " " & Chr(34) & "{tray}" & Chr(34)'


def _install_startup_shortcut():
    os.makedirs(_startup_dir(), exist_ok=True)
    vbs = (
        'Set shell = CreateObject("WScript.Shell")\r\n'
        f'shell.Run {_vbs_quoted_run(_pythonw_path(), _tray_path())}, 0, False\r\n'
    )
    with open(_startup_script_path(), "w", encoding="utf-8") as f:
        f.write(vbs)
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
            capture_output=True, text=True, timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def install():
    """Tenta a Tarefa Agendada primeiro (cobre restart automático pelo SO); se
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
                capture_output=True, text=True, timeout=20,
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
            capture_output=True, text=True, timeout=20,
        )
        if result.returncode == 0:
            removed.append("task")
    except (OSError, subprocess.SubprocessError):
        pass
    if _remove_startup_shortcut():
        removed.append("startup_folder")
    return {"ok": True, "removed": removed}


def status():
    if _task_installed():
        return {"installed": True, "method": "task"}
    if os.path.isfile(_startup_script_path()):
        return {"installed": True, "method": "startup_folder"}
    return {"installed": False, "method": None}
