import sqlite3
import unittest
from unittest.mock import Mock, patch

from app.embeddings import get_model_digest
from app.indexer import checkpoint, ensure_embedding_index_schema, get_page


class IndexerProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(
            """
            CREATE TABLE embedding_index (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chunk_id INTEGER NOT NULL,
                chunk_hash TEXT NOT NULL,
                model TEXT NOT NULL,
                dimensions INTEGER,
                collection TEXT NOT NULL,
                indexed_at TEXT NOT NULL,
                UNIQUE(chunk_id, model, collection)
            );
            CREATE TABLE extracted_chunks (
                id INTEGER PRIMARY KEY,
                text TEXT,
                extracted_book_id INTEGER,
                chapter_id INTEGER
            );
            CREATE TABLE extracted_books (id INTEGER PRIMARY KEY, book_id INTEGER);
            CREATE TABLE extracted_chapters (
                id INTEGER PRIMARY KEY,
                chapter_number INTEGER,
                title TEXT
            );
            CREATE TABLE books (id INTEGER PRIMARY KEY, title TEXT, authors TEXT);
            INSERT INTO books VALUES (1, 'Book', 'Author');
            INSERT INTO extracted_books VALUES (10, 1);
            INSERT INTO extracted_chapters VALUES (20, 5, 'Chapter');
            INSERT INTO extracted_chunks VALUES (1, 'old', 10, 20);
            INSERT INTO extracted_chunks VALUES (2, 'current', 10, 20);
            INSERT INTO extracted_chunks VALUES (3, 'stale', 10, 20);
            INSERT INTO extracted_chunks VALUES (4, 'missing', 10, 20);
            INSERT INTO embedding_index
                (chunk_id, chunk_hash, model, dimensions, collection, indexed_at)
            VALUES (1, 'a', 'qwen3-embedding', 4096, 'calibre_chunks_disk', '2026-01-01');
            """
        )
        ensure_embedding_index_schema(self.conn)
        self.conn.execute(
            """
            INSERT INTO embedding_index
                (chunk_id, chunk_hash, model, dimensions, collection, indexed_at,
                 model_digest, indexer_version)
            VALUES (2, 'b', 'qwen3-embedding', 4096, 'calibre_chunks_disk',
                    '2026-01-02', 'digest-current', '1')
            """
        )
        self.conn.execute(
            """
            INSERT INTO embedding_index
                (chunk_id, chunk_hash, model, dimensions, collection, indexed_at,
                 model_digest, indexer_version)
            VALUES (3, 'c', 'qwen3-embedding', 4096, 'calibre_chunks_disk',
                    '2026-01-03', 'digest-old', '1')
            """
        )

    def tearDown(self):
        self.conn.close()

    def test_resume_skips_legacy_and_current_rows_but_reindexes_old_digest(self):
        rows = get_page(
            self.conn,
            0,
            'qwen3-embedding',
            'digest-current',
            'calibre_chunks_disk',
            10,
        )

        self.assertEqual([row[0] for row in rows], [3, 4])

    def test_model_digest_comes_from_exact_ollama_tag(self):
        response = Mock()
        response.json.return_value = {
            'models': [
                {'name': 'qwen3-embedding:latest', 'digest': 'sha256:verified'}
            ]
        }

        with (
            patch('app.embeddings.EMBED_MODEL', 'qwen3-embedding'),
            patch('app.embeddings.OLLAMA_URL', 'http://localhost:11434/api/embed'),
            patch('app.embeddings.requests.get', return_value=response) as get,
        ):
            self.assertEqual(get_model_digest(), 'sha256:verified')

        get.assert_called_once_with('http://localhost:11434/api/tags', timeout=30)

    def test_checkpoint_persists_model_and_pipeline_versions(self):
        checkpoint(
            self.conn,
            [(4, 'indexed text')],
            'qwen3-embedding',
            4096,
            'sha256:verified',
            'calibre_chunks_disk',
        )
        record = self.conn.execute(
            """
            SELECT model, dimensions, model_digest, indexer_version
            FROM embedding_index
            WHERE chunk_id = 4
            """
        ).fetchone()

        self.assertEqual(record, ('qwen3-embedding', 4096, 'sha256:verified', '1'))


if __name__ == '__main__':
    unittest.main()