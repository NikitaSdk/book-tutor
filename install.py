"""
Установка Book Tutor. Запусти один раз:  python install.py

1. Ставит нужные библиотеки (mcp, pymupdf).
2. Прописывает Book Tutor в настройки приложения Claude Desktop.
После этого перезапусти Claude Desktop полностью (через значок в трее → Quit).
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVER = HERE / "server.py"

print("1/2  Устанавливаю библиотеки…")
subprocess.check_call([sys.executable, "-m", "pip", "install", "--upgrade", "mcp", "pymupdf"])

entry = {"command": sys.executable, "args": [str(SERVER)]}
(HERE / "python_path.txt").write_text(sys.executable, encoding="utf-8")  # для BookTutor.exe


candidates = []
if os.name == "nt":
    appdata = Path(os.environ.get("APPDATA", ""))
    candidates.append(appdata / "Claude" / "claude_desktop_config.json")
    # версия Claude из Microsoft Store хранит настройки в другом месте
    local = Path(os.environ.get("LOCALAPPDATA", ""))
    for pkg in (local / "Packages").glob("Claude_*"):
        candidates.append(pkg / "LocalCache" / "Roaming" / "Claude" / "claude_desktop_config.json")
elif sys.platform == "darwin":
    candidates.append(Path.home() / "Library/Application Support/Claude/claude_desktop_config.json")
else:
    candidates.append(Path.home() / ".config/Claude/claude_desktop_config.json")

existing = [c for c in candidates if c.parent.exists()]
targets = existing or candidates[:1]

print("2/2  Подключаю к Claude Desktop…")
for cfg in targets:
    cfg.parent.mkdir(parents=True, exist_ok=True)
    data = {}
    if cfg.exists():
        shutil.copy(cfg, cfg.with_suffix(".json.bak"))
        try:
            data = json.loads(cfg.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError:
            print(f"   ! {cfg} повреждён, копия сохранена как .bak — создаю заново")
    data.setdefault("mcpServers", {})["book-tutor"] = entry
    cfg.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"   записано: {cfg}")

print(f"""
Готово!
  • Перезапусти Claude Desktop полностью (значок в трее → Quit, потом открой снова).
  • Клади PDF-книги в папку: {HERE / 'books'}
  • Читалка открывается программой BookTutor.exe на рабочем столе.
  • В чате Claude напиши: «открой читалку».

Если Claude не видит Book Tutor: Settings → Developer → Edit Config
и проверь, что там есть блок "book-tutor":
{json.dumps({"mcpServers": {"book-tutor": entry}}, ensure_ascii=False, indent=2)}
""")
input("Нажми Enter, чтобы закрыть…")
