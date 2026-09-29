"""真实 SQLite/上传文件/快照；不依赖云端、Embedding 或 LangSmith。"""

from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from app.api.projects import router
from app.core.errors import register_exception_handlers
from app.models.documents import (
    ParsedBlock,
    ParsedDocument,
    ParsedSection,
    document_ref_from_record,
)
from app.models.rules import RuleItem, RuleSet, RuleSourceFile
from app.parsers.assets import AssetStore, file_asset_root
from app.parsers.service import persist_parse_result
from app.rules.local_trace import LocalTrace
from app.services.project_service import ProjectService
from app.services.rule_library_service import RuleLibraryService
from app.storage.database import initialize_database
from app.storage.project_management import mark_interrupted_projects, update_management
from app.storage.repositories import FileRepository, ProjectRepository


@pytest.fixture
def env(monkeypatch, temp_settings):
    for module in (
        "app.storage.file_store",
        "app.parsers.service",
        "app.parsers.common",
        "app.retrieval.store",
        "app.services.rule_library_service",
        "app.services.container",
    ):
        monkeypatch.setattr(module + ".get_settings", lambda: temp_settings)
    monkeypatch.setattr(
        "app.storage.database.database_path",
        lambda: temp_settings.storage.database_path,
    )
    initialize_database(temp_settings.storage.database_path)
    queued = []
    monkeypatch.setattr(
        "app.api.projects.background_runner.submit",
        lambda function, *args: queued.append((function, args)),
    )
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router)
    return SimpleNamespace(
        client=TestClient(app), settings=temp_settings, queued=queued
    )


def upload(
    env,
    *,
    request_id=None,
    content=b"Technical requirements",
    name="sample.md",
    path="folder/sample.md",
    display_name="测试规则库",
):
    request_id = request_id or str(uuid4())
    response = env.client.post(
        "/api/projects",
        data={
            "client_request_id": request_id,
            "display_name": display_name,
            "relative_paths": path,
        },
        files={"files": (name, content)},
    )
    return response, request_id


def ready(env, empty=False, *, display_name="测试规则库"):
    response, _ = upload(env, display_name=display_name)
    assert response.status_code == 202, response.text
    pid = response.json()["project_id"]
    file = FileRepository().list_by_scope(project_id=pid, data_role="rule_source")[0]
    source = RuleSourceFile(
        file_id=file.file_id, file_name=file.file_name, source_path=file.source_path
    )
    rules = RuleSet(
        project_id=pid,
        project_profile="测试项目",
        source_files=[source],
        rules=[]
        if empty
        else [
            RuleItem(
                rule_id="rule-10",
                provenance="ai_generated",
                category="technical",
                requirement="精度不低于要求",
                priority="important",
                verification="bid_only",
                reason="依据技术表",
                sources=[source],
            )
        ],
    )
    path = ProjectService(env.settings)._write_rule_set(rules)
    ProjectRepository().update_status(
        pid,
        "rule_ready",
        rule_set_path=str(path.relative_to(env.settings.storage.data_root)),
    )
    update_management(pid, phase="completed", trace_status="completed")
    return pid, file, env.client.get(f"/api/projects/{pid}").json()


def draft(file, **kwargs):
    return dict(
        category="technical",
        requirement="精度不低于要求",
        priority="important",
        verification="bid_only",
        reason="依据技术表",
        source_file_ids=[file.file_id],
        **kwargs,
    )


def test_upload_is_async_idempotent_and_recoverable(env):
    first, request_id = upload(env)
    assert first.status_code == 202
    pid = first.json()["project_id"]
    assert first.headers["location"] == f"/api/projects/{pid}"
    again, _ = upload(env, request_id=request_id)
    assert again.status_code == 200 and again.json() == first.json()
    assert len(env.queued) == 1
    assert (
        len(FileRepository().list_by_scope(project_id=pid, data_role="rule_source"))
        == 1
    )
    different, _ = upload(env, request_id=request_id, content=b"changed")
    assert different.status_code == 409
    assert different.json()["error"]["code"] == "upload_request_conflict"
    found = env.client.get(
        "/api/projects", params={"client_request_id": request_id}
    ).json()
    assert found["total"] == 1 and found["items"][0]["project_id"] == pid


@pytest.mark.parametrize("status", ["processing_rules", "rule_ready", "rule_failed"])
def test_duplicate_rule_library_name_creates_nothing(env, status):
    response, request_id = upload(env, display_name="Test01")
    pid = response.json()["project_id"]
    ProjectRepository().update_status(pid, status)
    duplicate, _ = upload(env, display_name="  test01  ")
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "duplicate_library_name"
    assert env.client.get("/api/projects").json()["total"] == 1
    assert len(env.queued) == 1
    repeated, _ = upload(env, request_id=request_id, display_name="Test01")
    assert repeated.status_code == 200
    assert repeated.json()["project_id"] == pid


