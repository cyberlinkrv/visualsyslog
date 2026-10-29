from datetime import datetime, timedelta, timezone

import pytest

from visualsyslog.server import parse_syslog
from visualsyslog.store import LogStore


def test_parse_rfc3164_pri_and_message():
    item = parse_syslog("<34>Oct 11 22:14:15 router sshd[123]: failed login", "192.0.2.10", "UDP")
    assert item["facility"] == 4
    assert item["severity"] == 2
    assert item["hostname"] == "router"
    assert item["app_name"] == "sshd"
    assert item["message"] == "failed login"
    assert item["source_ip"] == "192.0.2.10"


def test_parse_unstructured():
    item = parse_syslog("plain message", "127.0.0.1", "TCP")
    assert item["severity"] is None
    assert item["message"] == "plain message"


def test_parse_rfc5424():
    item = parse_syslog("<165>1 2024-01-01T00:00:00Z host app 42 ID47 - hello", "192.0.2.1", "UDP")
    assert item["facility"] == 20
    assert item["severity"] == 5
    assert item["hostname"] == "host"
    assert item["app_name"] == "app"
    assert item["event_at"].startswith("2024-01-01T00:00:00")
    assert item["message"] == "hello"


def test_insert_search_and_retention_setting(tmp_path):
    store = LogStore(tmp_path / "test.db")
    store.insert(parse_syslog("<13>Oct 11 22:14:15 edge firewall: blocked", "10.0.0.5", "UDP"))
    rows, count = store.list_messages(search="firewall")
    assert count == 1 and rows[0]["hostname"] == "edge"
    assert store.stats()["retention_days"] == 1095
    store.set_retention_days(2000)
    assert store.retention_days() == 2000
    with pytest.raises(ValueError):
        store.set_retention_days(0)
    store.close()


def test_hosts_metric_counts_distinct_source_ips(tmp_path):
    store = LogStore(tmp_path / "hosts.db")
    store.insert(parse_syslog("first event", "10.10.10.1", "UDP"))
    store.insert(parse_syslog("another event", "10.10.10.1", "TCP"))
    store.insert(parse_syslog("third event", "11.11.11.1", "UDP"))
    assert store.stats()["hosts"] == 2
    store.close()


def test_purge_expired(tmp_path):
    store = LogStore(tmp_path / "purge.db", retention_days=1)
    store.insert(parse_syslog("test", "127.0.0.1", "UDP"))
    old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(timespec="milliseconds")
    with store.db:
        store.db.execute("UPDATE messages SET received_at=?", (old,))
    assert store.purge_expired() == 1
    assert store.list_messages()[1] == 0
    store.close()


def test_search_exact_ip_port_and_multiple_terms(tmp_path):
    store = LogStore(tmp_path / "search.db")
    store.insert(parse_syslog("<13>connection from 192.168.20.11:514 refused on port 514", "10.0.0.5", "UDP"))
    store.insert(parse_syslog("<13>connection from 192.168.20.110:1514 refused", "10.0.0.50", "UDP"))

    assert store.list_messages(search="192.168.20.11")[1] == 1
    assert store.list_messages(search="514")[1] == 1
    assert store.list_messages(search="connection refused")[1] == 2
    assert store.list_messages(search='"connection from 192.168.20.11:514"')[1] == 1
    assert store.list_messages(search="connection nonexistent")[1] == 0
    store.close()


def test_search_paginates_sorted_results(tmp_path):
    store = LogStore(tmp_path / "search-pages.db")
    for i in range(55):
        store.insert(parse_syslog(f"<13>connection from host{i} port 514", f"10.0.0.{i + 1}", "UDP"))
    first, total = store.list_messages(limit=50, offset=0, search="connection")
    second, _ = store.list_messages(limit=50, offset=50, search="connection")
    assert total == 55
    assert len(first) == 50 and len(second) == 5
    assert first[0]["id"] > second[0]["id"]
    store.close()



