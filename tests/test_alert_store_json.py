import unittest
import tempfile
import os
import time
import json
import shutil

from refactor.services.alert_store_json import JsonAlertStore


class TestJsonAlertStore(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.path = os.path.join(self.tmpdir, "alert_store.json")

    def tearDown(self):
        try:
            shutil.rmtree(self.tmpdir)
        except Exception:
            pass

    def test_record_and_persist_and_cooldown(self):
        store = JsonAlertStore(path=self.path)
        key = "test:key"
        # initially allowed
        self.assertTrue(store.should_alert(key, 10))
        store.record_alert(key)
        # right after recording, large cooldown prevents alert
        self.assertFalse(store.should_alert(key, 3600))
        # new instance reads persisted timestamp
        store2 = JsonAlertStore(path=self.path)
        self.assertFalse(store2.should_alert(key, 3600))
        # simulate older timestamp on disk
        data = store2._data.copy()
        data[key] = time.time() - 7200
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(data, f)
        store3 = JsonAlertStore(path=self.path)
        self.assertTrue(store3.should_alert(key, 3600))

    def test_corrupted_file_resets(self):
        # write invalid JSON
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("not a json")
        # should not raise
        store = JsonAlertStore(path=self.path)
        self.assertTrue(store.should_alert("any", 1))

    def test_cleanup_removes_old(self):
        store = JsonAlertStore(path=self.path)
        key_old = "old"
        key_new = "new"
        store._data[key_old] = time.time() - 10_000
        store._data[key_new] = time.time()
        store._save()
        store.cleanup(older_than_sec=3600)
        self.assertNotIn(key_old, store._data)
        self.assertIn(key_new, store._data)


if __name__ == "__main__":
    unittest.main()
