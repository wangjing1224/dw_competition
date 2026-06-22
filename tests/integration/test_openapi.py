"""三个业务接口都进入 OpenAPI 的路由注册测试。"""

from fastapi.testclient import TestClient

from app.main import app


def test_business_routes_are_registered():
    with TestClient(app) as client:
        paths = client.get("/openapi.json").json()["paths"]

    assert "/api/projects" in paths
    assert "/api/projects/{project_id}/bidders" in paths
    assert (
        "/api/projects/{project_id}/bidders/{bidder_id}/audit-tasks" in paths
    )
    assert "/api/audit-tasks/{task_id}" in paths
