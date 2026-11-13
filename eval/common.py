"""Shared helpers for the evaluations: neutral page text, OpenRouter calls, and loading a server version."""

import importlib.util
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pymupdf

ROOT = Path(__file__).resolve().parent
RESULTS = ROOT / "results"


def book_pdf():
    path = os.environ.get("BOOK_PDF")
    if not path:
        sys.exit("set BOOK_PDF to the path of the book's PDF")
    return path


def pages():
    """Page text from Chapter 1 to the endnotes, extracted independently of any server version."""
    d = pymupdf.open(book_pdf())
    toc = d.get_toc()
    first = next(p for _, t, p in toc if t.startswith("CHAPTER 1"))
    notes = next(p for _, t, p in toc if t == "NOTES")
    return {n: d[n - 1].get_text() for n in range(first, notes)}


def openrouter(path, payload):
    """POST to OpenRouter with retries on rate limits and server errors."""
    req = urllib.request.Request(
        f"https://openrouter.ai/api/v1/{path}", json.dumps(payload).encode(),
        {"Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}", "Content-Type": "application/json"})
    for attempt in range(8):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code != 429 and e.code < 500 or attempt == 7:
                raise
            time.sleep(float(e.headers.get("Retry-After") or 2 ** attempt))


def chat(model, prompt, max_tokens=1500):
    for attempt in range(5):
        reply = openrouter("chat/completions", {"model": model, "max_tokens": max_tokens, "temperature": 0,
                                                "messages": [{"role": "user", "content": prompt}]})
        content = reply["choices"][0]["message"].get("content")
        if content:
            return content.strip()
        time.sleep(2 ** attempt)
    raise RuntimeError(f"{model} returned no content")


def load_server(server_dir, data_dir):
    """Import mcp/server.py from server_dir (any version of it) with its caches in data_dir."""
    os.environ["DATA_DIR"] = str(data_dir)
    os.environ.setdefault("BOOK_PDF", book_pdf())
    Path(data_dir).mkdir(parents=True, exist_ok=True)
    spec = importlib.util.spec_from_file_location("server", Path(server_dir) / "server.py")
    server = importlib.util.module_from_spec(spec)
    sys.modules["server"] = server
    spec.loader.exec_module(server)
    server._index()
    return server
