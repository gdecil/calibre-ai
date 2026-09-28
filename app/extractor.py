from __future__ import annotations

import argparse
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from html import unescape
from pathlib import Path

import pymupdf
from bs4 import BeautifulSoup
from ebooklib import epub, ITEM_DOCUMENT

from .config import DATABASE_PATH
from .fulltext import ensure_fulltext_triggers

# Evita crash della console Windows con caratteri Unicode non rappresentabili.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def clean(t: str) -> str:
    t = t.replace("\u00ad", "").replace("\r\n", "\n").replace("\r", "\n")
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n[ \t]+", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def wc(t: str) -> int:
    return len(re.findall(r"\b[\wÀ-ÖØ-öø-ÿ’'-]+\b", t, re.UNICODE))


def chunks(t: str, size: int = 6000, overlap: int = 800):
    if not t:
        return []
    if overlap >= size:
        raise ValueError("overlap deve essere inferiore a size")

    out = []
    start = 0

    while start < len(t):
        end = min(start + size, len(t))

        if end < len(t):
            pts = [
                t.rfind("\n\n", start + int(size * 0.65), end),
                t.rfind(". ", start + int(size * 0.65), end),
                t.rfind(" ", start + int(size * 0.65), end),
            ]
            boundary = max(pts)
            if boundary > start:
                end = boundary + (2 if t[boundary:boundary + 2] == "\n\n" else 1)

        piece = t[start:end].strip()
        if piece:
            out.append((start, end, piece))

        if end >= len(t):
            break

        start = max(0, end - overlap)

    return out


# ---------------------------------------------------------------------------
# EPUB
# ---------------------------------------------------------------------------

def epub_extract(path: Path):
    try:
        return epub_extract_ebooklib(path)
    except Exception as ebooklib_error:
        print(f"    EPUB fallback: ebooklib ha fallito: {ebooklib_error}")

        try:
            chapters = epub_extract_zip(path)
            if chapters:
                print(
                    f"    EPUB fallback: recuperati "
                    f"{len(chapters)} documenti HTML/XHTML"
                )
                return chapters, len(chapters), "epub_zip_fallback"

            raise RuntimeError(
                "fallback ZIP EPUB non ha trovato documenti HTML/XHTML"
            )

        except Exception as fallback_error:
            raise RuntimeError(
                f"ebooklib: {ebooklib_error}; fallback ZIP: {fallback_error}"
            )


def epub_extract_ebooklib(path: Path):
    book = epub.read_epub(str(path))
    out = []

    for item in book.get_items_of_type(ITEM_DOCUMENT):
        soup = BeautifulSoup(item.get_content(), "html.parser")

        for tag in soup(["script", "style", "nav"]):
            tag.decompose()

        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        text = clean(soup.get_text("\n", strip=True))

        if text:
            out.append({
                "title": title or item.get_name(),
                "page_start": None,
                "page_end": None,
                "text": text,
            })

    return out, len(out), "epub"


def epub_extract_zip(path: Path):
    candidates = []

    with zipfile.ZipFile(path, "r") as z:
        for info in z.infolist():
            if info.is_dir():
                continue

            name = info.filename.lower()
            if not name.endswith((".html", ".htm", ".xhtml", ".xht")):
                continue

            try:
                raw = z.read(info)
            except Exception as e:
                print(
                    f"    EPUB fallback: impossibile leggere "
                    f"{info.filename}: {e}"
                )
                continue

            text = None
            for encoding in ("utf-8", "utf-16", "cp1252", "latin-1"):
                try:
                    text = raw.decode(encoding)
                    break
                except UnicodeDecodeError:
                    continue

            if text is None:
                text = raw.decode("utf-8", errors="replace")

            try:
                soup = BeautifulSoup(text, "html.parser")

                for tag in soup(["script", "style", "nav"]):
                    tag.decompose()

                title = ""
                if soup.title:
                    title = soup.title.get_text(" ", strip=True)

                if not title:
                    heading = soup.find(["h1", "h2", "h3"])
                    if heading:
                        title = heading.get_text(" ", strip=True)

                body_text = clean(
                    unescape(soup.get_text("\n", strip=True))
                )

                if not body_text:
                    continue

                candidates.append({
                    "name": info.filename,
                    "title": title or Path(info.filename).name,
                    "text": body_text,
                })

            except Exception as e:
                print(
                    f"    EPUB fallback: impossibile analizzare "
                    f"{info.filename}: {e}"
                )

    candidates.sort(key=lambda x: x["name"].lower())

    chapters = [
        {
            "title": x["title"],
            "page_start": None,
            "page_end": None,
            "text": x["text"],
        }
        for x in candidates
    ]

    return chapters


