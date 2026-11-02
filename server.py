"""
Book Tutor — читалка PDF, связанная с Claude через MCP.

Этот файл запускает приложение Claude Desktop (вручную запускать не нужно).
Он делает две вещи:
  1. MCP-сервер: даёт Claude инструменты (читать главы, искать по книге,
     сохранять заметки, создавать тесты, проверять ответы).
  2. Окно читалки Book Tutor (его открывает BookTutor.exe на рабочем столе).

Обе части работают с одной базой library.db, поэтому то, что делает Claude,
сразу видно в читалке, а то, что ты выделяешь в читалке, видит Claude.
"""

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app_window import open_app_window
from pathlib import Path

import pymupdf  # PyMuPDF

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

# mcp 2.x: FastMCP переименован в MCPServer.
# В 1.x ToolError лежал в пакете mcp.server.fastmcp; в 2.x этот путь — файл-заглушка
# без модуля exceptions, поэтому запасной импорт только через importlib.
try:
    from mcp.server.mcpserver import MCPServer as FastMCP
    from mcp.server.mcpserver.exceptions import ToolError
except ModuleNotFoundError:
    import importlib

    _fastmcp = importlib.import_module("mcp.server.fastmcp")
    _errors = importlib.import_module("mcp.server.fastmcp.exceptions")
    FastMCP = _fastmcp.FastMCP
    ToolError = _errors.ToolError

BASE = Path(__file__).resolve().parent
BOOKS_DIR = Path(os.environ.get("BOOK_TUTOR_BOOKS", BASE / "books"))
WEB_DIR = BASE / "web"
DB_PATH = BASE / "library.db"
PORT = int(os.environ.get("BOOK_TUTOR_PORT", "8765"))

BOOKS_DIR.mkdir(parents=True, exist_ok=True)


