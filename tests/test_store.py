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
