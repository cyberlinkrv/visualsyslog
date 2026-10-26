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
        assert old_db.exists(), "The previous database should remain as backup"
        assert client.get("/api/messages").json()["items"][0]["message"] == "retained record"
        moved = client.get("/api/settings").json()
        assert moved["database_path"] == str(destination)
        assert client.put("/api/settings/database", json={"database_path": str(destination)}).status_code == 200
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
    assert app.state.storage_warning is not None
    assert str(missing) in app.state.storage_warning
    app.state.store.close()
