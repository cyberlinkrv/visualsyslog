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