def test_concurrent_same_name_rule_upload_only_creates_one(env):
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: upload(env)[0], range(2)))
    assert sorted(r.status_code for r in results) == [202, 409]
    assert env.client.get("/api/projects").json()["total"] == 1
    assert len(env.queued) == 1


def test_default_name_does_not_collide_with_user_named_id(env):
    assert upload(env, display_name="project-2")[0].status_code == 202
    response, request_id = upload(env, display_name=None)
    assert response.status_code == 202
    assert response.json()["project_id"] == "project-2"
    assert response.json()["display_name"] == "project-2（2）"
    repeated, _ = upload(env, request_id=request_id, display_name=None)
    assert repeated.status_code == 200 and repeated.json() == response.json()


@pytest.mark.parametrize(
    "name,content,path,code",
    [
        ("empty.md", b"", "empty.md", 422),
        ("a.zip", b"zip", "a.zip", 400),
        ("a.md", b"x", "../a.md", 400),
        ("a.md", b"x", ".", 400),
        ("a.md", b"x", "bad\x00/a.md", 400),
    ],
)
def test_invalid_upload_does_not_create_project(env, name, content, path, code):
    result, _ = upload(env, name=name, content=content, path=path)
    assert result.status_code == code
    assert env.client.get("/api/projects").json()["total"] == 0


def test_receiving_failure_before_project_creation_is_not_requeued(env, monkeypatch):
    from app.core.errors import BusinessError

    def fail_create(_):
        raise BusinessError("test_storage_error", "存储暂不可用", http_status=500)

    monkeypatch.setattr(ProjectRepository, "create", fail_create)
    first, request_id = upload(env)
    again, _ = upload(env, request_id=request_id)
    assert first.status_code == again.status_code == 500
    assert first.json() == again.json()
    assert env.client.get("/api/projects").json()["total"] == 0
    assert not env.queued


def test_edit_revision_provenance_noop_and_policy(env):
    pid, file, detail = ready(env)
    # 索引缺失不禁止人工维护、导出。
    assert detail["warning"]["code"] == "tender_index_unavailable"
    headers = {"If-Match": f'"{detail["revision"]}"'}
    body = {"action": "update_rule", "data": draft(file, rule_id="rule-10")}
    unchanged = env.client.post(f"/api/projects/{pid}/edit", headers=headers, json=body)
    assert unchanged.status_code == 200, unchanged.text
    assert unchanged.json()["revision"] == detail["revision"]
    assert unchanged.json()["rule"]["provenance"] == "ai_generated"
    body["data"].update(priority="normal", verification="evidence_check")
    rejected = env.client.post(f"/api/projects/{pid}/edit", headers=headers, json=body)
    assert (
        rejected.status_code == 422
        and rejected.json()["error"]["code"] == "invalid_rule_policy_combination"
    )
    assert (
        env.client.get(f"/api/projects/{pid}").json()["revision"] == detail["revision"]
    )
    body["data"].update(priority="critical", requirement="修改后的精度要求")
    edited = env.client.post(f"/api/projects/{pid}/edit", headers=headers, json=body)
    assert (
        edited.status_code == 200
        and edited.json()["rule"]["provenance"] == "human_modified"
    )
    assert (
        env.client.post(
            f"/api/projects/{pid}/edit", headers=headers, json=body
        ).status_code
        == 409
    )
    assert env.client.post(f"/api/projects/{pid}/edit", json=body).status_code == 428
    exported = env.client.get(f"/api/projects/{pid}/export?format=json")
    assert (
        exported.status_code == 200
        and exported.json()["rules"][0]["requirement"] == "修改后的精度要求"
    )
    assert env.client.get(f"/api/projects/{pid}/export?format=md").status_code == 200


def test_add_to_empty_library_and_strict_sources(env):
    pid, file, detail = ready(env, empty=True)
    other_pid, other_file, _ = ready(env, display_name="其他规则库")
    headers = {"If-Match": f'"{detail["revision"]}"'}
    body = {"action": "add_rule", "data": draft(file)}
    body["data"]["source_file_ids"] = [other_file.file_id]
    assert (
        env.client.post(
            f"/api/projects/{pid}/edit", headers=headers, json=body
        ).status_code
        == 422
    )
    body["data"]["source_file_ids"] = [file.file_id]
    saved = env.client.post(f"/api/projects/{pid}/edit", headers=headers, json=body)
    assert (
        saved.status_code == 201 and saved.json()["rule"]["provenance"] == "human_added"
    )
    assert (
        env.client.get(
            f"/api/projects/{other_pid}/files/{file.file_id}/download"
        ).status_code
        == 404
    )


