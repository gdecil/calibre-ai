import sqlite3
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import DATABASE_PATH, QDRANT_COLLECTION, QDRANT_URL

DB_PATH = str(DATABASE_PATH)
COLLECTION = QDRANT_COLLECTION

conn = sqlite3.connect(DB_PATH)
sqlite_count = conn.execute(
    "SELECT COUNT(*) FROM embedding_index WHERE collection = ?",
    (COLLECTION,),
).fetchone()[0]

r = requests.get(
    f"{QDRANT_URL.rstrip('/')}/collections/{COLLECTION}",
    timeout=30,
)
r.raise_for_status()
data = r.json()["result"]

qdrant_count = data["points_count"]
indexed_vectors = data.get("indexed_vectors_count", 0)
status = data["status"]

print(f"SQLite indexed: {sqlite_count}")
print(f"Qdrant points: {qdrant_count}")
print(f"Qdrant indexed vectors: {indexed_vectors}")
print(f"Qdrant status: {status}")
print(f"Difference Qdrant-SQLite: {qdrant_count - sqlite_count}")
