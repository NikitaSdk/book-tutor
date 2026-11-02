# BookTutor

**A PDF reader connected to Claude through MCP.** You read a book in the browser; Claude sees the chapter you opened and the text you selected, writes summaries, explains passages, builds quizzes and grades your answers. Everything is saved as notes to the book.

![Python](https://img.shields.io/badge/Python-3.10+-3776AB?logo=python&logoColor=white)
![MCP](https://img.shields.io/badge/MCP-server-000000)
![SQLite](https://img.shields.io/badge/SQLite-003B57?logo=sqlite&logoColor=white)
![PDF.js](https://img.shields.io/badge/PDF.js-reader-E44D26)

## Features

- **MCP server with 14 tools:** reading context, table of contents, full-text search, notes, quizzes, answer grading, quizzes built from past mistakes.
- **Chapter detection:** from the PDF outline, a text heuristic, or an LLM pass when the book has no outline.
- **Local web reader** on PDF.js (works offline), reading progress and time tracking.
- **One-click install:** `install.py` installs dependencies and registers the server in Claude Desktop; also packaged as a Windows `.exe`.

## Example prompts

- "Summarise the chapter I'm reading and save it"
- "Explain the selected passage"
- "Make an 8-question quiz on this chapter, half of them open questions"
- "Make a quiz from my mistakes"

## Structure

```
server.py        MCP server + reader web server + SQLite library
web/index.html   reader UI (PDF.js in web/vendor)
install.py       installs packages and connects the server to Claude Desktop
launcher.py      entry point for the packaged app
```

## Run

```bash
python install.py           # once; then restart Claude Desktop
python server.py --web-only # reader only, without Claude
```

Put PDF files into a `books/` folder next to `server.py`. Detailed guide in Russian: [README.ru.md](README.ru.md).
