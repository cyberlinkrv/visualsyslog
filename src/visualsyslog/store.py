from __future__ import annotations

import os
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import load_settings, save_settings


def default_data_dir() -> Path:
    override = os.environ.get("VISUALSYSLOG_DATA_DIR")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "VisualSyslog"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "visualsyslog"


class LogStore:
    def __init__(self, path: Path | str | None = None, retention_days: int = 1095,
                 settings_file: Path | str | None = None):
        requested = str(path) if path is not None else str(default_data_dir() / "visualsyslog.db")
        self.in_memory = requested == ":memory:"
        self.path = Path(":memory:") if self.in_memory else Path(requested).expanduser().resolve()
        self.settings_file = Path(settings_file) if settings_file else None
        if not self.in_memory:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(":memory:", check_same_thread=False, timeout=5) if self.in_memory else self._connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                received_at TEXT NOT NULL,
                event_at TEXT,
                facility INTEGER,
                severity INTEGER,
                hostname TEXT,
                app_name TEXT,
                source_ip TEXT NOT NULL,
                protocol TEXT NOT NULL,
                message TEXT NOT NULL,
                raw TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_messages_received ON messages(received_at DESC);
            CREATE INDEX IF NOT EXISTS idx_messages_host ON messages(hostname);
            CREATE INDEX IF NOT EXISTS idx_messages_severity ON messages(severity);
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        self.db.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('retention_days',?)", (str(max(1, retention_days)),))
        self.db.commit()

    @staticmethod
    def _connect(path: Path) -> sqlite3.Connection:
        db = sqlite3.connect(path, check_same_thread=False, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        db.execute("PRAGMA busy_timeout=5000")
        return db

    def retention_days(self) -> int:
        with self.lock:
            row = self.db.execute("SELECT value FROM settings WHERE key='retention_days'").fetchone()
            return max(1, int(row[0]))

    def set_retention_days(self, days: int) -> None:
        if not 1 <= days <= 36500:
            raise ValueError("O prazo deve ficar entre 1 e 36.500 dias.")
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('retention_days',?)", (str(days),))

    def move_to(self, destination: Path | str) -> tuple[Path, Path, bool]:
        target = Path(destination).expanduser()
        if not target.is_absolute():
            raise ValueError("Informe o caminho absoluto do arquivo de banco de dados.")
        target = target.resolve()
        with self.lock:
            source_was_memory = self.in_memory
            source = self.path if source_was_memory else self.path.resolve()
            if not source_was_memory and target == source:
                return source, target, False
            if target.exists():
                raise FileExistsError(f"O arquivo de destino já existe: {target}")
            target.parent.mkdir(parents=True, exist_ok=True)
            temp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
            replacement = None
            try:
                backup = sqlite3.connect(temp, timeout=5)
                try:
                    self.db.backup(backup)
                    backup.commit()
                finally:
                    backup.close()
                os.replace(temp, target)
                replacement = self._connect(target)
                if self.settings_file:
                    save_settings(self.settings_file, {"database_path": str(target)})
            except Exception:
                if replacement is not None:
                    replacement.close()
                for candidate in (temp, target, Path(str(target) + "-wal"), Path(str(target) + "-shm")):
                    try:
                        candidate.unlink(missing_ok=True)
                    except OSError:
                        pass
                raise
            old_db = self.db
            self.db = replacement
            self.path = target
            self.in_memory = False
            old_db.close()
            source_removed = True
            if not source_was_memory:
                for old_file in (source, Path(str(source) + "-wal"), Path(str(source) + "-shm")):
                    try:
                        old_file.unlink(missing_ok=True)
                    except OSError:
                        source_removed = False
            return source, target, source_removed

    def insert(self, item: dict) -> int:
        received = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        with self.lock, self.db:
            cur = self.db.execute("""INSERT INTO messages
              (received_at,event_at,facility,severity,hostname,app_name,source_ip,protocol,message,raw)
              VALUES(?,?,?,?,?,?,?,?,?,?)""", (received, item.get("event_at"), item.get("facility"), item.get("severity"),
              item.get("hostname"), item.get("app_name"), item["source_ip"], item["protocol"], item["message"], item["raw"]))
            return int(cur.lastrowid)

    def list_messages(self, limit=200, offset=0, search="", severity=None):
        limit = max(1, min(int(limit), 1000)); offset = max(0, int(offset))
        clauses=[]; params=[]
        if search:
            clauses.append("(message LIKE ? OR hostname LIKE ? OR source_ip LIKE ? OR app_name LIKE ?)")
            params.extend([f"%{search}%"]*4)
        if severity is not None:
            clauses.append("severity=?"); params.append(int(severity))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.lock:
            rows = self.db.execute(f"SELECT * FROM messages{where} ORDER BY id DESC LIMIT ? OFFSET ?", (*params, limit, offset)).fetchall()
            total = self.db.execute(f"SELECT COUNT(*) FROM messages{where}", params).fetchone()[0]
        return [dict(r) for r in rows], total

    def stats(self):
        with self.lock:
            total = self.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            today = datetime.now(timezone.utc).date().isoformat()
            recent = self.db.execute("SELECT COUNT(*) FROM messages WHERE received_at >= ?", (today,)).fetchone()[0]
            hosts = self.db.execute("SELECT COUNT(DISTINCT hostname) FROM messages").fetchone()[0]
        return {"total": total, "today": recent, "hosts": hosts, "retention_days": self.retention_days(), "database": str(self.path)}

    def purge_expired(self) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=self.retention_days())).isoformat(timespec="milliseconds")
        with self.lock, self.db:
            cur = self.db.execute("DELETE FROM messages WHERE received_at < ?", (cutoff,))
            return cur.rowcount

    def close(self):
        with self.lock:
            self.db.close()