def test_dashboard_stats_reuse_incremental_counters(tmp_path):
    store = LogStore(tmp_path / "fast-stats.db")
    store.insert(parse_syslog("first", "10.10.10.1", "UDP"))
    store.insert(parse_syslog("second", "10.10.10.1", "TCP"))
    store.insert(parse_syslog("third", "11.11.11.1", "UDP"))
    assert store.stats()["total"] == 3
    assert store.stats()["hosts"] == 2
    statements = []
    store.db.set_trace_callback(statements.append)
    for _ in range(5):
        assert store.stats()["total"] == 3
    store.db.set_trace_callback(None)
    assert not any("COUNT(*) FROM messages" in sql or "COUNT(DISTINCT source_ip)" in sql
                   for sql in statements)
    store.close()


def test_expiration_updates_incremental_message_and_host_counters(tmp_path):
    store = LogStore(tmp_path / "fast-purge.db", retention_days=1)
    store.insert(parse_syslog("old host", "10.0.0.1", "UDP"))
    old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(timespec="milliseconds")
    with store.db:
        store.db.execute("UPDATE messages SET received_at=?", (old,))
    assert store.stats()["total"] == 1
    assert store.stats()["hosts"] == 1
    assert store.purge_expired() == 1
    assert store.stats()["total"] == 0
    assert store.stats()["hosts"] == 0
    store.close()