def log(*args):
    # В MCP через stdio нельзя писать в stdout — только в stderr.
    print("[book-tutor]", *args, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- база данных

def db():
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with db() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS notes(
                id INTEGER PRIMARY KEY, book TEXT, chapter TEXT, page INTEGER,
                title TEXT, content TEXT, kind TEXT DEFAULT 'note',
                created REAL);
            CREATE TABLE IF NOT EXISTS highlights(
                id INTEGER PRIMARY KEY, book TEXT, page INTEGER, text TEXT,
                comment TEXT, created REAL);
            CREATE TABLE IF NOT EXISTS read_chapters(
                book TEXT, chapter_index INTEGER, created REAL,
                PRIMARY KEY(book, chapter_index));
            CREATE TABLE IF NOT EXISTS quizzes(
                id INTEGER PRIMARY KEY, book TEXT, chapter TEXT, title TEXT,
                questions TEXT, created REAL);
            CREATE TABLE IF NOT EXISTS answers(
                id INTEGER PRIMARY KEY, quiz_id INTEGER, q_index INTEGER,
                answer TEXT, correct INTEGER, feedback TEXT, created REAL,
                UNIQUE(quiz_id, q_index));
            CREATE TABLE IF NOT EXISTS state(
                key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS chat(
                id INTEGER PRIMARY KEY, book TEXT, role TEXT, text TEXT,
                status TEXT, created REAL);
            CREATE TABLE IF NOT EXISTS book_progress(
                book TEXT PRIMARY KEY, status TEXT, page INTEGER, updated REAL);
            """
        )
        cols = [r[1] for r in con.execute("PRAGMA table_info(read_chapters)")]
        if "start_page" not in cols:
            con.execute("ALTER TABLE read_chapters ADD COLUMN start_page INTEGER")
        prog_cols = [r[1] for r in con.execute("PRAGMA table_info(book_progress)")]
        if "checkpoint" not in prog_cols:
            con.execute("ALTER TABLE book_progress ADD COLUMN checkpoint INTEGER DEFAULT 0")
        if "seconds" not in prog_cols:
            con.execute("ALTER TABLE book_progress ADD COLUMN seconds INTEGER DEFAULT 0")


def set_state(key, value):
    with db() as con:
        con.execute("INSERT OR REPLACE INTO state VALUES(?,?)", (key, json.dumps(value)))


def get_state(key, default=None):
    with db() as con:
        row = con.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def bump_version():
    """Читалка следит за этим числом и обновляет панели, когда оно меняется."""
    set_state("version", time.time())


# ---------------------------------------------------------------- работа с PDF

def list_pdf_files():
    return sorted(p.name for p in BOOKS_DIR.glob("*.pdf"))


_page_cache: dict = {}


def page_count_of(path: Path) -> int:
    key = (str(path), path.stat().st_mtime)
    if key not in _page_cache:
        doc = pymupdf.open(path)
        try:
            _page_cache[key] = doc.page_count
        finally:
            doc.close()
    return _page_cache[key]


def save_progress(name: str, status: str | None = None, promote: bool = False):
    """Статус полки: unread / reading / done. Прочитанные страницы тут не меняются."""
    with db() as con:
        row = con.execute("SELECT status FROM book_progress WHERE book=?", (name,)).fetchone()
        cur_status = row["status"] if row else "unread"
        if status in ("unread", "reading", "done"):
            cur_status = status
        if promote and cur_status == "unread":
            cur_status = "reading"
        con.execute(
            "INSERT INTO book_progress(book, status, page, checkpoint, updated) VALUES(?,?,0,0,?) "
            "ON CONFLICT(book) DO UPDATE SET status=excluded.status, updated=excluded.updated",
            (name, cur_status, time.time()))
    return cur_status


def progress_of(name: str) -> dict:
    path = BOOKS_DIR / name
    total = page_count_of(path) if path.is_file() else 0
    with db() as con:
        row = con.execute("SELECT status, checkpoint FROM book_progress WHERE book=?", (name,)).fetchone()
    status = row["status"] if row else "unread"
    checkpoint = int(row["checkpoint"] or 0) if row else 0
    checkpoint = min(total, max(0, checkpoint)) if total else 0
    percent = round(100 * checkpoint / total) if total else 0
    return {"name": name, "status": status, "pages": total, "checkpoint": checkpoint,
            "read_pages": checkpoint, "percent": percent}


def reading_time(name: str = "", add: int = 0) -> dict:
    """Секунды чтения: по одной книге и сумма по всем. add — сколько прибавить."""
    add = max(0, min(300, int(add or 0)))
    with db() as con:
        if name and add:
            con.execute(
                "INSERT INTO book_progress(book, status, page, checkpoint, seconds, updated) "
                "VALUES(?,?,0,0,?,?) "
                "ON CONFLICT(book) DO UPDATE SET "
                "seconds=COALESCE(book_progress.seconds,0)+excluded.seconds, updated=excluded.updated",
                (name, "unread", add, time.time()))
        book_s = 0
        if name:
            row = con.execute(
                "SELECT COALESCE(seconds,0) AS seconds FROM book_progress WHERE book=?",
                (name,)).fetchone()
            book_s = int(row["seconds"]) if row else 0
        total = con.execute("SELECT COALESCE(SUM(seconds),0) AS s FROM book_progress").fetchone()["s"]
    return {"book_name": name, "book": book_s, "total": int(total or 0)}


def confirm_checkpoint(name: str, page: int) -> dict:
    """Страница, до которой человек подтвердил чтение. Процент = эта страница / всего страниц."""
    path = BOOKS_DIR / name
    total = page_count_of(path) if path.is_file() else 0
    page = max(0, min(total, int(page))) if total else 0
    chapters = chapters_of(path) if path.is_file() else []
    done = [ch for ch in chapters if int(ch["end"]) <= page]
    with db() as con:
        if page <= 0:
            status = "unread"
        elif total and page >= total:
            status = "done"
        else:
            status = "reading"
        con.execute(
            "INSERT INTO book_progress(book, status, page, checkpoint, updated) VALUES(?,?,?,?,?) "
            "ON CONFLICT(book) DO UPDATE SET status=excluded.status, checkpoint=excluded.checkpoint, "
            "updated=excluded.updated",
            (name, status, page, page, time.time()))
        con.execute("DELETE FROM read_chapters WHERE book=?", (name,))
        for ch in done:
            con.execute(
                "INSERT INTO read_chapters(book, chapter_index, created, start_page) VALUES(?,?,?,?)",
                (name, int(ch["index"]), time.time(), int(ch["start"])))
    bump_version()
    return progress_of(name)


def _chapter_marked(ch, marks) -> bool:
    """Глава прочитана, если совпала стартовая страница (она не меняется) или старый номер."""
    for m in marks:
        start = m.get("start_page")
        if start:
            if int(start) == int(ch["start"]):
                return True
        elif int(m["chapter_index"]) == int(ch["index"]):
            return True
    return False


def library():
    order = {"reading": 0, "unread": 1, "done": 2}
    rows = [progress_of(name) for name in list_pdf_files()]
    rows.sort(key=lambda r: (order.get(r["status"], 9), r["name"].lower()))
    return rows


def resolve_book(book: str | None) -> Path:
    """Находит файл книги по имени (можно неполному). Пусто = открытая сейчас."""
    if not book:
        book = (get_state("reader") or {}).get("book")
        if not book:
            raise ToolError("Книга не указана и в читалке ничего не открыто.")
    files = list_pdf_files()
    if book in files:
        return BOOKS_DIR / book
    low = book.lower()
    matches = [f for f in files if low in f.lower()]
    if len(matches) == 1:
        return BOOKS_DIR / matches[0]
    if not matches:
        raise ToolError(f"Книга «{book}» не найдена. Есть: {files}")
    raise ToolError(f"Под «{book}» подходит несколько книг: {matches}")


_chapter_cache: dict = {}
_outline_lock = threading.Lock()
_outline_running: set = set()
_claude_lock = threading.Lock()

_NUMBERED = re.compile(r"^\d{1,3}[\.\)]\s+[A-ZА-ЯЁ«\"].{2,70}$")
_NAMED = re.compile(
    r"^(?:Глава|Chapter)\s+\d+\.?\s*.{0,50}$"
    r"|^(?:Книга|Часть|Part)\s+[IVXLC\d]+\.?\s+\S.{0,55}$"
    r"|^(?:Пролог|Эпилог|Введение|Заключение|Предисловие)\.?$"
)


def _title_line(line: str, first: bool) -> bool:
    line = line.strip()
    if not (4 <= len(line) <= 72) or line.endswith((",", ";", ":", ".", "!", "?", "…")):
        return False
    if _NUMBERED.match(line) and len(line.split()) <= 12:
        return True
    if _NAMED.match(line) and len(line.split()) <= 10:
        return True
    if not first:
        return False
    if any(ch in line for ch in ",;:!?—–()"):
        return False
    words = line.split()
    return line[0].isupper() and 1 <= len(words) <= 6


def _pack_chapters(items, page_count: int):
    """items: [{title, start, level}] -> главы с концом и индексом."""
    items = [it for it in items if 1 <= int(it["start"]) <= page_count and str(it.get("title") or "").strip()]
    items.sort(key=lambda it: (int(it["start"]), int(it.get("level") or 1)))
    cleaned = []
    for it in items:
        title = re.sub(r"\s+", " ", str(it["title"])).strip()
        start = int(it["start"])
        level = max(1, min(3, int(it.get("level") or 1)))
        if cleaned and cleaned[-1]["start"] == start and cleaned[-1]["title"].lower() == title.lower():
            continue
        cleaned.append({"title": title[:120], "start": start, "level": level})
    result = []
    for i, it in enumerate(cleaned):
        end = page_count
        if i + 1 < len(cleaned):
            end = max(it["start"], cleaned[i + 1]["start"] - 1)
        result.append({"index": i, "title": it["title"], "level": it["level"],
                       "start": it["start"], "end": end})
    return result


def _page_chunks(page_count: int):
    result = []
    for i, s in enumerate(range(1, page_count + 1, 10)):
        e = min(page_count, s + 9)
        result.append({"index": i, "title": f"Страницы {s}–{e}", "level": 1, "start": s, "end": e})
    return result


def heuristic_chapters(path: Path):
    """Быстрые главы по заголовкам в тексте, пока ИИ не собрал оглавление."""
    doc = pymupdf.open(path)
    n = doc.page_count
    pdf_toc = [t for t in doc.get_toc(simple=True) if t[2] >= 1]
    if pdf_toc:
        doc.close()
        return _pack_chapters(
            [{"title": title, "start": start, "level": level} for level, title, start in pdf_toc], n)
    found = []
    seen = set()
    for i in range(n):
        lines = [ln.strip() for ln in doc[i].get_text("text").splitlines() if ln.strip()]
        heads = [ln for j, ln in enumerate(lines[:8]) if _title_line(ln, j == 0)]
        if len(heads) >= 2:
            continue  # содержание или указатель, не начало главы
        if not heads:
            continue
        title = re.sub(r"\s+", " ", heads[0])
        key = title.lower()
        if key in seen:
            continue
        seen.add(key)
        level = 1 if re.match(r"^(Книга|Часть|Part|Пролог|Эпилог|Введение|Предисловие|Заключение)\b", title) else 2
        found.append({"title": title, "start": i + 1, "level": level})
    doc.close()
    front = [f for f in found if f["start"] <= 16 and re.match(r"^(Книга|Глава|Часть)\b", f["title"])]
    if len(front) >= 3:
        ban = {f["title"].lower() for f in front}
        found = [f for f in found if f["title"].lower() not in ban]
    found = [f for f in found if f["start"] > 2 or re.match(r"^(Предисловие|Введение|Пролог)\b", f["title"])]
    packed = _pack_chapters(found, n)
    return packed if len(packed) >= 4 else _page_chunks(n)


def _outline_key(path: Path) -> str:
    return f"outline:{path.name}:{int(path.stat().st_mtime)}"


def load_outline(path: Path):
    data = get_state(_outline_key(path))
    if isinstance(data, list) and len(data) >= 3:
        return data
    return None


def outline_pending(path: Path) -> bool:
    return path.name in _outline_running


def _book_scan_text(path: Path, limit=70000) -> str:
    """Первые строки страниц — компактная карта книги для ИИ."""
    doc = pymupdf.open(path)
    rows = []
    for i in range(doc.page_count):
        lines = [ln.strip() for ln in doc[i].get_text("text").splitlines() if ln.strip()]
        if not lines:
            continue
        head = re.sub(r"\s+", " ", " / ".join(lines[:2]))[:110]
        rows.append((i + 1, head))
    doc.close()
    counts = {}
    for _, head in rows:
        counts[head] = counts.get(head, 0) + 1
    kept = [f"{p}: {head}" for p, head in rows if counts[head] < 5]
    text = "\n".join(kept)
    return text[:limit]


def _parse_outline_json(raw: str, page_count: int):
    match = re.search(r"\[\s*\{.*\}\s*\]", raw, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, list):
        return None
    packed = _pack_chapters(data, page_count)
    return packed if len(packed) >= 3 else None


def _oauth_clash(text: str) -> bool:
    low = (text or "").lower()
    return "oauth" in low and ("refresh" in low or "another claude" in low)


def run_claude_cli(cmd, **kwargs):
    """Один вызов Claude за раз. Если вход обновляется параллельно — короткая пауза и повтор."""
    last = None
    for attempt in range(3):
        with _claude_lock:
            last = subprocess.run(cmd, **kwargs)
        blob = (last.stdout or "") + "\n" + (last.stderr or "")
        if not _oauth_clash(blob):
            return last
        log("Claude обновляет вход, повтор", attempt + 1)
        time.sleep(3 + attempt * 3)
    return last


def _ask_claude_plain(prompt: str) -> str:
    cli = find_claude_cli()
    if not cli:
        raise RuntimeError("Claude не найден")
    cmd = [cli, "-p", "--output-format", "json"]
    flags = 0x08000000 if os.name == "nt" else 0
    proc = run_claude_cli(cmd, input=prompt, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=240, creationflags=flags)
    raw = (proc.stdout or "").strip()
    if not raw:
        raise RuntimeError((proc.stderr or "пустой ответ Claude")[:300])
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    if data.get("is_error"):
        raise RuntimeError(str(data.get("result") or "ошибка Claude")[:300])
    return str(data.get("result") or "")


def _outline_job(path: Path):
    try:
        doc = pymupdf.open(path)
        pages = doc.page_count
        doc.close()
        scan = _book_scan_text(path)
        prompt = (
            "Составь оглавление книги по карте страниц. "
            f"Книга: «{path.stem}». Всего страниц: {pages}.\n"
            "Каждая строка — «номер страницы: начало текста на ней». "
            "Повторяющиеся колонтитулы уже убраны.\n"
            "Найди реальные части, главы и разделы и страницу, где каждый начинается "
            "(не страницу содержания, а страницу, с которой идёт сам текст раздела).\n"
            "Верни ТОЛЬКО JSON-массив без пояснений: "
            '[{"title":"короткое название как в книге","start":1,"level":1}]. '
            "level 1 — часть или книга, level 2 — глава или раздел. "
            "Если глав много, перечисли их все, вплоть до 120. Не выдумывай то, чего нет в карте.\n\n"
            + scan
        )
        raw = _ask_claude_plain(prompt)
        packed = _parse_outline_json(raw, pages)
        base = heuristic_chapters(path)
        if not packed or (len(base) >= 8 and len(packed) < len(base) * 0.5):
            log("оглавление: ответ ИИ слабее найденных заголовков, оставляю их")
            if len(base) >= 4 and not str(base[0]["title"]).startswith("Страницы "):
                set_state(_outline_key(path), base)
                bump_version()
            return
        set_state(_outline_key(path), packed)
        for key in list(_chapter_cache):
            if key[0] == str(path):
                _chapter_cache.pop(key, None)
        bump_version()
        log(f"оглавление готово: {path.name}, пунктов {len(packed)}")
    except Exception as e:  # noqa
        log("оглавление не собралось:", e)
    finally:
        with _outline_lock:
            _outline_running.discard(path.name)


def schedule_outline(path: Path):
    if load_outline(path):
        return
    with _outline_lock:
        if path.name in _outline_running:
            return
        _outline_running.add(path.name)
    threading.Thread(target=_outline_job, args=(path,), daemon=True).start()


def chapters_of(path: Path):
    """Список глав: [{index, title, level, start, end}] (страницы с 1).
    Сначала оглавление PDF, затем разбор ИИ (он кэшируется), пока его нет — заголовки из текста."""
    saved = load_outline(path)
    if saved:
        return saved
    key = (str(path), path.stat().st_mtime)
    if key not in _chapter_cache:
        _chapter_cache[key] = heuristic_chapters(path)
        schedule_outline(path)
    return _chapter_cache[key]


def pages_text(path: Path, start: int, end: int, limit=160_000):
    doc = pymupdf.open(path)
    start = max(1, start)
    end = min(doc.page_count, end)
    parts, total = [], 0
    for p in range(start, end + 1):
        t = doc[p - 1].get_text("text").strip()
        chunk = f"\n\n[стр. {p}]\n{t}"
        if total + len(chunk) > limit:
            parts.append(f"\n\n[…текст обрезан на стр. {p}; запроси остальное через get_pages({p}, {end})]")
            break
        parts.append(chunk)
        total += len(chunk)
    doc.close()
    text = "".join(parts).strip()
    if not text:
        text = ("(На этих страницах нет текстового слоя — вероятно, это скан. "
                "Нужно сначала распознать PDF через OCR.)")
    return text


def chapter_for_page(path: Path, page: int):
    best = None
    for ch in chapters_of(path):
        if ch["start"] <= page <= ch["end"]:
            if best is None or ch["level"] >= best["level"]:
                best = ch
    return best


# ---------------------------------------------------------------- MCP-инструменты

mcp = FastMCP(
    "book-tutor",
    instructions=(
        "Book Tutor — читалка PDF пользователя в отдельном окне программы на этом компьютере. "
        "Он учится с твоей помощью: выжимки глав, объяснения, тесты. "
        "Начинай с get_reading_context, чтобы узнать, какая книга, страница и "
        "выделение сейчас открыты. Выжимки и объяснения, которые стоит сохранить, "
        "записывай через save_note — так они становятся заметками к книге. "
        "Тесты создавай через create_quiz (пользователь проходит их в читалке), "
        "потом читай ответы через get_quiz_results и проверяй открытые вопросы "
        "через grade_answer. Опирайся только на текст книги и указывай страницы."
    ),
)


@mcp.tool()
def open_reader() -> str:
    """Открывает окно программы Book Tutor на компьютере пользователя."""
    if not open_app_window(PORT):
        return "Не удалось открыть окно Book Tutor: на компьютере нет Microsoft Edge."
    return f"Окно Book Tutor открыто. Книги лежат в папке {BOOKS_DIR}"


@mcp.tool()
def list_books() -> str:
    """Список PDF-книг в папке books."""
    files = list_pdf_files()
    if not files:
        return f"Папка {BOOKS_DIR} пуста. Положи туда PDF."
    return "\n".join(files)


@mcp.tool()
def get_reading_context() -> str:
    """Что пользователь делает прямо сейчас: открытая книга, страница,
    текущая глава и выделенный текст (если есть)."""
    st = get_state("reader") or {}
    if not st.get("book"):
        return f"В читалке ничего не открыто. Книги: {list_pdf_files()}. Попроси открыть окно Book Tutor."
    path = resolve_book(st["book"])
    page = int(st.get("page") or 1)
    ch = chapter_for_page(path, page)
    out = {
        "book": st["book"],
        "page": page,
        "chapter": ch,
        "selection": st.get("selection") or None,
        "selection_page": st.get("selection_page"),
    }
    return json.dumps(out, ensure_ascii=False, indent=1)


@mcp.tool()
def get_toc(book: str = "") -> str:
    """Оглавление книги с номерами глав (index) и диапазонами страниц.
    book — имя файла (можно часть); пусто = книга, открытая в читалке."""
    path = resolve_book(book)
    lines = [f"{path.name}"]
    for ch in chapters_of(path):
        pad = "  " * (ch["level"] - 1)
        lines.append(f"{pad}[{ch['index']}] {ch['title']}  (стр. {ch['start']}–{ch['end']})")
    return "\n".join(lines)


@mcp.tool()
def get_chapter_text(chapter_index: int = -1, book: str = "") -> str:
    """Полный текст главы с пометками [стр. N].
    chapter_index из get_toc; -1 = глава, которую пользователь читает сейчас."""
    path = resolve_book(book)
    chs = chapters_of(path)
    if chapter_index < 0:
        page = int((get_state("reader") or {}).get("page") or 1)
        ch = chapter_for_page(path, page) or chs[0]
    else:
        if chapter_index >= len(chs):
            raise ToolError(f"В книге {len(chs)} глав (0–{len(chs) - 1}).")
        ch = chs[chapter_index]
    head = f"# {ch['title']} (глава {ch['index']}, стр. {ch['start']}–{ch['end']})\n"
    return head + pages_text(path, ch["start"], ch["end"])


@mcp.tool()
def get_pages(start: int, end: int, book: str = "") -> str:
    """Текст страниц с start по end включительно (нумерация с 1)."""
    path = resolve_book(book)
    return pages_text(path, start, end)


@mcp.tool()
def search_book(query: str, book: str = "", max_results: int = 15) -> str:
    """Поиск по всей книге. Возвращает фрагменты с номерами страниц.
    Ищет страницы, где встречается больше всего слов из запроса."""
    path = resolve_book(book)
    words = [w for w in re.findall(r"\w+", query.lower()) if len(w) > 2]
    if not words:
        words = [query.lower()]
    # грубое «стемминг»: обрезаем окончания, чтобы находить разные формы слова
    stems = [w[:max(4, len(w) - 2)] if len(w) > 5 else w for w in words]
    doc = pymupdf.open(path)
    hits = []
    for i in range(doc.page_count):
        text = doc[i].get_text("text")
        low = text.lower()
        score = sum(low.count(s) for s in stems)
        present = sum(1 for s in stems if s in low)
        if present:
            hits.append((present, score, i + 1, text))
    doc.close()
    hits.sort(key=lambda h: (-h[0], -h[1]))
    out = []
    for present, score, page, text in hits[:max_results]:
        low = text.lower()
        pos = min((low.find(s) for s in stems if s in low), default=0)
        snip = text[max(0, pos - 300): pos + 500].replace("\n", " ")
        out.append(f"[стр. {page}] (совпало слов: {present}/{len(stems)})\n…{snip}…")
    return "\n\n".join(out) if out else "Ничего не найдено."


@mcp.tool()
def save_note(title: str, content: str, page: int = 0, chapter: str = "",
              book: str = "", kind: str = "summary") -> str:
    """Сохраняет заметку к книге (Markdown). Появляется во вкладке «Заметки».
    kind: summary (выжимка), explanation (объяснение), definition (определения), note.
    page — страница, к которой привязать заметку (0 = текущая)."""
    path = resolve_book(book)
    if not page:
        page = int((get_state("reader") or {}).get("page") or 1)
    if not chapter:
        ch = chapter_for_page(path, page)
        chapter = ch["title"] if ch else ""
    with db() as con:
        cur = con.execute(
            "INSERT INTO notes(book,chapter,page,title,content,kind,created) VALUES(?,?,?,?,?,?,?)",
            (path.name, chapter, page, title, content, kind, time.time()))
    bump_version()
    return f"Заметка #{cur.lastrowid} сохранена к «{path.name}», стр. {page}."


@mcp.tool()
def list_notes(book: str = "") -> str:
    """Все заметки и выделения пользователя по книге."""
    path = resolve_book(book)
    with db() as con:
        notes = con.execute("SELECT * FROM notes WHERE book=? ORDER BY page, id", (path.name,)).fetchall()
        hls = con.execute("SELECT * FROM highlights WHERE book=? ORDER BY page, id", (path.name,)).fetchall()
    out = ["## Заметки"]
    out += [f"#{n['id']} [стр. {n['page']}] {n['title']} ({n['kind']})\n{n['content']}" for n in notes] or ["нет"]
    out.append("\n## Выделения пользователя")
    out += [f"[стр. {h['page']}] «{h['text']}»" + (f" — {h['comment']}" if h['comment'] else "") for h in hls] or ["нет"]
    return "\n\n".join(out)


@mcp.tool()
def delete_note(note_id: int) -> str:
    """Удаляет заметку по номеру."""
    with db() as con:
        con.execute("DELETE FROM notes WHERE id=?", (note_id,))
    bump_version()
    return f"Заметка #{note_id} удалена."


@mcp.tool()
def create_quiz(title: str, questions: list[dict], chapter: str = "", book: str = "") -> str:
    """Создаёт тест, который пользователь проходит во вкладке «Тесты» читалки.

    questions — список вопросов. Каждый вопрос:
      {"type": "choice", "question": "...", "options": ["A", "B", "C", "D"],
       "answer": 1,               # индекс верного варианта (с 0)
       "explanation": "почему так", "page": 42}
    или
      {"type": "open", "question": "Объясни своими словами ...",
       "answer": "эталонный ответ (пользователь его не видит до проверки)",
       "page": 42}
    Вопросы с выбором проверяются сразу в читалке, открытые — тобой
    через get_quiz_results + grade_answer. Делай вопросы на понимание,
    а не только на память; обязательно указывай page."""
    path = resolve_book(book)
    clean = []
    for i, q in enumerate(questions):
        t = q.get("type", "choice")
        if t not in ("choice", "open"):
            raise ToolError(f"Вопрос {i}: type должен быть choice или open")
        if not q.get("question"):
            raise ToolError(f"Вопрос {i}: нет текста")
        if t == "choice":
            opts = q.get("options") or []
            if len(opts) < 2 or not isinstance(q.get("answer"), int) or not 0 <= q["answer"] < len(opts):
                raise ToolError(f"Вопрос {i}: нужны options и answer — индекс верного варианта")
        clean.append({"type": t, "question": q["question"], "options": q.get("options", []),
                      "answer": q.get("answer"), "explanation": q.get("explanation", ""),
                      "page": q.get("page")})
    if not chapter:
        page = int((get_state("reader") or {}).get("page") or 1)
        ch = chapter_for_page(path, page)
        chapter = ch["title"] if ch else ""
    with db() as con:
        cur = con.execute("INSERT INTO quizzes(book,chapter,title,questions,created) VALUES(?,?,?,?,?)",
                          (path.name, chapter, title, json.dumps(clean, ensure_ascii=False), time.time()))
    bump_version()
    return (f"Тест #{cur.lastrowid} «{title}» из {len(clean)} вопросов создан и уже виден "
            f"во вкладке «Тесты» окна Book Tutor. Скажи пользователю пройти его "
            f"и написать тебе, когда закончит.")


@mcp.tool()
def get_quiz_results(quiz_id: int = 0) -> str:
    """Ответы пользователя на тест. quiz_id=0 — последний тест.
    Для открытых вопросов показан эталон, чтобы ты мог проверить ответ."""
    with db() as con:
        if quiz_id:
            qz = con.execute("SELECT * FROM quizzes WHERE id=?", (quiz_id,)).fetchone()
        else:
            qz = con.execute("SELECT * FROM quizzes ORDER BY id DESC LIMIT 1").fetchone()
        if not qz:
            return "Тестов пока нет."
        ans = {a["q_index"]: a for a in con.execute("SELECT * FROM answers WHERE quiz_id=?", (qz["id"],))}
    qs = json.loads(qz["questions"])
    out = [f"Тест #{qz['id']} «{qz['title']}» ({qz['book']}, {qz['chapter']})"]
    right = 0
    for i, q in enumerate(qs):
        a = ans.get(i)
        line = f"\n{i}. [{q['type']}] {q['question']}"
        if not a:
            line += "\n   — пока без ответа"
        elif q["type"] == "choice":
            chosen = q["options"][int(a["answer"])] if a["answer"] not in (None, "") else "?"
            ok = bool(a["correct"])
            right += ok
            line += f"\n   Ответ: {chosen} → {'ВЕРНО' if ok else 'НЕВЕРНО'} (правильно: {q['options'][q['answer']]})"
        else:
            status = {None: "ЖДЁТ ТВОЕЙ ПРОВЕРКИ", 1: "засчитано", 0: "не засчитано"}[a["correct"]]
            if a["correct"] == 1:
                right += 1
            line += f"\n   Ответ пользователя: {a['answer']}\n   Эталон: {q.get('answer')}\n   Статус: {status}"
            if a["feedback"]:
                line += f"\n   Твой отзыв: {a['feedback']}"
        out.append(line)
    out.append(f"\nИтого верно: {right} из {len(qs)}")
    return "\n".join(out)


@mcp.tool()
def grade_answer(quiz_id: int, question_index: int, correct: bool, feedback: str) -> str:
    """Выставляет оценку открытому вопросу. feedback — что верно, чего не хватает,
    со ссылкой на страницу. Отзыв сразу появится в читалке."""
    with db() as con:
        cur = con.execute("UPDATE answers SET correct=?, feedback=? WHERE quiz_id=? AND q_index=?",
                          (1 if correct else 0, feedback, quiz_id, question_index))
        if cur.rowcount == 0:
            return "Пользователь ещё не ответил на этот вопрос."
    bump_version()
    return "Оценка сохранена и видна в читалке."


@mcp.tool()
def get_mistakes(book: str = "") -> str:
    """Все вопросы, на которые пользователь ответил неверно (для повторения).
    Удобно, чтобы сделать тест «работа над ошибками»."""
    path = resolve_book(book)
    with db() as con:
        rows = con.execute(
            "SELECT q.id, q.chapter, q.questions, a.q_index, a.answer FROM answers a "
            "JOIN quizzes q ON q.id=a.quiz_id WHERE q.book=? AND a.correct=0", (path.name,)).fetchall()
    if not rows:
        return "Ошибок нет."
    out = []
    for r in rows:
        q = json.loads(r["questions"])[r["q_index"]]
        out.append(f"[тест {r['id']}, {r['chapter']}, стр. {q.get('page')}] {q['question']}")
    return "\n".join(out)


# ---------------------------------------------------------------- чат внутри читалки
# Сообщения из вкладки «Чат» уходят в Claude Code (программа claude) по твоей
# подписке. Ему подключается этот же server.py как MCP, поэтому в чате Claude
# умеет всё то же самое: читать главы, искать, сохранять заметки, делать тесты.

CHAT_SYSTEM = (
    "Ты — Claude внутри программы Book Tutor: встроенный чат читалки PDF. "
    "Пользователь читает книгу в окне рядом и учится с твоей помощью. "
    "Для работы с книгой используй инструменты book-tutor: get_reading_context, "
    "get_chapter_text, get_pages, search_book, get_toc, save_note, create_quiz, "
    "get_quiz_results, grade_answer, get_mistakes, list_notes. "
    "Опирайся на текст книги и указывай страницы. Выжимки и определения сохраняй "
    "через save_note, если пользователь просит сохранить. Тесты создавай через create_quiz: "
    "они появляются во вкладке «Тесты». Отвечай по-русски, понятно и по делу, "
    "в Markdown. Не трогай файлы на компьютере и не запускай команды."
)

_chat_busy = threading.Lock()


def find_claude_cli() -> str | None:
    """Ищет Claude Code — программу, через которую чат читалки говорит с Claude."""
    for name in ("claude", "claude.exe", "claude.cmd"):
        p = shutil.which(name)
        if p and "WindowsApps" not in p.replace("/", "\\"):
            return p
    home = Path.home()
    local = Path(os.environ.get("LOCALAPPDATA", ""))
    appdata = Path(os.environ.get("APPDATA", ""))
    candidates = [
        home / ".local" / "bin" / "claude.exe",
        home / ".local" / "bin" / "claude",
        home / ".claude" / "local" / "claude.exe",
        home / ".claude" / "bin" / "claude.exe",
        local / "Microsoft" / "WinGet" / "Links" / "claude.exe",
        local / "claude" / "claude.exe",
        local / "Programs" / "claude" / "claude.exe",
        appdata / "npm" / "claude.cmd",
        appdata / "npm" / "claude.exe",
    ]
    for c in candidates:
        if c.is_file():
            return str(c)
    for folder in (local / "Programs", home / ".local" / "bin", home / ".claude"):
        if not folder.is_dir():
            continue
        for c in folder.rglob("claude.exe"):
            if "WindowsApps" in str(c):
                continue
            return str(c)
    return None


def _chat_finish(msg_id: int, status: str, text: str):
    with db() as con:
        con.execute("UPDATE chat SET status=?, text=? WHERE id=?", (status, text, msg_id))
    bump_version()


def _run_claude(cli: str, prompt: str, session: str | None):
    cfg = BASE / "chat_mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {"book-tutor": {
        "command": sys.executable, "args": [str(BASE / "server.py")]}}}), encoding="utf-8")
    sysfile = BASE / "chat_system.txt"
    sysfile.write_text(CHAT_SYSTEM, encoding="utf-8")
    cmd = [cli, "-p", "--output-format", "json",
           "--mcp-config", str(cfg), "--strict-mcp-config",
           "--allowedTools", "mcp__book-tutor",
           "--append-system-prompt-file", str(sysfile)]
    if session:
        cmd += ["--resume", session]
    flags = 0x08000000 if os.name == "nt" else 0  # без чёрного окна
    return run_claude_cli(cmd, input=prompt, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", cwd=BASE,
                          timeout=600, creationflags=flags)


_auth_cache = {"cli": None, "t": 0.0, "ok": False}


def claude_logged_in(cli: str, fresh: bool = False) -> bool:
    now = time.time()
    if not fresh and _auth_cache["cli"] == cli and now - _auth_cache["t"] < 20:
        return _auth_cache["ok"]
    ok = False
    try:
        r = run_claude_cli(
            [cli, "auth", "status"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=30,
            creationflags=0x08000000 if os.name == "nt" else 0,
        )
        ok = bool(json.loads(r.stdout or "{}").get("loggedIn"))
    except (OSError, subprocess.TimeoutExpired, ValueError):
        ok = False
    _auth_cache.update(cli=cli, t=now, ok=ok)
    return ok


def chat_worker(msg_id: int, book: str, text: str):
    with _chat_busy:  # по одному запросу за раз
        cli = find_claude_cli()
        if not cli:
            return _chat_finish(msg_id, "error",
                "Не найдена программа Claude. Перезапусти Book Tutor с рабочего стола.")
        st = get_state("reader") or {}
        ctx = f"[Сейчас в читалке: книга «{book}», стр. {st.get('page', 1)}"
        sel = (st.get("selection") or "").strip()
        if sel:
            ctx += f"; выделено на стр. {st.get('selection_page')}: «{sel[:2000]}»"
        prompt = ctx + "]\n\n" + text

        sessions = get_state("chat_sessions") or {}
        try:
            r = _run_claude(cli, prompt, sessions.get(book))
            if r.returncode != 0 and sessions.get(book) and "session" in (r.stderr + r.stdout).lower():
                r = _run_claude(cli, prompt, None)  # старый разговор не найден — начинаем новый
        except subprocess.TimeoutExpired:
            return _chat_finish(msg_id, "error", "Claude слишком долго отвечал (больше 10 минут).")
        except OSError as e:
            return _chat_finish(msg_id, "error", f"Не удалось запустить Claude Code: {e}")

        try:
            data = json.loads(r.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            tail = (r.stderr or r.stdout or "").strip()[-1500:]
            return _chat_finish(msg_id, "error", "Claude Code вернул ошибку:\n\n```\n" + tail + "\n```")

        if data.get("session_id"):
            sessions[book] = data["session_id"]
            set_state("chat_sessions", sessions)
        answer = data.get("result") or "(пустой ответ)"
        failed = bool(data.get("is_error"))
        low = answer.lower()
        if _oauth_clash(answer):
            failed = True
            answer = ("Claude обновлял вход и не успел ответить. Подожди минуту и отправь сообщение ещё раз.")
        elif failed and ("not logged" in low or "please run /login" in low or "auth login" in low):
            try:
                subprocess.Popen([cli, "auth", "login"], cwd=BASE)
            except OSError:
                pass
            answer = ("Чат не привязан к аккаунту Claude. В браузере откроется вход — "
                      "войди и отправь сообщение ещё раз.")
        _chat_finish(msg_id, "error" if failed else "done", answer)


def chat_send(book: str, text: str):
    now = time.time()
    with db() as con:
        con.execute("INSERT INTO chat(book,role,text,status,created) VALUES(?,?,?,?,?)",
                    (book, "user", text, "done", now))
        cur = con.execute("INSERT INTO chat(book,role,text,status,created) VALUES(?,?,?,?,?)",
                          (book, "assistant", "", "thinking", now + 0.001))
    bump_version()
    threading.Thread(target=chat_worker, args=(cur.lastrowid, book, text), daemon=True).start()


def chat_reset(book: str):
    with db() as con:
        con.execute("DELETE FROM chat WHERE book=?", (book,))
    sessions = get_state("chat_sessions") or {}
    sessions.pop(book, None)
    set_state("chat_sessions", sessions)
    bump_version()


# ---------------------------------------------------------------- веб-сервер для читалки

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # не засорять вывод

    def _send(self, code, body: bytes, ctype="application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode())

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(url.path)
        qs = urllib.parse.parse_qs(url.query)
        try:
            if path in ("/", "/index.html"):
                return self._send(200, (WEB_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
            if path.startswith("/vendor/"):
                f = (WEB_DIR / path.lstrip("/")).resolve()
                if WEB_DIR.resolve() not in f.parents or not f.is_file():
                    return self._send(404, b"not found", "text/plain")
                types = {".js": "text/javascript", ".bcmap": "application/octet-stream",
                         ".pfb": "application/octet-stream", ".ttf": "font/ttf"}
                return self._send(200, f.read_bytes(), types.get(f.suffix, "application/octet-stream"))
            if path == "/api/books":
                return self._json(list_pdf_files())
            if path == "/api/library":
                return self._json(library())
            if path.startswith("/book/"):
                f = resolve_book(path[len("/book/"):])
                return self._send(200, f.read_bytes(), "application/pdf")
            if path == "/api/toc":
                book_path = resolve_book(qs["book"][0])
                return self._json({"chapters": chapters_of(book_path),
                                   "pending": outline_pending(book_path)})
            if path == "/api/version":
                return self._json({"version": get_state("version", 0)})
            if path == "/api/reading-time":
                book = ""
                if qs.get("book") and qs["book"][0]:
                    book = resolve_book(qs["book"][0]).name
                return self._json(reading_time(book))
            if path == "/api/resume":
                st = get_state("reader") or {}
                places = get_state("places") or {}
                book = st.get("book") or ""
                page = 1
                if book:
                    page = int(places.get(book) or st.get("page") or 1)
                return self._json({"book": book, "page": page, "places": places})
            if path == "/api/data":
                book = resolve_book(qs["book"][0]).name
                with db() as con:
                    notes = [dict(r) for r in con.execute("SELECT * FROM notes WHERE book=? ORDER BY page,id", (book,))]
                    hls = [dict(r) for r in con.execute("SELECT * FROM highlights WHERE book=? ORDER BY page,id", (book,))]
                    read_rows = [dict(r) for r in con.execute(
                        "SELECT chapter_index, start_page FROM read_chapters WHERE book=? ORDER BY chapter_index",
                        (book,))]
                    read = [r["chapter_index"] for r in read_rows]
                    quizzes = []
                    for q in con.execute("SELECT * FROM quizzes WHERE book=? ORDER BY id DESC", (book,)):
                        q = dict(q)
                        q["questions"] = json.loads(q["questions"])
                        q["answers"] = {a["q_index"]: dict(a) for a in con.execute(
                            "SELECT * FROM answers WHERE quiz_id=?", (q["id"],))}
                        quizzes.append(q)
                    chat = [dict(r) for r in con.execute("SELECT * FROM chat WHERE book=? ORDER BY id", (book,))]
                cli = find_claude_cli()
                return self._json({"notes": notes, "highlights": hls, "quizzes": quizzes, "chat": chat,
                                   "read": read, "read_marks": read_rows, "progress": progress_of(book),
                                   "claude_found": cli is not None,
                                   "claude_logged_in": bool(cli and claude_logged_in(cli))})
            self._send(404, b"not found", "text/plain")
        except Exception as e:  # noqa
            self._json({"error": str(e)}, 400)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            data = self._body()
            if path == "/api/reading-time":
                book = resolve_book(data.get("book") or "").name
                return self._json(reading_time(book, int(data.get("seconds") or 0)))
            if path == "/api/state":
                st = get_state("reader") or {}
                for key in ("book", "page", "selection"):
                    if key in data:
                        st[key] = data[key]
                book_name = data.get("book") or st.get("book")
                if book_name and data.get("page"):
                    try:
                        page = int(data["page"])
                    except (TypeError, ValueError):
                        page = 0
                    if page >= 1:
                        resolved = resolve_book(book_name).name
                        places = get_state("places") or {}
                        places[resolved] = page
                        set_state("places", places)
                        st["book"] = resolved
                        st["page"] = page
                set_state("reader", st)
                if book_name and data.get("promote"):
                    save_progress(resolve_book(book_name).name, promote=True)
                return self._json({"ok": True})
            if path == "/api/checkpoint":
                book = resolve_book(data["book"]).name
                return self._json(confirm_checkpoint(book, int(data.get("page") or 0)))
            if path == "/api/progress":
                book = resolve_book(data["book"]).name
                save_progress(book, status=data.get("status"))
                bump_version()
                return self._json(progress_of(book))
            if path == "/api/chat":
                text = (data.get("text") or "").strip()
                if not text:
                    return self._json({"error": "пустое сообщение"}, 400)
                chat_send(resolve_book(data["book"]).name, text)
                return self._json({"ok": True})
            if path == "/api/chat/reset":
                chat_reset(resolve_book(data["book"]).name)
                return self._json({"ok": True})
            if path == "/api/note":
                path_book = resolve_book(data["book"])
                title = (data.get("title") or "").strip()
                content = (data.get("content") or "").strip()
                if not title and not content:
                    return self._json({"error": "пусто"}, 400)
                page = int(data.get("page") or (get_state("reader") or {}).get("page") or 1)
                chapter = (data.get("chapter") or "").strip()
                if not chapter:
                    ch = chapter_for_page(path_book, page)
                    chapter = ch["title"] if ch else ""
                kind = data.get("kind") or "note"
                with db() as con:
                    con.execute(
                        "INSERT INTO notes(book,chapter,page,title,content,kind,created) VALUES(?,?,?,?,?,?,?)",
                        (path_book.name, chapter, page, title or content[:80], content, kind, time.time()))
                bump_version()
                return self._json({"ok": True})
            if path == "/api/highlight":
                with db() as con:
                    con.execute("INSERT INTO highlights(book,page,text,comment,created) VALUES(?,?,?,?,?)",
                                (data["book"], data["page"], data["text"], data.get("comment", ""), time.time()))
                bump_version()
                return self._json({"ok": True})
            if path == "/api/read":
                book = resolve_book(data["book"]).name
                idx = int(data["index"])
                start = data.get("start")
                with db() as con:
                    if data.get("done", True):
                        con.execute(
                            "INSERT INTO read_chapters(book,chapter_index,created,start_page) VALUES(?,?,?,?) "
                            "ON CONFLICT(book, chapter_index) DO UPDATE SET "
                            "start_page=COALESCE(excluded.start_page, read_chapters.start_page)",
                            (book, idx, time.time(), int(start) if start else None))
                    elif start:
                        con.execute(
                            "DELETE FROM read_chapters WHERE book=? AND (chapter_index=? OR start_page=?)",
                            (book, idx, int(start)))
                    else:
                        con.execute("DELETE FROM read_chapters WHERE book=? AND chapter_index=?", (book, idx))
                    row = con.execute("SELECT status FROM book_progress WHERE book=?", (book,)).fetchone()
                if data.get("done", True) and (not row or row["status"] == "unread"):
                    save_progress(book, status="reading")
                bump_version()
                return self._json({"ok": True})
            if path == "/api/delete":
                table = {"note": "notes", "highlight": "highlights", "quiz": "quizzes"}[data["kind"]]
                with db() as con:
                    con.execute(f"DELETE FROM {table} WHERE id=?", (data["id"],))
                    if table == "quizzes":
                        con.execute("DELETE FROM answers WHERE quiz_id=?", (data["id"],))
                bump_version()
                return self._json({"ok": True})
            if path == "/api/answer":
                with db() as con:
                    qz = con.execute("SELECT questions FROM quizzes WHERE id=?", (data["quiz_id"],)).fetchone()
                    q = json.loads(qz["questions"])[data["q_index"]]
                    if q["type"] == "choice":
                        correct = 1 if int(data["answer"]) == q["answer"] else 0
                    else:
                        correct = None  # проверит Claude
                    con.execute("INSERT OR REPLACE INTO answers(quiz_id,q_index,answer,correct,feedback,created) "
                                "VALUES(?,?,?,?,?,?)",
                                (data["quiz_id"], data["q_index"], str(data["answer"]), correct, None, time.time()))
                bump_version()
                return self._json({"ok": True, "correct": correct})
            self._send(404, b"not found", "text/plain")
        except Exception as e:  # noqa
            self._json({"error": str(e)}, 400)


def start_web() -> bool:
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        log(f"порт {PORT} занят — вероятно, читалка уже запущена другим экземпляром")
        return False
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log(f"окно Book Tutor, книги: {BOOKS_DIR}")
    return True


init_db()

if __name__ == "__main__":
    started = start_web()
    if "--web-only" in sys.argv:
        # режим окна без Claude (так читалку запускает BookTutor.exe)
        if not started:
            if "--no-window" not in sys.argv:
                open_app_window(PORT)
            sys.exit(0)
        with db() as _con:  # ответы, которые «думали», когда программа закрылась
            _con.execute("UPDATE chat SET status='error', text='Ответ прерван: программа была закрыта.' "
                         "WHERE status='thinking'")
        print(f"Book Tutor работает. Книги: {BOOKS_DIR}")
        print("Закрой окно Book Tutor и этот процесс, чтобы выключить читалку.")
        if "--no-window" not in sys.argv:
            open_app_window(PORT)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
    else:
        mcp.run()