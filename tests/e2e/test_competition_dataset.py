"""真实比赛材料从三个上传接口到 JSON/Word 输出的可选验收。"""

import time

from fastapi.testclient import TestClient

from app.main import app
from tests.e2e.conftest import multipart_folder
from tests.helpers.assertions import assert_all_sources_traceable
from app.models.results import AuditResult


def test_competition_dataset_end_to_end(competition_dataset):
    with TestClient(app) as client:
        files, data = multipart_folder(competition_dataset["tender"])
        project_response = client.post("/api/projects", files=files, data=data)
        project_response.raise_for_status()
        project = project_response.json()
        assert project["status"] == "rule_ready"

        files, data = multipart_folder(competition_dataset["evidence"])
        bidder_response = client.post(
            f"/api/projects/{project['project_id']}/bidders",
            files=files,
            data=data,
        )
        bidder_response.raise_for_status()
        bidder = bidder_response.json()
        assert bidder["status"] == "evidence_ready"

        files, data = multipart_folder(competition_dataset["bids"])
        task_response = client.post(
            f"/api/projects/{project['project_id']}/bidders/"
            f"{bidder['bidder_id']}/audit-tasks",
            files=files,
            data=data,
        )
        task_response.raise_for_status()
        task_id = task_response.json()["task_id"]

        deadline = time.monotonic() + 3600
        while time.monotonic() < deadline:
            task = client.get(f"/api/audit-tasks/{task_id}").json()
            if task["status"] in {
                "completed",
                "completed_with_report_error",
                "failed",
            }:
                break
            time.sleep(1)
        assert task["status"] == "completed", task.get("error")
        result = AuditResult.model_validate(task["result"])
        assert len(result.file_results) == len(
            [item for item in competition_dataset["bids"].rglob("*") if item.is_file()]
        )
        assert_all_sources_traceable(result)
        assert task["outputs"]["json"]
        assert task["outputs"]["word"]