def test_legacy_database_builds_resumable_fts_index(tmp_path):
    import sqlite3
    import time

    path = tmp_path / "legacy.db"
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT, received_at TEXT NOT NULL, event_at TEXT,
            facility INTEGER, severity INTEGER, hostname TEXT, app_name TEXT,
            source_ip TEXT NOT NULL, protocol TEXT NOT NULL, message TEXT NOT NULL, raw TEXT NOT NULL
        );
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    """)
    for i in range(50):
        text = f"legacy firewall connection from 192.0.2.{i} port 514"
        db.execute("INSERT INTO messages(received_at,source_ip,protocol,message,raw) VALUES(?,?,?,?,?)",
                   (datetime.now(timezone.utc).isoformat(timespec="milliseconds"), "10.0.0.1", "UDP", text, text))
    db.commit()
    db.close()

    store = LogStore(path)
    store.insert(parse_syslog("live record port 5514", "10.0.0.2", "UDP"))
    deadline = time.monotonic() + 5
    while not store.search_index_status()["ready"] and time.monotonic() < deadline:
        time.sleep(0.01)
    assert store.search_index_status()["ready"] is True
    assert store.performance_metrics_ready is True
    assert store.stats()["total"] == 51
    assert store.stats()["hosts"] == 2
    rows, total = store.list_messages(search="192.0.2.17 514")
    assert total == 1
    assert "192.0.2.17" in rows[0]["message"]
    assert store.list_messages(search="live 5514")[1] == 1
    store.close()



def test_search_uses_fts5_instead_of_regex_full_scan(tmp_path):
    store = LogStore(tmp_path / "fts-search.db")
    store.insert(parse_syslog("connection from 192.0.2.11 port 514", "10.0.0.5", "UDP"))
    assert store.search_index_status()["ready"] is True
    statements = []
    store.db.set_trace_callback(statements.append)
    rows, total = store.list_messages(search="192.0.2.11 514")
    store.db.set_trace_callback(None)
    assert total == 1 and len(rows) == 1
    assert any("messages_fts MATCH" in sql for sql in statements)
    select_statements = [sql for sql in statements if sql.lstrip().upper().startswith("SELECT")]
    assert len(select_statements) == 1
    assert "COUNT(*) OVER()" in select_statements[0]
    store.close()


def test_expiration_waits_for_fts_backfill(tmp_path):
    store = LogStore(tmp_path / "wait-for-fts.db", retention_days=1)
    store.insert(parse_syslog("old event", "10.0.0.1", "UDP"))
    old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(timespec="milliseconds")
    with store.db:
        store.db.execute("UPDATE messages SET received_at=?", (old,))
    store._set_index_state(ready=False, progress=50)
    assert store.purge_expired() == 0
    assert store.stats()["total"] == 1
    store._set_index_state(ready=True, progress=100)
    assert store.purge_expired() == 1
    store.close()


def test_optional_received_date_filters_calendar_day_and_preserves_pagination(tmp_path):
    store = LogStore(tmp_path / "date-search.db")
    for text, timestamp in [
        ("previous day", "2025-04-30T23:59:59.999+00:00"),
        ("target early", "2025-05-01T00:00:00.000+00:00"),
        ("target late", "2025-05-01T23:59:59.999+00:00"),
        ("next day", "2025-05-02T00:00:00.000+00:00"),
    ]:
        store.insert(parse_syslog(text, "192.0.2.1", "UDP"))
        with store.db:
            store.db.execute("UPDATE messages SET received_at=? WHERE message=?", (timestamp, text))
    all_rows, all_total = store.list_messages(search="target")
    day_rows, day_total = store.list_messages(search="target", received_date="2025-05-01")
    assert all_total == 2 and len(all_rows) == 2
    assert day_total == 2 and {row["message"] for row in day_rows} == {"target early", "target late"}
    first, first_total = store.list_messages(limit=1, search="target", received_date="2025-05-01")
    second, second_total = store.list_messages(limit=1, offset=1, search="target", received_date="2025-05-01")
    assert first_total == second_total == 2
    assert len(first) == len(second) == 1
    assert first[0]["id"] > second[0]["id"]
    store.close()


def test_unfiltered_dashboard_page_uses_incremental_count_instead_of_window(tmp_path):
    store = LogStore(tmp_path / "dashboard-page.db")
    store.insert(parse_syslog("first event", "10.0.0.1", "UDP"))
    store.insert(parse_syslog("second event", "10.0.0.2", "UDP"))
    assert store.performance_metrics_ready is True
    statements = []
    store.db.set_trace_callback(statements.append)
    rows, total = store.list_messages(limit=1)
    store.db.set_trace_callback(None)
    selects = [sql.upper() for sql in statements if sql.lstrip().upper().startswith("SELECT")]
    assert total == 2 and len(rows) == 1
    assert len(selects) == 2
    assert not any("COUNT(*) OVER()" in sql or "COUNT(*) FROM MESSAGES" in sql for sql in selects)
    assert any("PERFORMANCE_COUNTERS" in sql for sql in selects)
    store.close()


def test_v1_search_index_migrates_to_compact_v2_without_dropping_messages(tmp_path):
    import sqlite3
    import time

    path = tmp_path / "fts-v1.db"
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT, received_at TEXT NOT NULL, event_at TEXT,
            facility INTEGER, severity INTEGER, hostname TEXT, app_name TEXT,
            source_ip TEXT NOT NULL, protocol TEXT NOT NULL, message TEXT NOT NULL, raw TEXT NOT NULL
        );
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE performance_counters (key TEXT PRIMARY KEY, value INTEGER NOT NULL);
        CREATE TABLE host_counts (source_ip TEXT PRIMARY KEY, message_count INTEGER NOT NULL);
        CREATE VIRTUAL TABLE messages_fts USING fts5(
            message,raw,hostname,source_ip,app_name,content='messages',content_rowid='id',
            tokenize='unicode61 remove_diacritics 0'
        );
    """)
    raw = "<34>Oct 11 22:14:15 edge-router sshd[123]: compact-index-migration-marker"
    db.execute("""INSERT INTO messages(received_at,facility,severity,hostname,app_name,source_ip,
               protocol,message,raw) VALUES(?,?,?,?,?,?,?,?,?)""",
               (datetime.now(timezone.utc).isoformat(timespec="milliseconds"), 4, 2, "edge-router",
                "sshd", "192.0.2.19", "UDP", "compact-index-migration-marker", raw))
    db.execute("INSERT INTO messages_fts(rowid,message,raw,hostname,source_ip,app_name) "
               "SELECT id,message,raw,hostname,source_ip,app_name FROM messages")
    db.execute("INSERT INTO settings(key,value) VALUES('search_index_v1','1')")
    db.commit()
    db.close()

    store = LogStore(path)
    deadline = time.monotonic() + 5
    while not store.search_index_status()["ready"] and time.monotonic() < deadline:
        time.sleep(0.01)
    assert store.search_index_status()["ready"] is True
    columns = [row["name"] for row in store.db.execute("PRAGMA table_info(messages_fts)")]
    assert columns == ["raw", "source_ip"]
    rows, total = store.list_messages(search="compact-index-migration-marker")
    assert total == 1 and rows[0]["message"] == "compact-index-migration-marker"
    assert rows[0]["raw"] == raw
    assert store.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
    assert store._setting(store.db, "search_index_schema") == "2"
    store.close()