def test_concurrent_writes_one_wins(env):
    pid, _, detail = ready(env)

    def write(profile):
        return env.client.post(
            f"/api/projects/{pid}/edit",
            headers={"If-Match": f'"{detail["revision"]}"'},
            json={"action": "update_profile", "data": {"project_profile": profile}},
        ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(write, ["版本一", "版本二"])) == [200, 409]


def test_corrupt_snapshot_keeps_files_and_disables_edit_export(env):
    pid, file, _ = ready(env)
    path = env.settings.storage.data_root / "parsed" / "rules" / f"{pid}.json"
    path.write_text("broken")
    response = env.client.get(f"/api/projects/{pid}")
    assert response.status_code == 200
    assert response.json()["rule_set"] is None
    assert response.json()["snapshot_kind"] == "none"
    assert response.json()["warning"]["code"] == "rule_set_unavailable"
    assert response.json()["files"][0]["file_id"] == file.file_id
    assert (
        env.client.get(f"/api/projects/{pid}/files/{file.file_id}/download").status_code
        == 200
    )
    assert env.client.get(f"/api/projects/{pid}/export?format=md").status_code == 500
    listed = env.client.get("/api/projects?attention_only=true").json()
    assert listed["items"][0]["rule_count"] is None
    assert listed["overview"]["attention_count"] == 1


def test_preview_images_are_version_locked_and_scoped(env):
    pid, file, _ = ready(env)
    root = file_asset_root(env.settings.storage.data_root, file.file_id)
    buffer = BytesIO()
    Image.new("RGB", (20, 20), "blue").save(buffer, format="PNG")
    image = AssetStore(root).save_image(buffer.getvalue())
    parsed = ParsedDocument(
        document=document_ref_from_record(file),
        schema_version=2,
        parse_status="parsed",
        sections=[
            ParsedSection(
                section_id="s1",
                title="示例",
                blocks=[
                    ParsedBlock(kind="image", images=[image], source_block_index=0)
                ],
            )
        ],
    )
    persist_parse_result(parsed)
    preview = env.client.get(f"/api/projects/{pid}/files/{file.file_id}/preview").json()
    image_response = env.client.get(preview["images"][0]["url"])
    assert (
        image_response.status_code == 200
        and image_response.headers["content-type"] == "image/png"
    )
    assert (
        env.client.get(
            f"/api/projects/{pid}/files/{file.file_id}/preview?image_name=not-registered.png&version={preview['parse_version']}"
        ).status_code
        == 404
    )
    assert (
        env.client.get(
            f"/api/projects/{pid}/files/{file.file_id}/preview?image_name=1.png&version=../oops"
        ).status_code
        == 422
    )


def test_trace_cursor_details_redaction_and_restart(env):
    result, _ = upload(env)
    pid = result.json()["project_id"]
    trace = LocalTrace(pid)
    root = trace.start("file", "文件")
    child = trace.start("model", "模型调用", parent=root, input={"model_name": "test"})
    trace.finish(
        child,
        output={
            "content": "data:image/png;base64,verysecret",
            "reasoning_content": "do not expose",
            "api_key": "secret",
        },
    )
    first = env.client.get(
        f"/api/projects/{pid}/progress?include_trace=true&limit=2"
    ).json()["trace"]
    assert first["has_more"] and first["next_cursor"] == 2
    second = env.client.get(
        f"/api/projects/{pid}/progress?include_trace=true&after_seq=2"
    ).json()["trace"]
    assert (
        len(second["changes"]) == 1
        and second["changes"][0]["node"]["status"] == "succeeded"
    )
    detail = env.client.get(f"/api/projects/{pid}/trace/{child}")
    assert "verysecret" not in detail.text and "do not expose" not in detail.text
    mark_interrupted_projects()
    assert (
        env.client.get(f"/api/projects/{pid}").json()["project"]["status"]
        == "rule_failed"
    )
    assert (
        env.client.get(f"/api/projects/{pid}/trace/{root}").json()["node"]["status"]
        == "interrupted"
    )


def test_trace_recording_failure_does_not_raise(env, monkeypatch):
    result, _ = upload(env)
    monkeypatch.setattr(
        "app.rules.local_trace.append_event",
        lambda *args: (_ for _ in ()).throw(OSError("disk unavailable")),
    )
    trace = LocalTrace(result.json()["project_id"])
    span = trace.start("model", "模型调用")
    trace.finish(span, output={"content": "done"})
    assert trace.damaged


