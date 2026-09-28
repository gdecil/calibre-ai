import argparse
import json
import sqlite3
import time
from pathlib import Path

import requests

from app.config import DATABASE_PATH, QDRANT_COLLECTION, QDRANT_URL


QDRANT_BASE = QDRANT_URL.rstrip('/')


def scroll_points(offset, limit):
    response = requests.post(
        f"{QDRANT_BASE}/collections/{QDRANT_COLLECTION}/points/scroll",
        json={
            "offset": offset,
            "limit": limit,
            "with_payload": False,
            "with_vector": False,
        },
        timeout=60,
    )
    response.raise_for_status()
    return response.json()["result"]


def load_metadata(database, point_ids):
    placeholders = ",".join("?" for _ in point_ids)
    rows = database.execute(
        f"""
        SELECT
            c.id,
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
        WHERE c.id IN ({placeholders})
        """,
        point_ids,
    ).fetchall()
    return {row[0]: row[1:] for row in rows}


def update_group(point_ids, metadata):
    payload = {
        "book_id": metadata[0],
        "chapter_id": metadata[1],
        "chapter_number": metadata[2],
        "chapter_title": metadata[3],
        "book_title": metadata[4],
        "authors": metadata[5],
    }
    response = requests.put(
        f"{QDRANT_BASE}/collections/{QDRANT_COLLECTION}/points/payload?wait=true",
        json={"points": point_ids, "payload": payload},
        timeout=120,
    )
    response.raise_for_status()


def main():
    parser = argparse.ArgumentParser(
        description="Aggiorna i metadati dei punti Qdrant senza ricalcolare embedding"
    )
    parser.add_argument("--page-size", type=int, default=1000)
    parser.add_argument("--update-batch-size", type=int, default=256)
    parser.add_argument(
        "--state-file",
        default="data/backfill_qdrant_payload.state.json",
        help="File usato per riprendere dopo un'interruzione",
    )
    args = parser.parse_args()

    database = sqlite3.connect(DATABASE_PATH)
    state_path = Path(args.state_file)
    offset = None
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))
        offset = state.get("next_page_offset")
        print(f"Resuming from offset={offset}")

    processed = updated = missing = 0
    started = time.time()

    try:
        while True:
            result = scroll_points(offset, args.page_size)
            points = result["points"]
            if not points:
                break

            point_ids = [point["id"] for point in points]
            metadata = load_metadata(database, point_ids)
            groups = {}
            for point_id in point_ids:
                row = metadata.get(point_id)
                if row is None:
                    missing += 1
                    continue
                groups.setdefault(row, []).append(point_id)

            for row, ids in groups.items():
                for start in range(0, len(ids), args.update_batch_size):
                    update_group(ids[start:start + args.update_batch_size], row)
                    updated += len(ids[start:start + args.update_batch_size])

            processed += len(points)
            elapsed = time.time() - started
            print(
                f"processed={processed} updated={updated} missing={missing} "
                f"rate={processed / elapsed:.1f} points/s",
                flush=True,
            )

            offset = result.get("next_page_offset")
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(
                json.dumps({"next_page_offset": offset}),
                encoding="utf-8",
            )
            if offset is None:
                break
    finally:
        database.close()

    state_path.unlink(missing_ok=True)
    print(f"DONE processed={processed} updated={updated} missing={missing}")


if __name__ == "__main__":
    main()
