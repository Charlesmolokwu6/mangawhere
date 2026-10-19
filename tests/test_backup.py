import io
import json
import shutil
import sqlite3
import tarfile
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import backup
from server import db


class BackupTests(unittest.TestCase):
    def setUp(self):
        self._dir = Path(tempfile.mkdtemp())
        self._db = self._dir / "live.db"
        db.DB_PATH = self._db
        db.init_db()
        conn = db.get_connection()
        conn.execute("INSERT INTO users (email, name, created_at) VALUES ('a@b.c', 'A', 1)")
        conn.execute("INSERT INTO scrape_cache (key, value, expires_at) VALUES ('k', 'v', 9e9)")
        conn.commit()
        conn.close()
        self.out = self._dir / "out"

    def tearDown(self):
        shutil.rmtree(self._dir, ignore_errors=True)

    def _members(self, data: bytes) -> dict:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            return {m.name: tar.extractfile(m).read() for m in tar.getmembers()}

    def test_unencrypted_backup_holds_the_database_and_code(self):
        path = backup.main(["--out", str(self.out), "--no-encrypt"])
        files = self._members(path.read_bytes())
        self.assertEqual(set(files), {"database.sql", "code.tar", "manifest.json"})
        manifest = json.loads(files["manifest.json"])
        self.assertEqual(manifest["rows"]["users"], 1)
        self.assertNotIn("scrape_cache", manifest["rows"])

        # The dump restores into a working database, minus the throwaway rows.
        restored = sqlite3.connect(":memory:")
        restored.executescript(files["database.sql"].decode())
        self.assertEqual(restored.execute("SELECT email FROM users").fetchall(), [("a@b.c",)])
        self.assertEqual(restored.execute("SELECT COUNT(*) FROM scrape_cache").fetchone(), (0,))

    @unittest.skipUnless(shutil.which("openssl"), "needs openssl")
    def test_encrypted_backup_opens_with_the_passphrase_only(self):
        with patch.dict("os.environ", {"BACKUP_PASSPHRASE": "correct horse"}):
            path = backup.main(["--out", str(self.out)])
        self.assertTrue(path.name.endswith(".tar.gz.enc"))
        data = path.read_bytes()
        self.assertNotIn(b"a@b.c", data)
        self.assertIn("database.sql", self._members(backup.decrypt(data, "correct horse")))
        with self.assertRaises(Exception):
            backup.decrypt(data, "wrong")

    def test_refuses_to_write_plaintext_without_being_asked(self):
        with patch.dict("os.environ", {"BACKUP_PASSPHRASE": ""}):
            with self.assertRaises(SystemExit):
                backup.main(["--out", str(self.out)])
        self.assertFalse(self.out.exists())

    def test_keeps_only_the_newest(self):
        self.out.mkdir()
        for i in range(5):
            (self.out / f"mangawhere-2026010{i}-000000.tar.gz.enc").write_bytes(b"x")
        (self.out / "unrelated.txt").write_text("keep me")
        removed = backup.prune(self.out, 2)
        self.assertEqual(len(removed), 3)
        self.assertEqual(sorted(p.name for p in self.out.iterdir()),
                         ["mangawhere-20260103-000000.tar.gz.enc",
                          "mangawhere-20260104-000000.tar.gz.enc", "unrelated.txt"])


if __name__ == "__main__":
    unittest.main()