@pytest.mark.asyncio
async def test_real_pipeline_records_serial_tools_and_successful_commits(env):
    """走真实上传→解析→索引→Agent→正式库，只有模型与向量计算是受控替身。"""
    import numpy as np
    from langchain_core.messages import AIMessage

    class Model:
        def bind_tools(self, tools):
            return self

        async def ainvoke(self, messages, **kwargs):
            if messages[-1].type == "tool":
                return AIMessage(
                    content="All rules were saved successfully.\n\n处理完毕",
                    response_metadata={"finish_reason": "stop"},
                )
            valid = {
                "operations": [
                    {
                        "action": "add",
                        "category": "technical",
                        "priority": "important",
                        "verification": "bid_only",
                        "requirement": "温度精度至少达到技术表要求",
                        "reason": "依据当前技术表",
                    }
                ],
                "project_profile": "设备招标",
                "return_view": "changed",
            }
            return AIMessage(
                content="核对并保存要求",
                additional_kwargs={"reasoning_content": "不得展示的隐藏推理"},
                tool_calls=[
                    {
                        "id": "bad-1",
                        "type": "tool_call",
                        "name": "submit_rule_delta",
                        "args": {
                            "operations": [],
                            "project_profile": None,
                            "return_view": "changed",
                        },
                    },
                    {
                        "id": "save-1",
                        "type": "tool_call",
                        "name": "submit_rule_delta",
                        "args": valid,
                    },
                    {
                        "id": "read-1",
                        "type": "tool_call",
                        "name": "submit_rule_delta",
                        "args": {
                            "operations": None,
                            "project_profile": None,
                            "return_view": None,
                        },
                    },
                ],
            )

    class Embedding:
        async def embed(self, texts):
            return np.ones(
                (len(texts), env.settings.embedding_model.dimension), dtype=np.float32
            )

    response, _ = upload(env)
    pid = response.json()["project_id"]
    result = await ProjectService(
        env.settings,
        SimpleNamespace(
            rule_extraction=Model(), embedding=Embedding(), rerank=object()
        ),
    ).process_project(pid)
    assert result.status == "rule_ready"
    detail = RuleLibraryService().detail(pid)
    assert detail["available_for_audit"] and len(detail["rule_set"]["rules"]) == 1
    events = RuleLibraryService().trace_page(pid, "rule_ready", 0, 500)
    final = {e["node"]["span_id"]: e["node"] for e in events["changes"]}
    assert {n["kind"] for n in final.values()} == {
        "file",
        "batch",
        "turn",
        "model",
        "tool",
    }
    tools = [n for n in final.values() if n["kind"] == "tool"]
    assert [n["tool_call_id"] for n in tools] == ["bad-1", "save-1", "read-1"]
    assert [n["status"] for n in tools] == ["failed", "succeeded", "succeeded"]
    assert len({n["parent_span_id"] for n in tools}) == 1
    saved = RuleLibraryService().trace_detail(pid, tools[1]["span_id"])
    assert saved["output"]["rule_changes"][0]["before"] is None
    assert (
        saved["output"]["rule_changes"][0]["after"]["requirement"]
        == "温度精度至少达到技术表要求"
    )
    assert saved["output"]["profile_change"] == {"before": "", "after": "设备招标"}
    read = RuleLibraryService().trace_detail(pid, tools[2]["span_id"])
    assert "rule_changes" not in read["output"]
    model_nodes = [n for n in final.values() if n["kind"] == "model"]
    for node in model_nodes:
        model_detail = RuleLibraryService().trace_detail(pid, node["span_id"])
        assert set(model_detail["input"]) == {
            "model_name",
            "tools_enabled",
            "tool_names",
            "platform",
            "thinking_enabled",
            "estimated_input_tokens",
            "estimate_method",
            "context_window_tokens",
            "input_threshold_tokens",
            "project_id",
            "file_id",
            "batch_index",
            "turn_index",
        }
        diagnostics = model_detail["output"]["diagnostics"]
        assert diagnostics["project_id"] == pid
        assert diagnostics["role"] == "rule_model"
        assert diagnostics["generation_limits"]
        assert diagnostics["estimated_input_tokens"] > 0
        assert diagnostics["output_tokens"] is None  # Mock 未提供用量，不填 0。
        assert "不得展示的隐藏推理" not in str(model_detail)
    assert events["trace_status"] == "completed"
