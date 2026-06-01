import tempfile
import unittest
from pathlib import Path

from ai4research.store import connect, init_schema


class StoreTest(unittest.TestCase):
    def test_schema_creates_all_tables_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "ai4research.db"
            conn = connect(db)
            try:
                init_schema(conn)
                n = conn.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
                ).fetchone()[0]
                self.assertGreaterEqual(n, 29)
                # foreign keys are enforced
                self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
                # idempotent: a second init on a populated db is a no-op (does not raise)
                init_schema(conn)
                self.assertEqual(
                    conn.execute(
                        "SELECT value FROM schema_meta WHERE key='schema_version'"
                    ).fetchone()[0],
                    "0.1.0",
                )
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
