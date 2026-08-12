"""Install / uninstall J.A.R.V.I.S autostart on Windows.

This script creates a per-user Run registry entry pointing to a small
launcher batch that invokes the project's main.py using the current Python
interpreter. Run with administrative privileges if you want a machine-wide
install (not implemented here).

Usage:
  python scripts/install_autostart.py install
  python scripts/install_autostart.py uninstall

It writes a launcher to %APPDATA%\Jarvis\run_jarvis.bat and a registry key
HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run\\Jarvis -> "C:\\Path\\to\\run_jarvis.bat"
"""
from __future__ import annotations
import os
import sys
from pathlib import Path
try:
    import winreg
except Exception:
    winreg = None

APP_NAME = "Jarvis"
RUN_KEY = r"Software\\Microsoft\\Windows\\CurrentVersion\\Run"

def _launcher_path() -> Path:
    appdata = os.getenv("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    d = Path(appdata) / "Jarvis"
    d.mkdir(parents=True, exist_ok=True)
    return d / "run_jarvis.bat"

def install():
    if winreg is None:
        print("winreg module not available — autostart is Windows-only and requires the standard library winreg module.")
        return 1

    python = sys.executable
    project_root = Path(__file__).resolve().parents[2]
    main_py = project_root / "main.py"
    if not main_py.exists():
        print(f"Error: main.py not found at {main_py}")
        return 1

    launcher = _launcher_path()
    # Use pythonw so the console doesn't pop up on login
    cmd = f'"{python}" "{main_py}"'
    content = f"@echo off\n{cmd}\n"
    launcher.write_text(content, encoding="utf-8")
    print(f"Wrote launcher to {launcher}")

    # Set HKCU Run key
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, str(launcher))
        print(f"Autostart installed (HKCU Run -> {launcher})")
    except Exception as e:
        print(f"Failed to write registry key: {e}")
        return 1
    return 0

def uninstall():
    if winreg is None:
        print("winreg module not available — autostart is Windows-only and requires the standard library winreg module.")
        return 1

    launcher = _launcher_path()
    # Remove registry entry
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, APP_NAME)
        print("Removed registry autostart entry")
    except FileNotFoundError:
        print("Registry autostart entry not found")
    except Exception as e:
        print(f"Failed to remove registry entry: {e}")
    # Remove launcher file
    try:
        if launcher.exists():
            launcher.unlink()
            print(f"Removed launcher {launcher}")
    except Exception as e:
        print(f"Failed to remove launcher: {e}")
    return 0

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    cmd = sys.argv[1].lower()
    if cmd == 'install':
        sys.exit(install())
    elif cmd == 'uninstall':
        sys.exit(uninstall())
    else:
        print(__doc__)
        sys.exit(1)
