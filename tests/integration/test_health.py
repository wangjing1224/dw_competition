"""FastAPI 服务启动和健康检查测试。"""

from fastapi.testclient import TestClient

from app.main import app


def test_health(monkeypatch, temp_settings):
    monkeypatch.setattr("app.main.get_settings", lambda: temp_settings)
    monkeypatch.setattr(
        "app.storage.database.database_path",
        lambda: temp_settings.storage.database_path,
    )
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