def recover_epub_with_calibre(path: Path):
    converted, tempdir = convert(path)
    try:
        return epub_extract(converted)
    finally:
        shutil.rmtree(tempdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# TXT / LOG
# ---------------------------------------------------------------------------

def text_file_extract(path: Path):
    raw = path.read_bytes()

    text = None
    used_encoding = None

    for encoding in (
        "utf-8-sig",
        "utf-16",
        "utf-16-le",
        "utf-16-be",
        "cp1252",
        "latin-1",
    ):
        try:
            text = raw.decode(encoding)
            used_encoding = encoding
            break
        except UnicodeDecodeError:
            continue

    if text is None:
        text = raw.decode("utf-8", errors="replace")
        used_encoding = "utf-8-replace"

    text = clean(text)

    if not text:
        raise RuntimeError("file di testo vuoto")

    return [{
        "title": path.stem,
        "page_start": None,
        "page_end": None,
        "text": text,
    }], 1, f"text:{used_encoding}"


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------

def pdf_error_requires_ocr(error: Exception) -> bool:
    msg = str(error).lower()

    keywords = (
        "encrypted",
        "password",
        "protected",
        "document closed",
        "no text",
        "cannot extract text",
        "cannot open",
        "failed to open",
        "file is damaged",
        "damaged",
    )

    return any(k in msg for k in keywords)


def pdf_extract(path: Path):
    try:
        document = pymupdf.open(str(path))
    except Exception as e:
        if pdf_error_requires_ocr(e):
            raise RuntimeError(f"OCR_REQUIRED: {e}") from e
        raise

    try:
        if document.needs_pass:
            raise RuntimeError("OCR_REQUIRED: PDF encrypted/protected")

        chapters = []

        for page_number, page in enumerate(document, start=1):
            text = clean(page.get_text("text"))

            if not text:
                continue

            chapters.append({
                "title": f"Pagina {page_number}",
                "page_start": page_number,
                "page_end": page_number,
                "text": text,
            })

        if not chapters:
            raise RuntimeError("OCR_REQUIRED: PDF senza testo estraibile")

        return chapters, len(chapters), "pymupdf"

    except RuntimeError:
        raise
    except Exception as e:
        if pdf_error_requires_ocr(e):
            raise RuntimeError(f"OCR_REQUIRED: {e}") from e
        raise
    finally:
        document.close()


# ---------------------------------------------------------------------------
# Calibre conversion
# ---------------------------------------------------------------------------

def find_ebook_convert():
    cmd = shutil.which("ebook-convert")
    if cmd:
        return cmd

    candidates = [
        Path(r"C:\Program Files\Calibre2\ebook-convert.exe"),
        Path(r"C:\Program Files (x86)\Calibre2\ebook-convert.exe"),
        Path.home() / "AppData" / "Local" / "Programs" / "Calibre2" / "ebook-convert.exe",
        Path(r"X:\Program Files\Calibre2\ebook-convert.exe"),
    ]

    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)

    return None


