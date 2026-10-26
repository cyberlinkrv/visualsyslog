from __future__ import annotations

import os
import re
import sqlite3
import threading
import time
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

from .config import save_settings


@lru_cache(maxsize=2048)
def _compile_search_pattern(pattern: str):
    return re.compile(pattern, re.IGNORECASE)


def _regexp(pattern: str, value: str | None) -> int:
    return int(value is not None and _compile_search_pattern(pattern).search(str(value)) is not None)


def _search_terms(search: str) -> list[tuple[str, bool]]:
    return [(quoted or plain, bool(quoted)) for quoted, plain in re.findall(r'"([^"]+)"|(\S+)', search.strip())]


def _fts_query(search: str) -> str | None:
    terms = _search_terms(search)
    if not terms or any(not any(char.isalnum() for char in term) for term, _ in terms):
        return None
    return " AND ".join('"' + term.replace('"', '""') + '"' for term, _ in terms)


def default_data_dir() -> Path:
    override = os.environ.get("VISUALSYSLOG_DATA_DIR")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "VisualSyslog"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "visualsyslog"


class LogStore:
    """SQLite store with incremental counters and resumable FTS5 search indexing."""

    def __init__(self, path: Path | str | None = None, retention_days: int = 1095,
                 settings_file: Path | str | None = None):
        requested = str(path) if path is not None else str(default_data_dir() / "visualsyslog.db")
        self.in_memory = requested == ":memory:"
        self.path = Path(":memory:") if self.in_memory else Path(requested).expanduser().resolve()
        self.settings_file = Path(settings_file) if settings_file else None
        if not self.in_memory:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self._index_state_lock = threading.Lock()
        self._index_stop = threading.Event()
        self._index_thread: threading.Thread | None = None
        self._metrics_ready = False
        self._metrics_progress = 0
        self._counter_max_id = 0
        self._fts_available = False
        self._fts_ready = False
        self._fts_progress = 0
        self._fts_error: str | None = None
        self.db = (sqlite3.connect(":memory:", check_same_thread=False, timeout=10)
                   if self.in_memory else self._connect(self.path))
        self.db.row_factory = sqlite3.Row
        self.db.create_function("REGEXP", 2, _regexp, deterministic=True)
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
            CREATE TABLE IF NOT EXISTS performance_counters (key TEXT PRIMARY KEY, value INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS host_counts (source_ip TEXT PRIMARY KEY, message_count INTEGER NOT NULL);
        """)
        self.db.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('retention_days',?)",
                        (str(max(1, retention_days)),))
        self._prepare_counters()
        self._install_counter_triggers()
        self.db.commit()
        self._stats_day = datetime.now(timezone.utc).date().isoformat()
        self._stats_today = int(self.db.execute(
            "SELECT COUNT(*) FROM messages WHERE received_at >= ?", (self._stats_day,)
        ).fetchone()[0])
        self._initialize_search_index()
        if not self.in_memory and (not self._metrics_ready or (self._fts_available and not self._fts_ready)):
            self._start_search_index()

    @staticmethod
    def _connect(path: Path) -> sqlite3.Connection:
        db = sqlite3.connect(path, check_same_thread=False, timeout=10)
        db.row_factory = sqlite3.Row
        db.create_function("REGEXP", 2, _regexp, deterministic=True)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        db.execute("PRAGMA busy_timeout=10000")
        db.execute("PRAGMA cache_size=-32768")
        db.execute("PRAGMA temp_store=MEMORY")
        db.execute("PRAGMA mmap_size=268435456")
        return db

    def _prepare_counters(self) -> None:
        marker = self.db.execute("SELECT value FROM settings WHERE key='performance_counters_v1'").fetchone()
        if marker and marker[0] == "1":
            self._metrics_ready = True
            self._metrics_progress = 100
            return
        saved_max = self._setting(self.db, "performance_counter_max_id")
        saved_cursor = self._setting(self.db, "performance_counter_cursor")
        if saved_max and saved_cursor:
            self._counter_max_id = int(saved_max)
            self._metrics_progress = min(99, int(int(saved_cursor) * 100 / max(1, self._counter_max_id)))
            return
        self._counter_max_id = int(self.db.execute("SELECT COALESCE(MAX(id),0) FROM messages").fetchone()[0])
        # A saved cursor allows the old database to be upgraded in the background and resumed after restart.
        with self.db:
            self.db.execute("DELETE FROM host_counts")
            self.db.execute("INSERT OR REPLACE INTO performance_counters(key,value) VALUES('messages',0)")
            self.db.execute("INSERT OR REPLACE INTO performance_counters(key,value) VALUES('hosts',0)")
            self.db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('performance_counter_max_id',?)",
                            (str(self._counter_max_id),))
            self.db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('performance_counter_cursor','0')")
            if self._counter_max_id == 0:
                self.db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('performance_counters_v1','1')")
                self.db.execute("DELETE FROM settings WHERE key IN ('performance_counter_max_id','performance_counter_cursor')")
                self._metrics_ready = True
                self._metrics_progress = 100

    def _install_counter_triggers(self) -> None:
        self.db.executescript("""
            CREATE TRIGGER IF NOT EXISTS messages_perf_insert AFTER INSERT ON messages BEGIN
                INSERT OR IGNORE INTO host_counts(source_ip,message_count) VALUES(new.source_ip,0);
                UPDATE performance_counters SET value=value+changes() WHERE key='hosts';
                UPDATE host_counts SET message_count=message_count+1 WHERE source_ip=new.source_ip;
                UPDATE performance_counters SET value=value+1 WHERE key='messages';
            END;
            CREATE TRIGGER IF NOT EXISTS messages_perf_delete AFTER DELETE ON messages BEGIN
                UPDATE host_counts SET message_count=message_count-1 WHERE source_ip=old.source_ip;
                DELETE FROM host_counts WHERE source_ip=old.source_ip AND message_count<=0;
                UPDATE performance_counters SET value=value-changes() WHERE key='hosts';
                UPDATE performance_counters SET value=value-1 WHERE key='messages';
            END;
        """)

    def _setting(self, db: sqlite3.Connection, key: str, default: str = "") -> str:
        row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return str(row[0]) if row else default

    def _initialize_search_index(self) -> None:
        try:
            self.db.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
                message,raw,hostname,source_ip,app_name,content='messages',content_rowid='id',
                tokenize='unicode61 remove_diacritics 0'
            )""")
            self.db.executescript("""
                CREATE TRIGGER IF NOT EXISTS messages_fts_insert AFTER INSERT ON messages BEGIN
                    INSERT INTO messages_fts(rowid,message,raw,hostname,source_ip,app_name)
                    VALUES(new.id,new.message,new.raw,new.hostname,new.source_ip,new.app_name);
                END;
                CREATE TRIGGER IF NOT EXISTS messages_fts_delete AFTER DELETE ON messages BEGIN
                    INSERT INTO messages_fts(messages_fts,rowid,message,raw,hostname,source_ip,app_name)
                    VALUES('delete',old.id,old.message,old.raw,old.hostname,old.source_ip,old.app_name);
                END;
                CREATE TRIGGER IF NOT EXISTS messages_fts_update AFTER UPDATE ON messages BEGIN
                    INSERT INTO messages_fts(messages_fts,rowid,message,raw,hostname,source_ip,app_name)
                    VALUES('delete',old.id,old.message,old.raw,old.hostname,old.source_ip,old.app_name);
                    INSERT INTO messages_fts(rowid,message,raw,hostname,source_ip,app_name)
                    VALUES(new.id,new.message,new.raw,new.hostname,new.source_ip,new.app_name);
                END;
            """)
            self.db.commit()
            self._fts_available = True
        except sqlite3.OperationalError as exc:
            self._set_index_state(error=f"FTS5 indisponível: {exc}")
            return

        if self._setting(self.db, "search_index_v1") == "1":
            self._set_index_state(ready=True, progress=100)
            return
        try:
            max_id = int(self.db.execute("SELECT COALESCE(MAX(id),0) FROM messages").fetchone()[0])
            if max_id == 0:
                with self.db:
                    self.db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('search_index_v1','1')")
                    self.db.execute("DELETE FROM settings WHERE key='search_index_cursor'")
                self._set_index_state(ready=True, progress=100)
                return
            saved_max = self._setting(self.db, "search_index_max_id")
            self._index_max_id = int(saved_max) if saved_max else max_id
            if not saved_max:
                with self.db:
                    self.db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('search_index_max_id',?)",
                                    (str(self._index_max_id),))
            cursor = int(self._setting(self.db, "search_index_cursor", "0") or 0)
            self._set_index_state(ready=False, progress=min(99, int(cursor * 100 / max_id)))
            if self.in_memory:
                self._build_search_index_once(self.db, max_id, cursor)
        except sqlite3.Error as exc:
            self._set_index_state(error=str(exc))

    def _set_index_state(self, *, ready: bool | None = None, progress: int | None = None,
                         error: str | None = None) -> None:
        with self._index_state_lock:
            if ready is not None:
                self._fts_ready = ready
            if progress is not None:
                self._fts_progress = max(0, min(100, progress))
            self._fts_error = error

    def _build_search_index_once(self, db: sqlite3.Connection, max_id: int, cursor: int) -> None:
        rows = db.execute("""SELECT id,message,raw,hostname,source_ip,app_name FROM messages
                             WHERE id>? AND id<=? ORDER BY id""", (cursor, max_id)).fetchall()
        with db:
            db.executemany("""INSERT INTO messages_fts(rowid,message,raw,hostname,source_ip,app_name)
                              VALUES(?,?,?,?,?,?)""",
                           [(r["id"],r["message"],r["raw"],r["hostname"],r["source_ip"],r["app_name"])
                            for r in rows])
            db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('search_index_v1','1')")
            db.execute("DELETE FROM settings WHERE key='search_index_cursor'")
        self._set_index_state(ready=True, progress=100)

    def _start_search_index(self) -> None:
        if self._index_thread and self._index_thread.is_alive():
            return
        if self._metrics_ready and (not self._fts_available or self._fts_ready):
            return
        self._index_stop.clear()
        self._index_thread = threading.Thread(target=self._build_search_index,
                                              name="visualsyslog-search-index", daemon=True)
        self._index_thread.start()

    def _build_search_index(self) -> None:
        db = None
        try:
            db = sqlite3.connect(self.path, timeout=10)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA busy_timeout=10000")
            db.execute("PRAGMA cache_size=-8192")
            db.execute("PRAGMA temp_store=MEMORY")
            if not self._metrics_ready:
                self._build_counters(db)
            if not self._fts_available or self._fts_ready:
                return
            max_id = self._index_max_id
            cursor = int(self._setting(db, "search_index_cursor", "0") or 0)
            while not self._index_stop.is_set():
                rows = db.execute("""SELECT id,message,raw,hostname,source_ip,app_name FROM messages
                                     WHERE id>? AND id<=? ORDER BY id LIMIT 500""",
                                  (cursor,max_id)).fetchall()
                if not rows:
                    with db:
                        db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('search_index_v1','1')")
                        db.execute("DELETE FROM settings WHERE key IN ('search_index_cursor','search_index_max_id')")
                    self._set_index_state(ready=True, progress=100)
                    return
                next_cursor = int(rows[-1]["id"])
                with db:
                    db.executemany("""INSERT INTO messages_fts(rowid,message,raw,hostname,source_ip,app_name)
                                      VALUES(?,?,?,?,?,?)""",
                                   [(r["id"],r["message"],r["raw"],r["hostname"],r["source_ip"],r["app_name"])
                                    for r in rows])
                    db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('search_index_cursor',?)",
                               (str(next_cursor),))
                cursor = next_cursor
                self._set_index_state(progress=min(99, int(cursor * 100 / max_id)))
                time.sleep(0.01)
        except (sqlite3.Error, OSError) as exc:
            self._set_index_state(error=str(exc))
        finally:
            if db is not None:
                db.close()

    def _build_counters(self, db: sqlite3.Connection) -> None:
        cursor = int(self._setting(db, "performance_counter_cursor", "0") or 0)
        max_id = self._counter_max_id
        batch_size = 5000
        while cursor < max_id and not self._index_stop.is_set():
            rows = db.execute("""SELECT id,source_ip FROM messages WHERE id>? AND id<=?
                                 ORDER BY id LIMIT ?""", (cursor,max_id,batch_size)).fetchall()
            if not rows:
                cursor = max_id
            else:
                next_cursor = int(rows[-1]["id"])
                hosts = Counter(row["source_ip"] for row in rows)
                with db:
                    for source_ip, count in hosts.items():
                        added = db.execute("INSERT OR IGNORE INTO host_counts(source_ip,message_count) VALUES(?,0)",
                                           (source_ip,))
                        if added.rowcount:
                            db.execute("UPDATE performance_counters SET value=value+1 WHERE key='hosts'")
                        db.execute("UPDATE host_counts SET message_count=message_count+? WHERE source_ip=?",
                                   (count,source_ip))
                        db.execute("UPDATE performance_counters SET value=value+? WHERE key='messages'", (count,))
                    cursor = next_cursor
                    db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('performance_counter_cursor',?)",
                               (str(cursor),))
            self._metrics_progress = min(99, int(cursor * 100 / max(1,max_id)))
            time.sleep(0.005)
        if self._index_stop.is_set():
            return
        with db:
            db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('performance_counters_v1','1')")
            db.execute("DELETE FROM settings WHERE key IN ('performance_counter_max_id','performance_counter_cursor')")
        self._metrics_ready = True
        self._metrics_progress = 100

    def search_index_status(self) -> dict:
        with self._index_state_lock:
            return {"available":self._fts_available,"ready":self._fts_ready,
                    "progress":self._fts_progress,"error":self._fts_error,
                    "metrics_ready":self._metrics_ready,"metrics_progress":self._metrics_progress}

    @property
    def performance_metrics_ready(self) -> bool:
        return self._metrics_ready

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
            was_indexing = bool(self._index_thread and self._index_thread.is_alive())
            if was_indexing:
                self._index_stop.set()
                self._index_thread.join(timeout=10)
                if self._index_thread.is_alive():
                    self._index_stop.clear()
                    raise RuntimeError("A preparação do índice de busca ainda está ocupada; tente novamente em alguns segundos.")
            target.parent.mkdir(parents=True, exist_ok=True)
            temp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
            replacement = None
            try:
                backup = sqlite3.connect(temp, timeout=10)
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
                for candidate in (temp,target,Path(str(target)+"-wal"),Path(str(target)+"-shm")):
                    try:
                        candidate.unlink(missing_ok=True)
                    except OSError:
                        pass
                if was_indexing:
                    self._index_stop.clear()
                    self._start_search_index()
                raise
            old_db = self.db
            self.db = replacement
            self.path = target
            self.in_memory = False
            old_db.close()
            source_removed = True
            if not source_was_memory:
                for old_file in (source,Path(str(source)+"-wal"),Path(str(source)+"-shm")):
                    try:
                        old_file.unlink(missing_ok=True)
                    except OSError:
                        source_removed = False
            if was_indexing:
                self._index_stop.clear()
                self._start_search_index()
            return source,target,source_removed

    def insert(self, item: dict) -> int:
        received = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        with self.lock, self.db:
            cur = self.db.execute("""INSERT INTO messages
              (received_at,event_at,facility,severity,hostname,app_name,source_ip,protocol,message,raw)
              VALUES(?,?,?,?,?,?,?,?,?,?)""", (received,item.get("event_at"),item.get("facility"),item.get("severity"),
              item.get("hostname"),item.get("app_name"),item["source_ip"],item["protocol"],item["message"],item["raw"]))
            if received[:10] == self._stats_day:
                self._stats_today += 1
            return int(cur.lastrowid)

    @staticmethod
    def _patterns(search: str) -> list[str]:
        patterns = []
        for term, exact_phrase in _search_terms(search):
            if not term:
                continue
            pattern = re.escape(term)
            if exact_phrase:
                if term[0].isalnum() or term[0] == "_":
                    pattern = r"(?<!\w)" + pattern
                if term[-1].isalnum() or term[-1] == "_":
                    pattern += r"(?!\w)"
            else:
                pattern = rf"(?<!\w){pattern}(?!\w)"
            patterns.append(pattern)
        return patterns

    def _list_query(self, limit: int, offset: int, patterns: list[str], severity: int | None,
                    expression: str | None, use_fts: bool):
        clauses = []
        params = []
        from_sql = "messages"
        if use_fts and expression:
            from_sql = "messages JOIN messages_fts ON messages_fts.rowid=messages.id"
            clauses.append("messages_fts MATCH ?")
            params.append(expression)
        for pattern in patterns:
            clauses.append("(messages.message REGEXP ? OR messages.raw REGEXP ? OR messages.hostname REGEXP ? OR messages.source_ip REGEXP ? OR messages.app_name REGEXP ?)")
            params.extend([pattern] * 5)
        if severity is not None:
            clauses.append("severity=?")
            params.append(int(severity))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self.db.execute(f"SELECT messages.* FROM {from_sql}{where} ORDER BY messages.id DESC LIMIT ? OFFSET ?",
                               (*params,limit,offset)).fetchall()
        total = self.db.execute(f"SELECT COUNT(*) FROM {from_sql}{where}",params).fetchone()[0]
        return rows,total

    def list_messages(self, limit=200, offset=0, search="", severity=None):
        limit = max(1,min(int(limit),1000))
        offset = max(0,int(offset))
        patterns = self._patterns(search)
        expression = _fts_query(search) if search else None
        with self.lock:
            use_fts = self._fts_available and self._fts_ready and expression is not None
            try:
                rows,total = self._list_query(limit,offset,patterns,severity,expression,use_fts)
            except sqlite3.OperationalError:
                if not use_fts:
                    raise
                rows,total = self._list_query(limit,offset,patterns,severity,None,False)
        return [dict(r) for r in rows],int(total)

    def stats(self):
        with self.lock:
            counters = dict(self.db.execute("SELECT key,value FROM performance_counters").fetchall())
            day = datetime.now(timezone.utc).date().isoformat()
            if day != self._stats_day:
                self._stats_today = int(self.db.execute(
                    "SELECT COUNT(*) FROM messages WHERE received_at >= ?",(day,)
                ).fetchone()[0])
                self._stats_day = day
            index = self.search_index_status()
            retention = self.retention_days()
        return {"total":int(counters.get("messages",0)) if self._metrics_ready else None,
                "today":int(self._stats_today),
                "hosts":int(counters.get("hosts",0)) if self._metrics_ready else None,
                "performance_ready":self._metrics_ready,"retention_days":retention,
                "database":str(self.path),"search_index":index}

    def purge_expired(self) -> int:
        index_status = self.search_index_status()
        if not self._metrics_ready or (index_status["available"] and not index_status["ready"]):
            return 0
        cutoff = (datetime.now(timezone.utc)-timedelta(days=self.retention_days())).isoformat(timespec="milliseconds")
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                expired_hosts = self.db.execute("""SELECT source_ip,COUNT(*) AS n FROM messages
                                               WHERE received_at < ? GROUP BY source_ip""",(cutoff,)).fetchall()
                if not expired_hosts:
                    self.db.rollback()
                    return 0
                self.db.execute("DROP TRIGGER IF EXISTS messages_perf_delete")
                cur = self.db.execute("DELETE FROM messages WHERE received_at < ?",(cutoff,))
                removed = int(cur.rowcount)
                for row in expired_hosts:
                    self.db.execute("UPDATE host_counts SET message_count=message_count-? WHERE source_ip=?",
                                    (int(row["n"]),row["source_ip"]))
                    deleted_host = self.db.execute("DELETE FROM host_counts WHERE source_ip=? AND message_count<=0",
                                                   (row["source_ip"],))
                    if deleted_host.rowcount:
                        self.db.execute("UPDATE performance_counters SET value=value-1 WHERE key='hosts'")
                self.db.execute("UPDATE performance_counters SET value=MAX(0,value-?) WHERE key='messages'",(removed,))
                self.db.execute("""CREATE TRIGGER IF NOT EXISTS messages_perf_delete AFTER DELETE ON messages BEGIN
                    UPDATE host_counts SET message_count=message_count-1 WHERE source_ip=old.source_ip;
                    DELETE FROM host_counts WHERE source_ip=old.source_ip AND message_count<=0;
                    UPDATE performance_counters SET value=value-changes() WHERE key='hosts';
                    UPDATE performance_counters SET value=value-1 WHERE key='messages';
                END""")
                self.db.commit()
                return removed
            except Exception:
                self.db.rollback()
                raise

    def close(self):
        self._index_stop.set()
        if self._index_thread and self._index_thread.is_alive():
            self._index_thread.join(timeout=5)
        with self.lock:
            self.db.close()
