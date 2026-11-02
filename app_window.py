"""Окно Book Tutor: отдельное окно программы, без вкладок и адресной строки."""

import os
import subprocess
from pathlib import Path


def edge_executable() -> Path | None:
    candidates = [
        Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"))
        / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
        / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        Path(os.environ.get("LOCALAPPDATA", ""))
        / "Microsoft" / "Edge" / "Application" / "msedge.exe",
    ]
    for path in candidates:
        if path.is_file():
            return path
    return None


def open_app_window(port: int) -> bool:
    """Открывает читалку в своём окне. Адрес пользователю не показывается."""
    edge = edge_executable()
    if edge is None:
        return False
    profile = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "BookTutor" / "window"
    profile.mkdir(parents=True, exist_ok=True)
    subprocess.Popen(
        [
            str(edge),
            f"--app=http://127.0.0.1:{port}/",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return True
