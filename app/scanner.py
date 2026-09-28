from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from datetime import datetime, timezone

from .config import CALIBRE_LIBRARY, DATABASE_PATH

import sys

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS books (
    id INTEGER PRIMARY KEY,
    calibre_id INTEGER UNIQUE NOT NULL,
    title TEXT,
    authors TEXT,
    publisher TEXT,
    pubdate TEXT,
    series TEXT,
    series_index REAL,
    languages TEXT,
    tags TEXT,
    identifiers TEXT,
    comments TEXT,
    calibre_path TEXT,
    formats TEXT,
    indexed_at TEXT
);

CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY,
    book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    calibre_format TEXT,
    file_name TEXT,
    file_path TEXT,
    file_size INTEGER,
    content_hash TEXT,
    exists_on_disk INTEGER NOT NULL,
    scanned_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_files_hash ON files(content_hash);
CREATE INDEX IF NOT EXISTS idx_files_book ON files(book_id);
CREATE INDEX IF NOT EXISTS idx_books_calibre_id ON books(calibre_id);

CREATE TABLE IF NOT EXISTS scan_runs (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    books_total INTEGER,
    files_total INTEGER,
    files_missing INTEGER,
    duplicate_files INTEGER,
    errors INTEGER
);
"""

def sha256(path: Path, block_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            block = f.read(block_size)
            if not block:
                break
            h.update(block)
    return h.hexdigest()

def calibre_conn():
    # SQLite read-only URI. The Calibre DB is never modified.
    uri = CALIBRE_LIBRARY.joinpath("metadata.db").as_uri() + "?mode=ro"
    return sqlite3.connect(uri, uri=True)

def q1(conn, sql, params=()):
    return conn.execute(sql, params).fetchone()

def qall(conn, sql, params=()):
    return conn.execute(sql, params).fetchall()

def names(conn, sql, params=()):
    return [r[0] for r in qall(conn, sql, params)]

def scan():
    calibre_db = CALIBRE_LIBRARY / "metadata.db"
    if not calibre_db.exists():
        raise FileNotFoundError(f"Calibre DB non trovata: {calibre_db}")

    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    out = sqlite3.connect(DATABASE_PATH)
    out.executescript(SCHEMA)
    out.execute("PRAGMA foreign_keys=ON")

    started = datetime.now(timezone.utc).isoformat()
    run_id = out.execute(
        "INSERT INTO scan_runs(started_at) VALUES (?)", (started,)
    ).lastrowid

    src = calibre_conn()
    errors = 0
    files_total = files_missing = 0

    try:
        books = qall(src, """
            SELECT id, title, pubdate, series_index, path
            FROM books
            ORDER BY id
        """)

        for calibre_id, title, pubdate, series_index, calibre_path in books:
            try:
                authors = names(src, """
                    SELECT a.name
                    FROM authors a
                    JOIN books_authors_link l ON l.author=a.id
                    WHERE l.book=?
                    ORDER BY l.id
                """, (calibre_id,))

                publishers = names(src, """
                    SELECT p.name
                    FROM publishers p
                    JOIN books_publishers_link l ON l.publisher=p.id
                    WHERE l.book=?
                """, (calibre_id,))

                series = names(src, """
                    SELECT s.name
                    FROM series s
                    JOIN books_series_link l ON l.series=s.id
                    WHERE l.book=?
                """, (calibre_id,))

                languages = names(src, """
                    SELECT l.lang_code
                    FROM languages l
                    JOIN books_languages_link bl ON bl.lang_code=l.lang_code
                    WHERE bl.book=?
                    ORDER BY bl.item_order
                """, (calibre_id,))

                tags = names(src, """
                    SELECT t.name
                    FROM tags t
                    JOIN books_tags_link l ON l.tag=t.id
                    WHERE l.book=?
                    ORDER BY l.id
                """, (calibre_id,))

                identifiers = {
                    typ: val for typ, val in qall(
                        src, "SELECT type,val FROM identifiers WHERE book=?",
                        (calibre_id,)
                    )
                }

                comment_row = q1(
                    src, "SELECT text FROM comments WHERE book=? ORDER BY id LIMIT 1",
                    (calibre_id,)
                )
                comments = comment_row[0] if comment_row else None

                publisher = publishers[0] if publishers else None
                series_name = series[0] if series else None

                out.execute("DELETE FROM files WHERE book_id IN "
                            "(SELECT id FROM books WHERE calibre_id=?)",
                            (calibre_id,))
                out.execute("DELETE FROM books WHERE calibre_id=?", (calibre_id,))

                cur = out.execute("""
                    INSERT INTO books(
                        calibre_id,title,authors,publisher,pubdate,series,
                        series_index,languages,tags,identifiers,comments,
                        calibre_path,formats,indexed_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    calibre_id, title, "; ".join(authors), publisher, pubdate,
                    series_name, series_index, "; ".join(languages),
                    "; ".join(tags), json.dumps(identifiers, ensure_ascii=False),
                    comments, calibre_path, "", started
                ))
                book_id = cur.lastrowid

                formats = []
                for fmt, name, size in qall(src, """
                    SELECT format,name,uncompressed_size
                    FROM data
                    WHERE book=?
                    ORDER BY format
                """, (calibre_id,)):
                    formats.append(fmt.upper())
                    file_path = CALIBRE_LIBRARY / calibre_path / f"{name}.{fmt.lower()}"
                    exists = file_path.is_file()
                    digest = sha256(file_path) if exists else None

                    out.execute("""
                        INSERT INTO files(
                            book_id,calibre_format,file_name,file_path,
                            file_size,content_hash,exists_on_disk,scanned_at
                        ) VALUES (?,?,?,?,?,?,?,?)
                    """, (
                        book_id, fmt.upper(), name, str(file_path),
                        size if size is not None else (
                            file_path.stat().st_size if exists else None
                        ),
                        digest, int(exists), started
                    ))

                    files_total += 1
                    files_missing += int(not exists)

                out.execute(
                    "UPDATE books SET formats=? WHERE id=?",
                    (", ".join(formats), book_id)
                )

                out.commit()

            except Exception:
                errors += 1
                out.rollback()
                print(f"[ERRORE] calibre_id={calibre_id} title={title!r}")

        duplicate_files = q1(out, """
            SELECT COUNT(*) FROM (
                SELECT content_hash
                FROM files
                WHERE exists_on_disk=1 AND content_hash IS NOT NULL
                GROUP BY content_hash
                HAVING COUNT(*) > 1
            )
        """)[0]

        finished = datetime.now(timezone.utc).isoformat()
        out.execute("""
            UPDATE scan_runs
            SET finished_at=?, books_total=?, files_total=?,
                files_missing=?, duplicate_files=?, errors=?
            WHERE id=?
        """, (
            finished, len(books), files_total, files_missing,
            duplicate_files, errors, run_id
        ))
        out.commit()

        report = {
            "database": str(calibre_db),
            "output_database": str(DATABASE_PATH),
            "books_total": len(books),
            "files_total": files_total,
            "files_missing": files_missing,
            "duplicate_hash_groups": duplicate_files,
            "errors": errors,
            "finished_at": finished,
        }

        report_path = DATABASE_PATH.parent / "scan-report.json"
        report_path.write_text(
            json.dumps(report, indent=2, ensure_ascii=False),
            encoding="utf-8"
        )

        print("\n=== CALIBRE-AI SCAN ===")
        for k, v in report.items():
            print(f"{k:24} {v}")
        print(f"\nReport: {report_path}")
        print(f"DB AI:  {DATABASE_PATH}")

    finally:
        src.close()
        out.close()

if __name__ == "__main__":
    scan()
