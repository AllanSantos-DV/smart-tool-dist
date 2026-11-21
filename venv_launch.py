"""Starts a Python of this installation's venv in one process.

On Windows a venv's ``Scripts\\python.exe`` and ``pythonw.exe`` are a redirector that starts the base interpreter as a
child and waits for it (cpython issue 161511, declined upstream), so every process of the product showed up twice. The
base interpreter started with ``__PYVENV_LAUNCHER__`` naming the venv's executable runs inside the venv by itself:
``sys.prefix`` and ``sys.executable`` are the venv's, and Python clears the variable at startup so children do not
inherit it. It is how ``multiprocessing`` starts its workers from a venv (bpo-35797).
"""
import os
from pathlib import Path

LAUNCHER_VARIABLE = "__PYVENV_LAUNCHER__"


class VenvLaunchError(RuntimeError):
    """The venv names a base interpreter that is not on disk."""


def direct(executable):
    """Interpreter and environment additions that run a venv's executable without its redirector.

    Args:
        executable: path of a venv's ``python.exe`` or ``pythonw.exe`` (or of any other interpreter).

    Returns:
        ``(interpreter, additions)``: the base interpreter of the same name, read from the ``home`` line of the venv's
        ``pyvenv.cfg``, and ``{LAUNCHER_VARIABLE: executable}``. Outside Windows, or for an interpreter that is not in
        a venv, the executable itself and no additions.

    Raises:
        VenvLaunchError: the venv's ``pyvenv.cfg`` names no ``home`` or a base interpreter that does not exist.
    """
    executable = Path(executable)
    config = executable.parent.parent / "pyvenv.cfg"
    if os.name != "nt" or not config.is_file():
        return str(executable), {}
    settings = dict(line.split("=", 1) for line in config.read_text(encoding="utf-8").splitlines() if "=" in line)
    home = next((value.strip() for key, value in settings.items() if key.strip().lower() == "home"), "")
    base = Path(home) / executable.name
    if not home or not base.is_file():
        raise VenvLaunchError(f"The base Python of {config.parent} is missing ({base}); reinstall Smart Tool.")
    return str(base), {LAUNCHER_VARIABLE: str(executable)}


def environment(additions):
    """The environment for a child that gets ``additions``: the current one plus them, or None (inherit) when empty."""
    return {**os.environ, **additions} if additions else None


def hidden_run_vbs(executable, script, workdir=None):
    """VBScript that starts ``executable script`` hidden and without waiting, in one process.

    Args:
        executable: the venv's interpreter to run (see ``direct``).
        script: the Python file to run.
        workdir: working directory of the child, or None to keep the script host's.

    Returns:
        The script text. WScript.Shell sets the variable in its own process environment, which ``Run`` passes on.
    """
    interpreter, additions = direct(executable)
    lines = ['Set shell = CreateObject("WScript.Shell")']
    lines += [f'shell.Environment("Process")("{name}") = "{value}"' for name, value in additions.items()]
    if workdir:
        lines.append(f'shell.CurrentDirectory = "{workdir}"')
    lines.append(f'shell.Run Chr(34) & "{interpreter}" & Chr(34) & " " & Chr(34) & "{script}" & Chr(34), 0, False')
    return "\r\n".join(lines) + "\r\n"
