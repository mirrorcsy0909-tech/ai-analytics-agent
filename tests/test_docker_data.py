"""Verify database loading without Docker or private data."""

import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from eval.phase0_baseline import create_fixture
from scripts.load_docker_database import load_database


class DockerDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.source = Path(self.temp.name)/'source.db'
        create_fixture(self.source)

    def test_snapshot_has_expected_rows_and_source_is_unchanged(self):
        original = hashlib.sha256(self.source.read_bytes()).hexdigest()
        uploads = []
        def run(args, **kwargs):
            if 'stdin' in kwargs:
                uploaded = Path(self.temp.name)/'uploaded.db'
                uploaded.write_bytes(kwargs['stdin'].read())
                with sqlite3.connect(uploaded) as conn:
                    uploads.append(conn.execute('SELECT COUNT(*), SUM(Revenue) FROM transactions').fetchone())
            return SimpleNamespace(stdout='', returncode=0)
        with patch('scripts.load_docker_database.subprocess.run', side_effect=run):
            load_database(self.source, volume='test-data')
        self.assertEqual(uploads, [(8, 138.0)])
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), original)

    def test_running_consumer_blocks_database_replacement(self):
        with patch('scripts.load_docker_database.subprocess.run', return_value=SimpleNamespace(stdout='running-container')) as run:
            with self.assertRaises(RuntimeError): load_database(self.source)
        run.assert_called_once()

    def test_missing_source_is_not_created(self):
        path = self.source.parent/'missing.db'
        with patch('scripts.load_docker_database.subprocess.run') as run:
            with self.assertRaises(FileNotFoundError): load_database(path)
        run.assert_not_called()
        self.assertFalse(path.exists())

    def test_invalid_schema_stops_before_touching_docker_volume(self):
        bad = self.source.parent/'bad.db'
        sqlite3.connect(bad).close()
        with patch('scripts.load_docker_database.subprocess.run', return_value=SimpleNamespace(stdout='')) as run:
            with self.assertRaises(sqlite3.Error): load_database(bad)
        run.assert_called_once()


if __name__ == '__main__':
    unittest.main()
