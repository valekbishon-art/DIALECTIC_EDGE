"""Simple JSON-backed alert store for cooldown/deduping.

This store uses a filesystem lock (via `filelock`) when available to make
concurrent access safer across processes. If `filelock` is not installed,
the store falls back to a no-op lock for compatibility.

Usage:
    store = JsonAlertStore(path=None)  # defaults to refactor/services/alert_store.json
    if store.should_alert(key, cooldown_sec):
        # send alert
        store.record_alert(key)
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from typing import Dict

try:
    from filelock import FileLock
except Exception:
    FileLock = None


class JsonAlertStore:
    def __init__(self, path: str | None = None) -> None:
        base = os.path.dirname(__file__)
        default = os.path.join(base, "alert_store.json")
        self.path = path or default
        self._data: Dict[str, float] = {}
        self._lock_path = self.path + ".lock"
        self._load()

    def _load(self) -> None:
        lock_ctx = FileLock(self._lock_path, timeout=5) if FileLock else contextlib.nullcontext()
        try:
            with lock_ctx:
                if not os.path.exists(self.path):
                    self._data = {}
                    return
                with open(self.path, "r", encoding="utf-8") as f:
                    try:
                        self._data = json.load(f) or {}
                    except Exception:
                        # if corrupted, rename and start fresh
                        try:
                            os.replace(self.path, self.path + ".bak")
                        except Exception:
                            pass
                        self._data = {}
        except Exception:
            # on any failure prefer to continue with empty store
            self._data = {}

    def _save(self) -> None:
        tmp = self.path + ".tmp"
        lock_ctx = FileLock(self._lock_path, timeout=5) if FileLock else contextlib.nullcontext()
        try:
            with lock_ctx:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self._data, f, ensure_ascii=False, indent=2)
                    f.flush()
                    try:
                        os.fsync(f.fileno())
                    except Exception:
                        pass
                os.replace(tmp, self.path)
        except Exception:
            # best-effort; do not raise on save failures
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass

    def should_alert(self, key: str, cooldown_sec: int) -> bool:
        now = time.time()
        ts = self._data.get(key)
        if ts is None:
            return True
        return (now - float(ts)) >= float(cooldown_sec)

    def record_alert(self, key: str) -> None:
        self._data[key] = time.time()
        try:
            self._save()
        except Exception:
            pass

    def cleanup(self, older_than_sec: int) -> None:
        now = time.time()
        changed = False
        for k in list(self._data.keys()):
            try:
                if now - float(self._data.get(k, 0)) > older_than_sec:
                    del self._data[k]
                    changed = True
            except Exception:
                try:
                    del self._data[k]
                except Exception:
                    pass
                changed = True
        if changed:
            try:
                self._save()
            except Exception:
                pass
