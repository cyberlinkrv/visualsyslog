from fastapi.testclient import TestClient

from visualsyslog.server import create_app


def test_dashboard_and_retention_api(tmp_path):
    app = create_app(data_dir=tmp_path, udp_host="127.0.0.1", udp_port=0,
                     tcp_host="127.0.0.1", tcp_port=0)
    with TestClient(app) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert "VisualSyslog" in page.text
        stats = client.get("/api/stats").json()
        assert stats["retention_days"] == 1095
        assert stats["listeners"]["udp"].endswith(":0")
        result = client.put("/api/settings", json={"retention_days": 2500})
        assert result.status_code == 200
        assert result.json()["retention_days"] == 2500
        assert client.put("/api/settings", json={"retention_days": 0}).status_code == 422


def test_database_move_preserves_logs_and_survives_restart(tmp_path):
    data_dir = tmp_path / "app-data"
    old_db = data_dir / "visualsyslog.db"
    app = create_app(data_dir=data_dir, udp_host="127.0.0.1", udp_port=0,
                     tcp_host="127.0.0.1", tcp_port=0)
    with TestClient(app) as client:
        store = app.state.store
        from visualsyslog.server import parse_syslog
        store.insert(parse_syslog("<13>Oct 11 22:14:15 edge daemon: retained record", "10.0.0.7", "UDP"))
        destination = tmp_path / "other-drive" / "visualsyslog.db"
        response = client.put("/api/settings/database", json={"database_path": str(destination)})
        assert response.status_code == 200, response.text
        assert destination.exists()
        assert not old_db.exists(), "The previous database must be removed after migration"
        assert response.json()["previous_database_removed"] is True
        assert client.get("/api/messages").json()["items"][0]["message"] == "retained record"
        moved = client.get("/api/settings").json()
        assert moved["database_path"] == str(destination)
        same_path = client.put("/api/settings/database", json={"database_path": str(destination)})
        assert same_path.status_code == 200
        assert same_path.json()["previous_database_removed"] is False
    restarted = create_app(data_dir=data_dir, udp_host="127.0.0.1", udp_port=0,
                           tcp_host="127.0.0.1", tcp_port=0)
    assert restarted.state.store.path == destination.resolve()
    restarted.state.store.close()


def test_listener_ports_saved_to_settings(tmp_path):
    app = create_app(data_dir=tmp_path, udp_host="127.0.0.1", udp_port=0,
                     tcp_host="127.0.0.1", tcp_port=0)
    with TestClient(app) as client:
        response = client.put("/api/settings", json={"retention_days": 1200, "udp_port": 5515, "tcp_port": 5516})
        assert response.status_code == 200, response.text
        config = client.get("/api/settings").json()
        assert config["udp_port"] == 5515
        assert config["tcp_port"] == 5516
        assert config["retention_days"] == 1200


def test_settings_ports_persist_after_restart(tmp_path):
    app = create_app(data_dir=tmp_path, udp_host="127.0.0.1", udp_port=0,
                     tcp_host="127.0.0.1", tcp_port=0)
    with TestClient(app) as client:
        response = client.put("/api/settings", json={"udp_port": 5561, "tcp_port": 5562})
        assert response.status_code == 200, response.text
    restarted = create_app(data_dir=tmp_path, udp_host="127.0.0.1", udp_port=0,
                           tcp_host="127.0.0.1", tcp_port=0)
    assert restarted.state.udp_port == 5561
    assert restarted.state.tcp_port == 5562
    restarted.state.store.close()


def test_missing_configured_database_is_reported_without_overwrite(tmp_path):
    from visualsyslog.config import save_settings, settings_file_path

    data_dir = tmp_path / "app"
    settings_path = settings_file_path(data_dir)
    missing = tmp_path / "disconnected-drive" / "visualsyslog.db"
    save_settings(settings_path, {"database_path": str(missing)})
    app = create_app(data_dir=data_dir, udp_host="127.0.0.1", udp_port=0,
                     tcp_host="127.0.0.1", tcp_port=0)
    assert not missing.exists()
    assert not (data_dir / "visualsyslog-recovery.db").exists()
    assert app.state.store.in_memory is True
    assert app.state.storage_warning is not None
    assert str(missing) in app.state.storage_warning
    app.state.store.close()


def test_default_bind_ip_is_a_specific_address():
    import ipaddress
    from visualsyslog.network import get_default_bind_ip

    address = ipaddress.ip_address(get_default_bind_ip())
    assert address.version == 4
    assert not address.is_unspecified