def test_new_raw_payloads_are_compressed_readable_searchable_and_purgeable(tmp_path):
    store = LogStore(tmp_path / "compressed-raw.db", retention_days=1)
    message = "compression-token login-denied " + ("repeated-header-field=value " * 100)
    item = parse_syslog(message, "192.0.2.88", "UDP")
    store.insert(item)
    stored = store.db.execute("SELECT raw,typeof(raw) AS kind FROM messages").fetchone()
    assert stored["kind"] == "blob"
    assert len(stored["raw"]) < len(item["raw"].encode("utf-8"))
    rows, total = store.list_messages(search="compression-token")
    assert total == 1 and rows[0]["raw"] == item["raw"]
    phrase_rows, phrase_total = store.list_messages(search='"login-denied repeated-header-field=value"')
    assert phrase_total == 1 and phrase_rows[0]["message"] == item["message"]
    no_raw_rows, no_raw_total = store.list_messages(search="repeated-header-field=value", include_raw=False)
    assert no_raw_total == 1 and "raw" not in no_raw_rows[0]
    old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(timespec="milliseconds")
    with store.db:
        store.db.execute("UPDATE messages SET received_at=?", (old,))
    assert store.purge_expired() == 1
    assert store.list_messages(search="compression-token")[1] == 0
    store.close()


def test_fts_simple_terms_respect_underscore_word_boundaries(tmp_path):
    store = LogStore(tmp_path / "underscore-boundaries.db")
    store.insert(parse_syslog("user_id failed", "192.0.2.10", "UDP"))
    store.insert(parse_syslog("user-name failed", "192.0.2.11", "UDP"))
    statements = []
    store.db.set_trace_callback(statements.append)
    rows, total = store.list_messages(search="user")
    store.db.set_trace_callback(None)
    assert total == 1 and rows[0]["message"] == "user-name failed"
    search_sql = [sql for sql in statements if "MESSAGES_FTS MATCH" in sql.upper()]
    assert search_sql and all("REGEXP" not in sql.upper() for sql in search_sql)
    store.close()


def test_updates_ahead_of_fts_cursor_wait_for_backfill(tmp_path):
    store = LogStore(tmp_path / "cursor-update.db")
    message_id = store.insert(parse_syslog("old-value", "192.0.2.20", "UDP"))
    with store.db:
        store.db.execute("INSERT INTO messages_fts(messages_fts) VALUES('delete-all')")
        store.db.execute("DELETE FROM settings WHERE key='search_index_v2'")
        store.db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('search_index_max_id',?)",
                         (str(message_id),))
        store.db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('search_index_cursor','0')")
        store.db.execute("UPDATE messages SET message='updated-value',raw='updated-value' WHERE id=?",
                         (message_id,))
    assert store.list_messages(search="updated-value")[1] == 0
    store._build_search_index_once(store.db, message_id, 0)
    rows, total = store.list_messages(search="updated-value")
    assert total == 1 and len(rows) == 1
    store.close()


