"""
Запускалка Book Tutor. Из неё собирается BookTutor.exe на рабочем столе.

    pip install pyinstaller
    pyinstaller --noconfirm --onefile --noconsole --name BookTutor --distpath . launcher.py

Двойной щелчок открывает окно читалки. Браузер и адресная строка не используются.
Если сервер ещё не запущен — стартует server.py --web-only тем Python, где стоят mcp и pymupdf.
"""
import ctypes
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

from app_window import open_app_window

PORT = 8765

# Папка, где лежат server.py, web и books.
# Exe на рабочем столе сам по себе этих файлов не содержит и смотрит сюда.
PROJECT_DIR = Path(r"C:\Users\user\PycharmProjects\Booktutor")
KNOWN_PYTHON = Path(r"C:\Users\user\PycharmProjects\Learn\.venv\Scripts\python.exe")

if getattr(sys, "frozen", False):
    HERE = Path(sys.executable).resolve().parent
else:
    HERE = Path(__file__).resolve().parent


def app_dir() -> Path:
    if (HERE / "server.py").is_file():
        return HERE
    if (PROJECT_DIR / "server.py").is_file():
        return PROJECT_DIR
    return HERE


def message(text: str):
    """Окно с сообщением (без консоли, т.к. exe собирается с --noconsole)."""
    if os.name == "nt":
        ctypes.windll.user32.MessageBoxW(None, text, "Book Tutor", 0x40)
    else:
        print(text)


def reader_running() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=0.5):
            return True
    except OSError:
        return False


def find_python(home: Path) -> str | None:
    # 1. python_path.txt — его пишет install.py
    f = home / "python_path.txt"
    if f.exists():
        p = f.read_text(encoding="utf-8").strip()
        if Path(p).is_file():
            return p
    # 2. настройки Claude Desktop — там прописан тот же Python
    cfg = Path(os.environ.get("APPDATA", "")) / "Claude" / "claude_desktop_config.json"
    try:
        p = json.loads(cfg.read_text(encoding="utf-8"))["mcpServers"]["book-tutor"]["command"]
        if Path(p).is_file():
            return p
    except (OSError, KeyError, ValueError):
        pass
    # 3. интерпретатор, в котором уже стоят mcp и pymupdf
    if KNOWN_PYTHON.is_file():
        return str(KNOWN_PYTHON)
    # 4. если запускаешь launcher.py из PyCharm — текущий Python
    if not getattr(sys, "frozen", False):
        return sys.executable
    return None


def main():
    home = app_dir()
    server = home / "server.py"

    if reader_running():
        if not open_app_window(PORT):
            message("Не удалось открыть окно Book Tutor.\nНужен Microsoft Edge.")
        return

    if not server.is_file():
        message("Не найдены файлы Book Tutor.\nОжидалась папка:\n" + str(PROJECT_DIR))
        return

    python = find_python(home)
    if not python:
        message("Не найден Python с библиотеками.\nЗапусти один раз install.py.")
        return

    log_path = home / "booktutor.log"
    creationflags = 0
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    if os.name == "nt":
        creationflags = 0x08000000  # CREATE_NO_WINDOW: сервер работает без консоли

    log_file = open(log_path, "ab")
    proc = subprocess.Popen(
        [python, str(server), "--web-only", "--no-window"],
        cwd=home,
        env=env,
        stdout=log_file,
        stderr=log_file,
        creationflags=creationflags,
    )

    for _ in range(60):
        if reader_running():
            if not open_app_window(PORT):
                message("Читалка запущена, но окно не открылось.\nНужен Microsoft Edge.")
            return
        if proc.poll() is not None:
            err = ""
            try:
                err = log_path.read_text(encoding="utf-8", errors="replace")[-800:]
            except OSError:
                pass
            message("Сервер читалки сразу закрылся.\n\n" + (err or "Подробности в booktutor.log"))
            return
        time.sleep(0.25)
    message("Читалка долго не открывается. Посмотри booktutor.log в папке программы.")


if __name__ == "__main__":
    main()