def test_bind_ip_can_change_and_unassigned_address_is_rejected(tmp_path):
    app = create_app(data_dir=tmp_path, udp_host="127.0.0.1", udp_port=0,
                     tcp_host="127.0.0.1", tcp_port=0)
    with TestClient(app) as client:
        changed = client.put("/api/settings", json={"bind_ip": "127.0.0.2"})
        assert changed.status_code == 200, changed.text
        assert changed.json()["bind_ip"] == "127.0.0.2"
        assert client.get("/api/stats").json()["listeners"]["udp"].startswith("127.0.0.2:")
        bad = client.put("/api/settings", json={"bind_ip": "203.0.113.77"})
        assert bad.status_code == 400
        assert client.get("/api/settings").json()["bind_ip"] == "127.0.0.2"
        assert client.put("/api/settings", json={"bind_ip": "not-an-ip"}).status_code == 400
    restarted = create_app(data_dir=tmp_path, udp_host=None, udp_port=0, tcp_host=None, tcp_port=0)
    assert restarted.state.bind_ip == "127.0.0.2"
    restarted.state.store.close()


def test_unavailable_saved_bind_ip_falls_back_and_is_reported(tmp_path):
    from visualsyslog.config import save_settings, settings_file_path
    from visualsyslog.network import get_default_bind_ip

    data_dir = tmp_path / "app"
    save_settings(settings_file_path(data_dir), {"bind_ip": "203.0.113.88"})
    app = create_app(data_dir=data_dir, udp_host=None, udp_port=0, tcp_host=None, tcp_port=0)
    assert app.state.bind_ip == get_default_bind_ip()
    assert app.state.network_warning is not None
    assert "203.0.113.88" in app.state.network_warning
    app.state.store.close()


def test_auto_bind_mode_reselects_default_route_ip(tmp_path, monkeypatch):
    import visualsyslog.server as server

    app = create_app(data_dir=tmp_path, udp_host=None, udp_port=0, tcp_host=None, tcp_port=0)
    with TestClient(app) as client:
        current = app.state.bind_ip
        next_ip = "127.0.0.2" if current != "127.0.0.2" else "127.0.0.3"
        monkeypatch.setattr(server, "get_default_bind_ip", lambda: next_ip)
        response = client.put("/api/settings", json={"bind_ip_mode": "auto"})
        assert response.status_code == 200, response.text
        assert response.json()["bind_ip"] == next_ip
        assert response.json()["bind_ip_mode"] == "auto"


def test_language_setting_is_persisted(tmp_path):
    app = create_app(data_dir=tmp_path, udp_host="127.0.0.1", udp_port=0,
                     tcp_host="127.0.0.1", tcp_port=0)
    with TestClient(app) as client:
        response = client.put("/api/settings", json={"language": "en-US"})
        assert response.status_code == 200, response.text
        assert response.json()["language"] == "en-US"
    restarted = create_app(data_dir=tmp_path, udp_host="127.0.0.1", udp_port=0,
                           tcp_host="127.0.0.1", tcp_port=0)
    assert restarted.state.config_file.exists()
    from visualsyslog.config import load_settings
    assert load_settings(restarted.state.config_file)["language"] == "en-US"
    restarted.state.store.close()


def test_search_api_returns_precise_message_matches(tmp_path):
    from visualsyslog.server import parse_syslog

    app = create_app(data_dir=tmp_path, udp_host="127.0.0.1", udp_port=0,
                     tcp_host="127.0.0.1", tcp_port=0)
    with TestClient(app) as client:
        app.state.store.insert(parse_syslog("<13>connection from 10.20.30.40 port 514", "192.0.2.1", "UDP"))
        app.state.store.insert(parse_syslog("<13>connection from 10.20.30.400 port 1514", "192.0.2.2", "UDP"))
        exact = client.get("/api/messages", params={"search": "10.20.30.40 514", "limit": 1})
        assert exact.status_code == 200
        data = exact.json()
        assert data["total"] == 1
        assert len(data["items"]) == 1
        assert "10.20.30.40" in data["items"][0]["message"]
        assert client.get("/api/messages", params={"search": '"connection from 10.20.30.40"'}).json()["total"] == 1
        assert client.get("/api/messages", params={"search": "x" * 201}).status_code == 422


def test_search_results_modal_supports_refinement_and_loading_state(tmp_path):
    app = create_app(data_dir=tmp_path, udp_host="127.0.0.1", udp_port=0,
                     tcp_host="127.0.0.1", tcp_port=0)
    with TestClient(app) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert 'id="searchModalInput"' in page.text
        assert 'id="searchModalButton"' in page.text
        assert 'id="searchLoading"' in page.text
        assert 'aria-live="polite"' in page.text
        assert "animation:search-spin" in page.text
