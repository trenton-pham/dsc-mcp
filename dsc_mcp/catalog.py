from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import fcntl
import os
import re
import shutil
import sqlite3
import tempfile
from urllib.parse import quote
import httpx

from .sources import LIBRARIES, Section, canonical_library, download, extract

SCHEMA_VERSION = 1
DEFAULT_DB = Path("data/docs.sqlite3")
SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sections (
  doc_id TEXT PRIMARY KEY, library TEXT NOT NULL, title TEXT NOT NULL,
  heading TEXT NOT NULL, text TEXT NOT NULL, url TEXT NOT NULL,
  version TEXT NOT NULL, downloaded_at TEXT NOT NULL, content_hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS sections_library ON sections(library);
CREATE VIRTUAL TABLE IF NOT EXISTS sections_fts USING fts5(
  library, title, heading, text, doc_id UNINDEXED, tokenize='unicode61'
);
"""
STOPWORDS = {"a", "an", "and", "are", "can", "do", "does", "for", "from", "how", "i", "in", "is", "it", "of", "on", "or", "the", "to", "using", "with"}


class CatalogError(ValueError):
    pass


def _connect(db: Path) -> sqlite3.Connection:
    if not db.is_file():
        raise CatalogError(f"Documentation index is missing at {db}. Run `python -m dsc_mcp sync --db {db}` first.")
    uri = f"file:{quote(str(db.resolve()))}?mode=ro"
    conn = None
    try:
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        version = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if version is None or version[0] != str(SCHEMA_VERSION):
            raise CatalogError(f"Unsupported index schema at {db}. Run sync with this version of dsc-mcp.")
        return conn
    except (sqlite3.DatabaseError, CatalogError) as exc:
        if conn is not None:
            conn.close()
        if isinstance(exc, CatalogError):
            raise
        raise CatalogError(f"Invalid documentation index at {db}. Run sync to rebuild it: {exc}") from exc


@contextmanager
def _reader(db: Path):
    conn = _connect(db)
    try:
        yield conn
    except sqlite3.DatabaseError as exc:
        raise CatalogError(f"Invalid documentation index at {db}. Run a full sync to rebuild it: `python -m dsc_mcp sync --db {db}`. {exc}") from exc
    finally:
        conn.close()


def sync(db: Path, names: list[str] | None = None) -> dict[str, int]:
    """Publish one complete snapshot after every selected source succeeds."""
    db = Path(db)
    selected = tuple(dict.fromkeys(canonical_library(name) for name in names)) if names else tuple(LIBRARIES)
    db.parent.mkdir(parents=True, exist_ok=True)
    lock = db.with_name(db.name + ".lock")
    lock_file = lock.open("a")
    temp: Path | None = None
    try:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise CatalogError(f"Another sync is running for {db}.") from exc
        fd, path = tempfile.mkstemp(prefix=db.name + ".", suffix=".tmp", dir=db.parent)
        os.close(fd)
        temp = Path(path)
        if db.exists() and set(selected) != set(LIBRARIES):
            with _reader(db):
                pass
            shutil.copyfile(db, temp)
        conn = sqlite3.connect(temp)
        try:
            conn.executescript(SCHEMA)
            conn.execute("INSERT OR REPLACE INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
            counts: dict[str, int] = {}
            with conn:
                for name in selected:
                    sections: list[Section] = []
                    for source in LIBRARIES[name].sources:
                        try:
                            url, html = download(source)
                            sections.extend(extract(source, url, html))
                        except (httpx.HTTPError, ValueError) as exc:
                            raise CatalogError(f"Failed to refresh {name} from {source.url}: {exc}. Existing index retained.") from exc
                    if not sections:
                        raise CatalogError(f"No sections extracted for {name}; existing index retained.")
                    conn.execute("DELETE FROM sections_fts WHERE library=?", (name,))
                    conn.execute("DELETE FROM sections WHERE library=?", (name,))
                    conn.executemany("INSERT INTO sections VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                     [(s.doc_id, s.library, s.title, s.heading, s.text, s.url,
                                       s.version, s.downloaded_at, s.content_hash) for s in sections])
                    conn.executemany("INSERT INTO sections_fts VALUES (?, ?, ?, ?, ?)",
                                     [(s.library, s.title, s.heading, s.text, s.doc_id) for s in sections])
                    counts[name] = len(sections)
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise CatalogError("New index failed SQLite integrity check; existing index retained.")
        finally:
            conn.close()
        os.replace(temp, db)
        temp = None
        return counts
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)
        lock_file.close()


def list_libraries(db: Path = DEFAULT_DB) -> list[dict]:
    counts: dict[str, tuple[int, int, str, str]] = {}
    if Path(db).is_file():
        with _reader(Path(db)) as conn:
            for row in conn.execute("""SELECT library, COUNT(*) AS n,
                COUNT(DISTINCT CASE WHEN instr(url, '#') > 0 THEN substr(url, 1, instr(url, '#') - 1) ELSE url END) AS pages,
                MAX(downloaded_at) AS date, GROUP_CONCAT(DISTINCT version) AS versions
                FROM sections GROUP BY library"""):
                counts[row["library"]] = (row["n"], row["pages"], row["date"], row["versions"])
    return [{"library": name, "coverage": spec.coverage, "status": "curated" if name in counts else "missing",
             "sections": counts[name][0] if name in counts else 0,
             "pages": counts[name][1] if name in counts else 0,
             "downloaded_at": counts[name][2] if name in counts else None,
             "documentation_versions": counts[name][3].split(",") if name in counts else []}
            for name, spec in LIBRARIES.items()]


def search_docs(query: str, library: str | None = None, limit: int = 5, db: Path = DEFAULT_DB) -> list[dict]:
    if not isinstance(query, str) or not query.strip() or len(query) > 300:
        raise CatalogError("Query must contain 1 to 300 characters.")
    if type(limit) is not int or not 1 <= limit <= 10:
        raise CatalogError("Limit must be an integer from 1 to 10.")
    name = canonical_library(library) if library is not None else None
    tokens = re.findall(r"\w+", query.lower())
    tokens = [token for token in tokens if token not in STOPWORDS] or tokens
    if not tokens:
        raise CatalogError("Query must contain searchable words.")
    match = " OR ".join('"' + token.replace('"', '""') + '"' for token in dict.fromkeys(tokens))
    symbol = query.strip().lower() if re.fullmatch(r"\w+(?:\.\w+)+", query.strip()) else None
    with _reader(Path(db)) as conn:
        rows = conn.execute("""
            SELECT s.doc_id, s.library, s.title, s.heading, s.url, s.version,
                   s.downloaded_at, substr(s.text, 1, 900) AS snippet,
                   bm25(sections_fts, 0, 5, 8, 1) AS rank
            FROM sections_fts JOIN sections s ON s.doc_id=sections_fts.doc_id
            WHERE sections_fts MATCH ? AND (? IS NULL OR s.library=?)
            ORDER BY CASE WHEN lower(substr(s.url, instr(s.url, '#') + 1))=? THEN 0 ELSE 1 END,
                     CASE WHEN lower(substr(s.url, instr(s.url, '#') + 1))=? THEN s.rowid ELSE 0 END,
                     rank, s.title, s.heading LIMIT ?
        """, (match, name, name, symbol, symbol, limit)).fetchall()
        return [dict(row) for row in rows]


def read_doc(doc_id: str, db: Path = DEFAULT_DB) -> dict:
    if not isinstance(doc_id, str) or not re.fullmatch(r"[0-9a-f]{24}", doc_id):
        raise CatalogError("Invalid document ID. Use an ID returned by search_docs.")
    with _reader(Path(db)) as conn:
        row = conn.execute("SELECT * FROM sections WHERE doc_id=?", (doc_id,)).fetchone()
    if row is None:
        raise CatalogError("Document ID is absent from this snapshot. Search again; the index may have been refreshed.")
    return dict(row)