def convert(path: Path):
    cmd = find_ebook_convert()

    if not cmd:
        raise RuntimeError(
            "ebook-convert non trovato. Installare Calibre oppure "
            "aggiungere Calibre2 al PATH."
        )

    tempdir = Path(tempfile.mkdtemp(prefix="calibre_ai_"))
    out = tempdir / (path.stem + ".epub")

    try:
        result = subprocess.run(
            [cmd, str(path), str(out)],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

        if result.returncode != 0:
            diagnostics = []

            if result.stdout.strip():
                diagnostics.append(
                    "STDOUT:\n" + result.stdout.strip()[-4000:]
                )

            if result.stderr.strip():
                diagnostics.append(
                    "STDERR:\n" + result.stderr.strip()[-4000:]
                )

            detail = "\n".join(diagnostics)

            raise RuntimeError(
                f"ebook-convert exit code {result.returncode}"
                + (f"\n{detail}" if detail else "")
            )

        if not out.is_file():
            raise RuntimeError(
                f"ebook-convert non ha prodotto il file EPUB: {out}"
            )

        return out, tempdir

    except Exception:
        shutil.rmtree(tempdir, ignore_errors=True)
        raise


# ---------------------------------------------------------------------------
# Formati
# ---------------------------------------------------------------------------

def classify_archive(fmt: str, path: Path):
    fmt = fmt.upper()

    if fmt == "CBR":
        raise RuntimeError(
            "OCR_REQUIRED: CBR contiene immagini/fumetti; "
            "serve OCR/analisi immagini."
        )

    if fmt in {"RAR", "ZIP"}:
        raise RuntimeError(
            f"ARCHIVE: archivio {fmt}; ispezione/conversione separata."
        )

    raise RuntimeError(f"Formato non gestito: {fmt}")


def extract_file(fmt: str, path: Path):
    fmt = fmt.upper()

    if fmt in {"TXT", "LOG"}:
        return text_file_extract(path)

    if fmt == "EPUB":
        try:
            return epub_extract(path)
        except Exception as direct_error:
            print(f"    EPUB Calibre recovery: {direct_error}")

            try:
                return recover_epub_with_calibre(path)
            except Exception as calibre_error:
                raise RuntimeError(
                    f"EPUB: direct={direct_error}; calibre={calibre_error}"
                ) from calibre_error

    if fmt == "PDF":
        return pdf_extract(path)

    if fmt in {
        "MOBI",
        "PRC",
        "AZW",
        "AZW3",
        "KFX",
        "DOC",
        "DOCX",
        "LIT",
        "ODT",
        "RTF",
    }:
        converted, tempdir = convert(path)

        try:
            chapters, count, extractor_name = epub_extract(converted)
            return chapters, count, f"calibre->{extractor_name}"
        finally:
            shutil.rmtree(tempdir, ignore_errors=True)

    if fmt in {"CBR", "RAR", "ZIP"}:
        return classify_archive(fmt, path)

    raise RuntimeError(f"Formato non gestito: {fmt}")


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def delete_previous_extraction(conn: sqlite3.Connection, book_id: int):
    """
    Schema reale del DB V1:

        books.id
          -> extracted_books.book_id

        extracted_books.id
          -> extracted_chapters.extracted_book_id
          -> extracted_chunks.extracted_book_id

    Cancella soltanto l'estrazione del libro indicato.
    """

    rows = conn.execute(
        "SELECT id FROM extracted_books WHERE book_id = ?",
        (book_id,),
    ).fetchall()

    for (extracted_book_id,) in rows:
        conn.execute(
            "DELETE FROM extracted_chunks WHERE extracted_book_id = ?",
            (extracted_book_id,),
        )

        conn.execute(
            "DELETE FROM extracted_chapters WHERE extracted_book_id = ?",
            (extracted_book_id,),
        )

        conn.execute(
            "DELETE FROM extracted_books WHERE id = ?",
            (extracted_book_id,),
        )


def insert_extraction(
    conn: sqlite3.Connection,
    book_id: int,
    source_file_id: int,
    status: str,
    extractor_name: str | None = None,
    language: str | None = None,
    chars: int = 0,
    words: int = 0,
    chapter_count: int = 0,
    pages: int = 0,
    error: str | None = None,
    chapters=None,
    chunk_size: int = 6000,
    overlap: int = 800,
):
    now = datetime.now(timezone.utc).isoformat()

    cursor = conn.execute(
        """
        INSERT INTO extracted_books (
            book_id,
            source_file_id,
            status,
            extractor,
            language,
            chars,
            words,
            chapters,
            pages,
            error,
            extracted_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            book_id,
            source_file_id,
            status,
            extractor_name,
            language,
            chars,
            words,
            chapter_count,
            pages,
            error,
            now,
        ),
    )

    extracted_book_id = cursor.lastrowid

    if status != "OK" or not chapters:
        return extracted_book_id

    for chapter_number, chapter in enumerate(chapters, start=1):
        text = chapter["text"]

        chapter_cursor = conn.execute(
            """
            INSERT INTO extracted_chapters (
                extracted_book_id,
                chapter_number,
                title,
                page_start,
                page_end,
                text,
                chars,
                words
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                extracted_book_id,
                chapter_number,
                chapter.get("title"),
                chapter.get("page_start"),
                chapter.get("page_end"),
                text,
                len(text),
                wc(text),
            ),
        )

        chapter_id = chapter_cursor.lastrowid

        for local_chunk_number, (start_char, end_char, piece) in enumerate(
            chunks(text, chunk_size, overlap),
            start=1,
        ):
            conn.execute(
                """
                INSERT INTO extracted_chunks (
                    extracted_book_id,
                    chapter_id,
                    chunk_number,
                    start_char,
                    end_char,
                    text,
                    chars,
                    words
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    extracted_book_id,
                    chapter_id,
                    local_chunk_number,
                    start_char,
                    end_char,
                    piece,
                    len(piece),
                    wc(piece),
                ),
            )

    return extracted_book_id


def run(limit: int = 20, chunk_size: int = 6000, overlap: int = 800):
    if limit is None:
        limit = 20

    if limit <= 0:
        raise ValueError("limit deve essere > 0")

    if chunk_size <= 0:
        raise ValueError("chunk-size deve essere > 0")

    if overlap < 0 or overlap >= chunk_size:
        raise ValueError(
            "overlap deve essere >= 0 e inferiore a chunk-size"
        )

    db_path = Path(DATABASE_PATH)
    conn = sqlite3.connect(str(db_path))
    ensure_fulltext_triggers(conn)

    # Un record per libro: la JOIN evita di moltiplicare i libri in presenza
    # di più formati e considera solo il formato prioritario.
    rows = conn.execute(
        """
        SELECT
            b.id,
            b.calibre_id,
            b.title,
            b.authors,
            f.id,
            f.calibre_format,
            f.file_path
        FROM books b
        JOIN files f ON f.book_id = b.id
        LEFT JOIN extracted_books eb ON eb.book_id = b.id
        WHERE
            f.exists_on_disk = 1
            AND (
                eb.book_id IS NULL
                OR eb.status IN ('FAILED', 'OCR_REQUIRED')
            )
        GROUP BY b.id
        ORDER BY
            b.calibre_id,
            CASE f.calibre_format
                WHEN 'EPUB' THEN 1
                WHEN 'PDF' THEN 2
                WHEN 'AZW3' THEN 3
                WHEN 'AZW' THEN 4
                WHEN 'MOBI' THEN 5
                ELSE 99
            END
        LIMIT ?
        """,
        (limit,),
    ).fetchall()

    print(f"Da elaborare: {len(rows)}")

    counts = {
        "OK": 0,
        "FAILED": 0,
        "OCR_REQUIRED": 0,
        "UNSUPPORTED": 0,
    }

    try:
        for index, row in enumerate(rows, start=1):
            (
                book_id,
                calibre_id,
                title,
                authors,
                source_file_id,
                fmt,
                file_path,
            ) = row

            print()
            print(f"[{index}/{len(rows)}] {title} [{fmt}]")
            print(f"    File: {file_path}")

            # Retry: rimuove solo i dati estratti di questo libro.
            delete_previous_extraction(conn, book_id)
            conn.commit()

            try:
                path = Path(file_path)

                if not path.is_file():
                    raise RuntimeError(
                        f"File non trovato: {path}"
                    )

                chapters, chapter_count, extractor_name = extract_file(
                    fmt,
                    path,
                )

                total_chars = sum(len(c["text"]) for c in chapters)
                total_words = sum(wc(c["text"]) for c in chapters)

                pages = sum(
                    1
                    for c in chapters
                    if c.get("page_start") is not None
                )

                total_chunks = sum(
                    len(chunks(c["text"], chunk_size, overlap))
                    for c in chapters
                )

                insert_extraction(
                    conn,
                    book_id=book_id,
                    source_file_id=source_file_id,
                    status="OK",
                    extractor_name=extractor_name,
                    chars=total_chars,
                    words=total_words,
                    chapter_count=chapter_count,
                    pages=pages,
                    chapters=chapters,
                    chunk_size=chunk_size,
                    overlap=overlap,
                )

                conn.commit()

                counts["OK"] += 1

                print(
                    f"    OK: chapters={chapter_count} "
                    f"chars={total_chars:,} "
                    f"words={total_words:,} "
                    f"chunks={total_chunks:,}"
                )

            except Exception as error:
                message = str(error).strip() or repr(error)

                if "OCR_REQUIRED" in message:
                    status = "OCR_REQUIRED"
                elif (
                    message.startswith("ARCHIVE:")
                    or "Formato non gestito" in message
                ):
                    status = "UNSUPPORTED"
                else:
                    status = "FAILED"

                insert_extraction(
                    conn,
                    book_id=book_id,
                    source_file_id=source_file_id,
                    status=status,
                    error=message,
                )

                conn.commit()

                counts[status] += 1

                print(f"    {status}: {message}")

    finally:
        conn.close()

    print()
    print("--- RISULTATO BATCH ---")
    for status in ("OK", "OCR_REQUIRED", "UNSUPPORTED", "FAILED"):
        print(f"{status}: {counts[status]}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--chunk-size", type=int, default=6000)
    parser.add_argument("--overlap", type=int, default=800)
    args = parser.parse_args()

    run(args.limit, args.chunk_size, args.overlap)
