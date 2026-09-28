import argparse
import hashlib
import sqlite3
import time

import requests

from .config import DATABASE_PATH, QDRANT_COLLECTION, QDRANT_URL
from .embeddings import embed_texts, get_model_digest, EMBED_MODEL

DB_PATH = str(DATABASE_PATH)
COLLECTION = QDRANT_COLLECTION
PAGE_SIZE = 1000
DEFAULT_BATCH_SIZE = 16
DEFAULT_QDRANT_BATCH_SIZE = 16
INDEXER_VERSION = "1"


def ensure_embedding_index_schema(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS embedding_index (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chunk_id INTEGER NOT NULL,
            chunk_hash TEXT NOT NULL,
            model TEXT NOT NULL,
            dimensions INTEGER,
            collection TEXT NOT NULL,
            indexed_at TEXT NOT NULL,
            model_digest TEXT,
            indexer_version TEXT,
            UNIQUE(chunk_id, model, collection)
        )
        """
    )
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(embedding_index)")
    }
    if "model_digest" not in columns:
        conn.execute("ALTER TABLE embedding_index ADD COLUMN model_digest TEXT")
    if "indexer_version" not in columns:
        conn.execute("ALTER TABLE embedding_index ADD COLUMN indexer_version TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_embedding_index_chunk "
        "ON embedding_index(chunk_id)"
    )
    conn.commit()


def chunk_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def qdrant_upsert(points, retries=5):
    url = f"{QDRANT_URL.rstrip('/')}/collections/{COLLECTION}/points?wait=true"
    payload = {"points": points}

    delay = 1.0
    for attempt in range(retries):
        try:
            r = requests.put(url, json=payload, timeout=120)
            r.raise_for_status()
            return
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(delay)
            delay *= 2


def ensure_collection(dimensions):
    url = f"{QDRANT_URL.rstrip('/')}/collections/{COLLECTION}"
    r = requests.get(url, timeout=30)
    if r.status_code == 200:
        return

    r = requests.put(
        url,
        json={
            "vectors": {
                "size": dimensions,
                "distance": "Cosine",
                "on_disk": True,
            },
            "hnsw_config": {"on_disk": True},
            "on_disk_payload": True,
        },
        timeout=30,
    )
    r.raise_for_status()


def get_page(conn, start_id, model, model_digest, collection, limit):
    return conn.execute(
        """
        SELECT
            c.id,
            c.text,
            eb.book_id,
            c.chapter_id,
            ch.chapter_number,
            ch.title,
            b.title,
            b.authors
        FROM extracted_chunks c
        JOIN extracted_books eb ON eb.id = c.extracted_book_id
        LEFT JOIN extracted_chapters ch ON ch.id = c.chapter_id
        LEFT JOIN books b ON b.id = eb.book_id
        WHERE c.id >= ?
          AND NOT EXISTS (
              SELECT 1
              FROM embedding_index ei
              WHERE ei.chunk_id = c.id
                AND ei.model = ?
                AND ei.collection = ?
                                AND (ei.model_digest IS NULL OR ei.model_digest = ?)
          )
        ORDER BY c.id
        LIMIT ?
        """,
        (start_id, model, collection, model_digest, limit),
    ).fetchall()


def checkpoint(conn, rows, model, dimensions, model_digest, collection):
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    conn.executemany(
        """
        INSERT INTO embedding_index
            (chunk_id, chunk_hash, model, dimensions, collection, indexed_at,
             model_digest, indexer_version)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(chunk_id, model, collection) DO UPDATE SET
            chunk_hash=excluded.chunk_hash,
            dimensions=excluded.dimensions,
            model_digest=excluded.model_digest,
            indexer_version=excluded.indexer_version,
            indexed_at=excluded.indexed_at
        """,
        [
            (
                row[0],
                chunk_hash(row[1]),
                model,
                dimensions,
                collection,
                now,
                model_digest,
                INDEXER_VERSION,
            )
            for row in rows
        ],
    )
    conn.commit()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start-chunk-id", type=int, default=None)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    ap.add_argument(
        "--qdrant-batch-size",
        type=int,
        default=DEFAULT_QDRANT_BATCH_SIZE,
    )
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    ensure_embedding_index_schema(conn)
    model_digest = get_model_digest()
    print(
        f"Embedding model={EMBED_MODEL} digest={model_digest} "
        f"indexer_version={INDEXER_VERSION}"
    )

    if args.start_chunk_id is None:
        next_id = 0
        print("Mode: AUTO-RESUME (primo chunk mancante)")
    else:
        next_id = args.start_chunk_id
        print(f"Mode: START-ID {next_id}")

    remaining = args.limit
    indexed = 0
    started = time.time()

    while True:
        page_limit = PAGE_SIZE if remaining is None else min(PAGE_SIZE, remaining)
        if page_limit <= 0:
            break

        rows = get_page(
            conn,
            next_id,
            EMBED_MODEL,
            model_digest,
            COLLECTION,
            page_limit,
        )

        if not rows:
            break

        for pos in range(0, len(rows), args.batch_size):
            batch = rows[pos:pos + args.batch_size]
            texts = [row[1] for row in batch]

            embeddings = embed_texts(texts, timeout=600)
            dimensions = len(embeddings[0])
            ensure_collection(dimensions)

            for qpos in range(0, len(batch), args.qdrant_batch_size):
                qbatch = batch[qpos:qpos + args.qdrant_batch_size]
                qemb = embeddings[qpos:qpos + args.qdrant_batch_size]

                points = [
                    {
                        "id": row[0],
                        "vector": vector,
                        "payload": {
                            "chunk_id": row[0],
                            "book_id": row[2],
                            "chapter_id": row[3],
                            "chapter_number": row[4],
                            "chapter_title": row[5],
                            "book_title": row[6],
                            "authors": row[7],
                        },
                    }
                    for row, vector in zip(qbatch, qemb)
                ]
                qdrant_upsert(points)

            checkpoint(
                conn,
                batch,
                EMBED_MODEL,
                dimensions,
                model_digest,
                COLLECTION,
            )

            indexed += len(batch)
            elapsed = time.time() - started
            rate = indexed / elapsed if elapsed else 0
            last_id = batch[-1][0]
            print(
                f"indexed={indexed} last_chunk_id={last_id} "
                f"rate={rate:.2f} chunk/s",
                flush=True,
            )

            if remaining is not None:
                remaining -= len(batch)
                if remaining <= 0:
                    break

        next_id = rows[-1][0] + 1

        if remaining is not None and remaining <= 0:
            break

    elapsed = time.time() - started
    rate = indexed / elapsed if elapsed else 0
    print(
        f"DONE indexed={indexed} elapsed={elapsed:.1f}s "
        f"rate={rate:.2f} chunk/s"
    )


if __name__ == "__main__":
    main()
