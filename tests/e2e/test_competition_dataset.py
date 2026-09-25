"""真实比赛材料的规则/独立资料入库验收；审核消费适配另行接入。"""

from fastapi.testclient import TestClient

from app.main import app
from tests.e2e.conftest import multipart_folder


def test_competition_dataset_ingestion(competition_dataset):
    with TestClient(app) as client:
        files, data = multipart_folder(competition_dataset["tender"])
        project_response = client.post("/api/projects", files=files, data=data)
        project_response.raise_for_status()
        project = project_response.json()
        assert project["status"] == "rule_ready"

        files, data = multipart_folder(competition_dataset["evidence"])
        bidder_response = client.post(
            "/api/bidders",
            files=files,
            data=data,
        )
        bidder_response.raise_for_status()
        bidder = bidder_response.json()
        assert bidder["status"] == "evidence_ready"
        detail = client.get(f"/api/bidders/{bidder['bidder_id']}")
        detail.raise_for_status()
        library = detail.json()["library"]
        source_ids = {item["file_id"] for item in library["source_files"]}
        assert source_ids == {item["file_id"] for item in library["materials"]}
        assert source_ids == set(detail.json()["progress"]["completed_file_ids"])
        assert len(source_ids) == len(
            [
                item
                for item in competition_dataset["evidence"].rglob("*")
                if item.is_file()
            ]
        )
        assert all(
            item["bid_summary"] and item["evidence_summary"]
            for item in library["materials"]
        )

        files, data = multipart_folder(competition_dataset["bids"])
        task_response = client.post(
            f"/api/projects/{project['project_id']}/bidders/"
            f"{bidder['bidder_id']}/audit-tasks",
            files=files,
            data=data,
        )
        assert task_response.status_code == 400
        assert task_response.json()["error"]["code"] == "evidence_audit_not_adapted"
