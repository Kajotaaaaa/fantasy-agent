"""SQLite local: histórico de valores y control de alertas ya enviadas."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path


class Store:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path)
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS alerts_sent (key TEXT PRIMARY KEY, ts REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """
        )

    def alert_is_new(self, key: str, ttl_hours: float = 72) -> bool:
        """True la primera vez que se ve una alerta (y la registra)."""
        now = time.time()
        self.db.execute("DELETE FROM alerts_sent WHERE ts < ?", (now - ttl_hours * 3600,))
        cur = self.db.execute("INSERT OR IGNORE INTO alerts_sent(key, ts) VALUES (?, ?)", (key, now))
        self.db.commit()
        return cur.rowcount == 1

    def get(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set(self, key: str, value: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO kv(key, value) VALUES (?, ?)", (key, value))
        self.db.commit()

    def prefixed(self, prefix: str) -> dict[str, str]:
        """Todas las claves que empiezan por `prefix`, sin el prefijo. Vacíos (borrados con
        `set(key, "")`) se omiten."""
        rows = self.db.execute("SELECT key, value FROM kv WHERE key LIKE ?", (prefix + "%",)).fetchall()
        return {k[len(prefix):]: v for k, v in rows if v}
