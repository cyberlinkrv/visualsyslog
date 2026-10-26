from __future__ import annotations

import os
import sys
from pathlib import Path

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "VisualSyslog"


def is_windows_autostart_supported() -> bool:
    return os.name == "nt"


def windows_autostart_enabled() -> bool:
    if not is_windows_autostart_supported():
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, VALUE_NAME)
            return True
    except FileNotFoundError:
        return False


def set_windows_autostart(enabled: bool) -> None:
    if not is_windows_autostart_supported():
        raise OSError("A inicialização automática está disponível somente no Windows.")
    import winreg

    app_data = Path(os.environ.get("APPDATA", Path.home())) / "VisualSyslog"
    launcher = app_data / "startup.vbs"
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        if enabled:
            app_data.mkdir(parents=True, exist_ok=True)
            if getattr(sys, "frozen", False):
                program = Path(sys.executable)
                command = f'"{program}" --no-browser'
            else:
                pythonw = Path(sys.executable).with_name("pythonw.exe")
                program = pythonw if pythonw.exists() else Path(sys.executable)
                command = f'"{program}" -m visualsyslog --no-browser'
            vbs_command = command.replace('"', '""')
            launcher.write_text(
                'Set shell = CreateObject("WScript.Shell")\n'
                f'shell.Run "{vbs_command}", 0, False\n',
                encoding="utf-8",
            )
            windows_dir = Path(os.environ.get("WINDIR", "C:\\Windows"))
            wscript = windows_dir / "System32" / "wscript.exe"
            startup_command = f'"{wscript}" //B //Nologo "{launcher}"'
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, startup_command)
        else:
            try:
                winreg.DeleteValue(key, VALUE_NAME)
            except FileNotFoundError:
                pass
            try:
                launcher.unlink(missing_ok=True)
            except OSError:
                pass
