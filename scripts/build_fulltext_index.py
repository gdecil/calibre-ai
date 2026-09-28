import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import sqlite3

from app.config import DATABASE_PATH
from app.fulltext import rebuild_fulltext_index


def main():
    if not DATABASE_PATH.exists():
        raise FileNotFoundError(f"Database non trovato: {DATABASE_PATH}")

    conn = sqlite3.connect(DATABASE_PATH, timeout=60)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        print(f"Costruzione indice full-text su {DATABASE_PATH}...", flush=True)
        indexed_chunks = rebuild_fulltext_index(conn)
        print(f"Indice full-text pronto: {indexed_chunks:,} chunk", flush=True)
    finally:
        conn.close()


if __name__ == "__main__":
    main()