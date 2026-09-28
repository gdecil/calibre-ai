import re
import sqlite3
from datetime import datetime, timezone

FTS_TABLE = "extracted_chunks_fts"


def build_match_query(query: str) -> str:
    terms = list(dict.fromkeys(re.findall(r"[\w]+", query, flags=re.UNICODE)))
    return " AND ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms)


def fulltext_table_exists(conn: sqlite3.Connection) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (FTS_TABLE,),
    ).fetchone() is not None


def ensure_fulltext_triggers(conn: sqlite3.Connection) -> None:
    if not fulltext_table_exists(conn):
        return

    conn.executescript(
        f"""
        CREATE TRIGGER IF NOT EXISTS extracted_chunks_fts_insert
        AFTER INSERT ON extracted_chunks BEGIN
            INSERT INTO {FTS_TABLE}(rowid, text) VALUES (new.id, new.text);
            UPDATE fulltext_index_state
            SET indexed_chunks = indexed_chunks + 1, updated_at = datetime('now')
            WHERE id = 1 AND state = 'ready';
        END;

        CREATE TRIGGER IF NOT EXISTS extracted_chunks_fts_delete
        AFTER DELETE ON extracted_chunks BEGIN
            INSERT INTO {FTS_TABLE}({FTS_TABLE}, rowid, text)
            VALUES ('delete', old.id, old.text);
            UPDATE fulltext_index_state
            SET indexed_chunks = MAX(indexed_chunks - 1, 0), updated_at = datetime('now')
            WHERE id = 1 AND state = 'ready';
        END;

        CREATE TRIGGER IF NOT EXISTS extracted_chunks_fts_update
        AFTER UPDATE OF text ON extracted_chunks BEGIN
            INSERT INTO {FTS_TABLE}({FTS_TABLE}, rowid, text)
            VALUES ('delete', old.id, old.text);
            INSERT INTO {FTS_TABLE}(rowid, text) VALUES (new.id, new.text);
        END;
        """
    )


def ensure_fulltext_index(conn: sqlite3.Connection) -> bool:
    created = not fulltext_table_exists(conn)
    conn.execute(
        f"""
        CREATE VIRTUAL TABLE IF NOT EXISTS {FTS_TABLE}
        USING fts5(text, content='extracted_chunks', content_rowid='id',
                   tokenize='unicode61 remove_diacritics 2')
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS fulltext_index_state (
            id INTEGER PRIMARY KEY CHECK(id = 1),
            state TEXT NOT NULL,
            indexed_chunks INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            error TEXT
        )
        """
    )
    ensure_fulltext_triggers(conn)
    return created


def rebuild_fulltext_index(conn: sqlite3.Connection) -> int:
    ensure_fulltext_index(conn)
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO fulltext_index_state(id, state, indexed_chunks, updated_at, error)
        VALUES (1, 'building', 0, ?, NULL)
        ON CONFLICT(id) DO UPDATE SET
            state = 'building', updated_at = excluded.updated_at, error = NULL
        """,
        (now,),
    )
    conn.commit()

    try:
        conn.execute(f"INSERT INTO {FTS_TABLE}({FTS_TABLE}) VALUES ('rebuild')")
        indexed_chunks = conn.execute(
            "SELECT COUNT(*) FROM extracted_chunks"
        ).fetchone()[0]
        conn.execute(
            """
            UPDATE fulltext_index_state
            SET state = 'ready', indexed_chunks = ?, updated_at = ?, error = NULL
            WHERE id = 1
            """,
            (indexed_chunks, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        return indexed_chunks
    except Exception as error:
        conn.rollback()
        conn.execute(
            """
            UPDATE fulltext_index_state
            SET state = 'error', updated_at = ?, error = ?
            WHERE id = 1
            """,
            (datetime.now(timezone.utc).isoformat(), str(error)),
        )
        conn.commit()
        raise