def test_two_instances_resume_the_same_fts_cursor_without_duplicate_results(tmp_path):
    import time

    path = tmp_path / "two-indexers.db"
    first = LogStore(path)
    with first.db:
        first.db.execute("INSERT INTO messages_fts(messages_fts) VALUES('delete-all')")
        first.db.execute("DELETE FROM settings WHERE key IN ('search_index_v2','search_index_cursor','search_index_max_id')")
        rows = [
            ("2026-10-28T00:00:00.000+00:00", None, None, None, "host", "app", "192.0.2.1",
             "UDP", "sharedcursor-token repeated field", "sharedcursor-token repeated field " * 3)
            for _ in range(8000)
        ]
        first.db.executemany("""INSERT INTO messages
            (received_at,event_at,facility,severity,hostname,app_name,source_ip,protocol,message,raw)
            VALUES(?,?,?,?,?,?,?,?,?,?)""", rows)
        max_id = int(first.db.execute("SELECT MAX(id) FROM messages").fetchone()[0])
        first.db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('search_index_max_id',?)",
                         (str(max_id),))
    first._fts_ready = False
    first._index_max_id = max_id
    first._start_search_index()
    second = LogStore(path)
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if first.search_index_status()["ready"] and second.search_index_status()["ready"]:
                break
            time.sleep(0.01)
        assert first.search_index_status()["ready"] is True
        assert second.search_index_status()["ready"] is True
        assert first.list_messages(search="sharedcursor-token", limit=1)[1] == 8000
    finally:
        first.close()
        second.close()



def test_fast_search_page_is_100_rows_and_skips_total_count(tmp_path):
    store = LogStore(tmp_path / "fast-search-262.db")
    for i in range(225):
        store.insert(parse_syslog(f"needle event {i}", f"192.0.2.{i % 200 + 1}", "UDP"))
    with store.db:
        store.db.execute("UPDATE messages SET received_at='2026-10-29T12:00:00.000+00:00'")
    statements = []
    store.db.set_trace_callback(statements.append)
    first, more = store.search_page(limit=100, offset=0, search="needle", received_date="2026-10-29")
    second, second_more = store.search_page(limit=100, offset=100, search="needle", received_date="2026-10-29")
    store.db.set_trace_callback(None)
    assert len(first) == len(second) == 100
    assert more is True and second_more is True
    assert first[0]["id"] > second[0]["id"]
    assert "raw" not in first[0]
    assert any("messages_fts MATCH" in sql for sql in statements)
    assert any("ORDER BY messages_fts.rowid DESC" in sql for sql in statements)
    assert not any("COUNT(" in sql.upper() for sql in statements)
    plan = store.db.execute(
        "EXPLAIN QUERY PLAN SELECT messages.id FROM messages "
        "JOIN messages_fts ON messages_fts.rowid=messages.id "
        "WHERE messages_fts MATCH ? ORDER BY messages_fts.rowid DESC LIMIT 101",
        ("needle",),
    ).fetchall()
    assert not any("USE TEMP B-TREE" in str(row[3]) for row in plan)
    store.close()


def test_fast_search_page_respects_existing_single_day_prefilter(tmp_path):
    store = LogStore(tmp_path / "fast-search-date-262.db")
    old_id = store.insert(parse_syslog("needle yesterday", "192.0.2.1", "UDP"))
    today_id = store.insert(parse_syslog("needle today", "192.0.2.2", "UDP"))
    with store.db:
        store.db.execute("UPDATE messages SET received_at=? WHERE id=?",
                         ("2026-10-28T23:59:59.000+00:00", old_id))
        store.db.execute("UPDATE messages SET received_at=? WHERE id=?",
                         ("2026-10-29T12:00:00.000+00:00", today_id))
    rows, has_more = store.search_page(search="needle", received_date="2026-10-29")
    assert [row["id"] for row in rows] == [today_id]
    assert has_more is False
    store.close()
