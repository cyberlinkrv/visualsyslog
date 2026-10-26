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
