import importlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest


class TestSourcesMigration(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_cwd = os.getcwd()
        os.chdir(self._tmp.name)

    def tearDown(self):
        os.chdir(self._old_cwd)
        self._tmp.cleanup()
        for name in ("scanner", "videoqueue"):
            sys.modules.pop(name, None)

    def _load_scanner(self):
        for name in ("scanner", "videoqueue"):
            sys.modules.pop(name, None)
        import scanner  # noqa: PLC0415
        return importlib.reload(scanner)

    def test_imports_legacy_sources_once(self):
        os.makedirs("data", exist_ok=True)
        with open("data/sources.json", "w", encoding="utf-8") as f:
            json.dump([
                {"url": "https://www.youtube.com/@a", "name": "A", "start_after": 123},
            ], f)

        scanner = self._load_scanner()
        sources = scanner.load_sources()
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["name"], "A")
        self.assertIn("id", sources[0])

        # Changing legacy file should not overwrite already-migrated DB rows.
        with open("data/sources.json", "w", encoding="utf-8") as f:
            json.dump([
                {"url": "https://www.youtube.com/@b", "name": "B", "start_after": 999},
            ], f)

        sources2 = scanner.load_sources()
        self.assertEqual(len(sources2), 1)
        self.assertEqual(sources2[0]["name"], "A")

    def test_existing_sources_table_wins_over_legacy_file(self):
        os.makedirs("data", exist_ok=True)
        with open("data/sources.json", "w", encoding="utf-8") as f:
            json.dump([
                {"url": "https://www.youtube.com/@legacy", "name": "Legacy"},
            ], f)

        # Pre-seed DB source row.
        conn = sqlite3.connect("data/queue.db")
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations(name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sources (
              id TEXT PRIMARY KEY,
              url TEXT NOT NULL UNIQUE,
              name TEXT NOT NULL,
              start_after INTEGER,
              title_include TEXT,
              title_exclude TEXT,
              description_include TEXT,
              description_exclude TEXT,
              min_minutes INTEGER,
              max_minutes INTEGER,
              retention_days INTEGER,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT INTO sources(id,url,name,created_at,updated_at) VALUES (?,?,?,?,?)",
            ("src-1", "https://www.youtube.com/@db", "DB", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
        )
        conn.commit()
        conn.close()

        scanner = self._load_scanner()
        sources = scanner.load_sources()
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["name"], "DB")
        self.assertEqual(sources[0]["id"], "src-1")


if __name__ == "__main__":
    unittest.main()
