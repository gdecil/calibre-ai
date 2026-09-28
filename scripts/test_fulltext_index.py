import sqlite3
import unittest

from app.fulltext import (
    FTS_TABLE,
    build_match_query,
    ensure_fulltext_index,
    rebuild_fulltext_index,
)


class FullTextIndexTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(
            """
            CREATE TABLE extracted_chunks (
                id INTEGER PRIMARY KEY,
                text TEXT NOT NULL
            );
            INSERT INTO extracted_chunks VALUES (1, 'Parker entra nella stanza.');
            INSERT INTO extracted_chunks VALUES (2, 'Un altro personaggio entra.');
            """
        )
        ensure_fulltext_index(self.conn)
        rebuild_fulltext_index(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_search_matches_name_and_quotes_query_terms(self):
        match_query = build_match_query('Parker protagonista')
        rows = self.conn.execute(
            f"SELECT rowid FROM {FTS_TABLE} WHERE {FTS_TABLE} MATCH ?",
            ('Parker',),
        ).fetchall()

        self.assertEqual(match_query, '"Parker" AND "protagonista"')
        self.assertEqual(rows, [(1,)])

    def test_chunk_triggers_keep_fulltext_index_in_sync(self):
        self.conn.execute(
            "INSERT INTO extracted_chunks VALUES (?, ?)",
            (3, 'Parker ritorna.'),
        )
        inserted = self.conn.execute(
            f"SELECT rowid FROM {FTS_TABLE} WHERE {FTS_TABLE} MATCH ?",
            ('Parker',),
        ).fetchall()
        self.assertEqual(inserted, [(1,), (3,)])

        self.conn.execute(
            "UPDATE extracted_chunks SET text = ? WHERE id = ?",
            ('Nuovo testo.', 1),
        )
        after_update = self.conn.execute(
            f"SELECT rowid FROM {FTS_TABLE} WHERE {FTS_TABLE} MATCH ?",
            ('Parker',),
        ).fetchall()
        self.assertEqual(after_update, [(3,)])

        self.conn.execute("DELETE FROM extracted_chunks WHERE id = ?", (3,))
        after_delete = self.conn.execute(
            f"SELECT rowid FROM {FTS_TABLE} WHERE {FTS_TABLE} MATCH ?",
            ('Parker',),
        ).fetchall()
        self.assertEqual(after_delete, [])


if __name__ == "__main__":
    unittest.main()