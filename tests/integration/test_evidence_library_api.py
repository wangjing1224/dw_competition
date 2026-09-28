"""管理接口使用临时 SQLite 和解析资产；不连接真实模型或云端检索。"""

import fcntl
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import ToolMessage
from PIL import Image

from app.api.bidders import router
from app.audit.context import brief_catalog, evidence_view
from app.core.errors import register_exception_handlers
from app.evidence.context import render_material
from app.evidence.local_trace import EvidenceTrace, current_trace, observe_tool_result
from app.evidence.storage import library_path, load_library, write_library
from app.models.documents import (
    ParsedBlock,
    ParsedDocument,
    ParsedSection,
    document_ref_from_record,
)
from app.models.evidence import (
    EvidenceLibrary,
    EvidenceMaterial,
    EvidenceMaterialInput,
    EvidenceSourceFile,
)
from app.parsers.assets import AssetStore, file_asset_root
from app.parsers.service import persist_parse_result
from app.storage.bidder_management import mark_interrupted_bidders, update_management
from app.storage.database import initialize_database
from app.storage.repositories import BidderRepository, FileRepository


@pytest.fixture
def env(monkeypatch, temp_settings):
    for module in (
        "app.storage.file_store",
        "app.parsers.service",
        "app.parsers.common",
        "app.retrieval.store",
        "app.services.evidence_library_service",
    ):
        monkeypatch.setattr(module + ".get_settings", lambda: temp_settings)
    monkeypatch.setattr(
        "app.storage.database.database_path",
        lambda: temp_settings.storage.database_path,
    )
    initialize_database(temp_settings.storage.database_path)
    queued = []
    monkeypatch.setattr(
        "app.api.bidders.background_runner.submit",
        lambda fn, *args: queued.append((fn, args)),
    )
    application = FastAPI()
    register_exception_handlers(application)
    application.include_router(router)
    return SimpleNamespace(
        client=TestClient(application, raise_server_exceptions=False),
        settings=temp_settings,
        queued=queued,
    )


def upload(
    env, *, request_id=None, name="资料测试", path="合同.md", content=b"contract"
):
    request_id = request_id or str(uuid4())
    result = env.client.post(
        "/api/bidders",
        data={
            "display_name": name,
            "relative_paths": [path],
            "client_request_id": request_id,
        },
        files=[("files", ("sample.md", content, "text/markdown"))],
    )
    return result, request_id


def ready(env, *, name="资料测试"):
    response, _ = upload(env, name=name)
    bid = response.json()["bidder_id"]
    file = FileRepository().list_by_scope(bidder_id=bid, data_role="evidence_material")[
        0
    ]
    parsed = ParsedDocument(
        document=document_ref_from_record(file),
        schema_version=2,
        parse_status="parsed",
        sections=[
            ParsedSection(
                section_id="s1",
                title="合同",
                blocks=[
                    ParsedBlock(
                        kind="text", content="合同为20台设备。", source_block_index=0
                    )
                ],
            )
        ],
    )
    persist_parse_result(parsed)
    library = EvidenceLibrary(
        bidder_id=bid,
        source_files=[
            EvidenceSourceFile(
                file_id=file.file_id,
                file_name=file.file_name,
                source_path=file.source_path,
            )
        ],
        materials=[
            EvidenceMaterial(
                file_id=file.file_id,
                bid_summary="合同摘要",
                evidence_summary="合同为20台设备。",
                relations=[],
            )
        ],
    )
    write_library(library, env.settings)
    BidderRepository().update_status(bid, "evidence_ready")
    update_management(bid, phase="completed", trace_status="completed")
    return bid, file, env.client.get(f"/api/bidders/{bid}").json()


def edit(env, bid, revision, **changes):
    material = load_library(bid, env.settings).materials[0]
    data = material.model_dump(exclude={"provenance"})
    data.update(changes)
    return env.client.post(
        f"/api/bidders/{bid}/edit",
        headers={"If-Match": f'"{revision}"'},
        json={"action": "update_material", "data": data},
    )


def test_async_upload_idempotency_and_global_overview(env):
    first, request_id = upload(env)
    assert first.status_code == 202
    assert (
        first.json()["status"] == "processing_evidence" and not first.json()["reused"]
    )
    assert len(env.queued) == 1
    again, _ = upload(env, request_id=request_id)
    assert again.status_code == 200 and again.json()["reused"]
    assert len(env.queued) == 1
    assert upload(env, request_id=request_id, content=b"changed")[0].status_code == 409
    result = env.client.get("/api/bidders?status=evidence_failed").json()
    assert result["total"] == 0 and result["overview"]["processing_count"] == 1
    assert (
        env.client.get(f"/api/bidders?client_request_id={request_id}").json()["total"]
        == 1
    )
    assert (
        env.client.get(f"/api/bidders?client_request_id={request_id}&q=x").status_code
        == 422
    )
    assert (
        env.client.post("/api/bidders", files=[("files", ("a.md", b"x"))]).status_code
        == 422
    )
    bid = first.json()["bidder_id"]
    assert env.client.get(f"/api/bidders/{bid}").json()["library"] is None
    assert env.client.get(f"/api/bidders/{bid}/export").status_code == 409


@pytest.mark.parametrize(
    "status", ["processing_evidence", "evidence_ready", "evidence_failed"]
)
def test_duplicate_evidence_name_creates_nothing(env, status):
    response, request_id = upload(env, name="Test01")
    bid = response.json()["bidder_id"]
    BidderRepository().update_status(bid, status)
    duplicate, _ = upload(env, name="  test01  ")
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "duplicate_library_name"
    assert env.client.get("/api/bidders").json()["total"] == 1
    assert len(env.queued) == 1
    repeated, _ = upload(env, request_id=request_id, name="Test01")
    assert repeated.status_code == 200
    assert repeated.json()["bidder_id"] == bid


def test_same_name_can_be_used_in_different_library_kinds(env):
    from app.storage.project_management import update_management as update_project
    from app.storage.repositories import ProjectRepository

    project = ProjectRepository().create()
    update_project(project.project_id, display_name="资料测试")
    assert upload(env)[0].status_code == 202


def test_concurrent_same_name_evidence_upload_only_creates_one(env):
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: upload(env)[0], range(2)))
    assert sorted(r.status_code for r in results) == [202, 409]
    assert env.client.get("/api/bidders").json()["total"] == 1
    assert len(env.queued) == 1


def test_rename_rejects_duplicate_without_changing_library(env):
    bid, _, detail = ready(env, name="合同资料")
    other, _, _ = ready(env, name="证书资料")
    before = library_path(bid, env.settings).read_bytes()
    response = env.client.post(
        f"/api/bidders/{bid}/edit",
        headers={"If-Match": f'"{detail["revision"]}"'},
        json={"action": "rename", "data": {"display_name": " 证书资料 "}},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "duplicate_library_name"
    assert library_path(bid, env.settings).read_bytes() == before
    assert (
        env.client.get(f"/api/bidders/{bid}").json()["revision"] == detail["revision"]
    )

    # 历史重名仍可读取、无变化保存；不自动重命名或改写资料 JSON。
    update_management(other, display_name="合同资料")
    response = env.client.post(
        f"/api/bidders/{bid}/edit",
        headers={"If-Match": f'"{detail["revision"]}"'},
        json={"action": "rename", "data": {"display_name": "合同资料"}},
    )
    assert response.status_code == 200 and not response.json()["changed"]
    assert (
        env.client.get(f"/api/bidders/{other}").json()["bidder"]["display_name"]
        == "合同资料"
    )


@pytest.mark.parametrize("path,content,code", [("../x", b"x", 400), ("x.md", b"", 422)])
def test_invalid_upload_creates_nothing(env, path, content, code):
    assert upload(env, path=path, content=content)[0].status_code == code
    assert not env.queued
    assert env.client.get("/api/bidders").json()["total"] == 0


def test_same_request_race_never_double_queues(env):
    request_id = str(uuid4())
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(
            pool.map(lambda _: upload(env, request_id=request_id)[0], range(2))
        )
    assert sorted(r.status_code for r in responses) in ([200, 202], [202, 409])
    assert len(env.queued) == 1
    assert env.client.get("/api/bidders").json()["total"] == 1


def test_edit_is_atomic_and_noop_retains_provenance(env, monkeypatch):
    bid, _file, detail = ready(env)
    revision = detail["revision"]
    first = edit(env, bid, revision)
    assert first.status_code == 200 and not first.json()["changed"]
    assert first.json()["revision"] == revision
    result = edit(env, bid, revision, bid_summary="人工纠正合同对象")
    assert result.status_code == 200 and result.json()["changed"]
    saved = load_library(bid, env.settings)
    assert saved.materials[0].provenance == "human_modified"
    assert saved.materials[0].bid_summary == "人工纠正合同对象"
    assert edit(env, bid, revision, bid_summary="旧草稿").status_code == 409
    updated = env.client.get(f"/api/bidders/{bid}").json()
    assert edit(env, bid, updated["revision"]).json()["changed"] is False
    before = library_path(bid, env.settings).read_bytes()

    def fail(*_):
        raise OSError("mock disk failure")

    monkeypatch.setattr("app.evidence.storage.atomic_write", fail)
    assert (
        edit(env, bid, updated["revision"], bid_summary="无法写入").status_code == 500
    )
    assert library_path(bid, env.settings).read_bytes() == before


def test_provenance_not_model_visible_and_not_client_editable(env):
    bid, file, detail = ready(env)
    edit(env, bid, detail["revision"], evidence_summary="人工补充说明")
    library = load_library(bid, env.settings)
    assert "provenance" not in EvidenceMaterialInput.model_fields
    for view in (
        brief_catalog(library),
        render_material(library, file.file_id),
        evidence_view(library, [file.file_id])[2],
    ):
        assert "human_modified" not in view and "provenance" not in view
    assert (
        edit(
            env,
            bid,
            env.client.get(f"/api/bidders/{bid}").json()["revision"],
            provenance="ai_generated",
        ).status_code
        == 422
    )
    exported = env.client.get(f"/api/bidders/{bid}/export")
    assert (
        exported.status_code == 200
        and exported.json()["materials"][0]["provenance"] == "human_modified"
    )
    assert "人工修订" in env.client.get(f"/api/bidders/{bid}/export?format=md").text


def test_rename_and_invalid_relations(env):
    bid, file, detail = ready(env)
    _other, foreign, _ = ready(env, name="其他资料库")
    revision = detail["revision"]
    assert (
        edit(env, bid, revision, relations=[["支持", file.file_id]]).status_code == 422
    )
    assert (
        edit(env, bid, revision, relations=[["支持", foreign.file_id]]).status_code
        == 422
    )
    assert (
        edit(
            env, bid, revision, evidence_summary=f"见{foreign.file_id}/images/1.png"
        ).status_code
        == 422
    )
    response = env.client.post(
        f"/api/bidders/{bid}/edit",
        headers={"If-Match": f'"{revision}"'},
        json={"action": "rename", "data": {"display_name": "新的名称"}},
    )
    assert response.status_code == 200 and response.json()["revision"] != revision
    assert load_library(bid, env.settings).materials[0].provenance == "ai_generated"
    assert (
        env.client.post(
            f"/api/bidders/{bid}/edit",
            json={"action": "rename", "data": {"display_name": "x"}},
        ).status_code
        == 428
    )
    assert (
        env.client.post(
            f"/api/bidders/{bid}/edit",
            headers={"If-Match": "bad"},
            json={"action": "rename", "data": {"display_name": "x"}},
        ).status_code
        == 422
    )


def test_metadata_failure_does_not_disguise_success(env, monkeypatch):
    bid, _file, detail = ready(env)

    def fail(*_, **__):
        raise OSError("metadata failure")

    monkeypatch.setattr("app.services.evidence_library_service.update_management", fail)
    result = edit(env, bid, detail["revision"], bid_summary="已经落盘")
    assert result.status_code == 200 and result.json()["warning"]
    assert load_library(bid, env.settings).materials[0].provenance == "human_modified"
    assert (
        env.client.get(f"/api/bidders/{bid}").json()["revision"]
        == result.json()["revision"]
    )


def test_busy_and_failed_library_readonly(env):
    bid, _file, detail = ready(env)
    with library_path(bid, env.settings).with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert (
            edit(env, bid, detail["revision"], bid_summary="blocked").status_code == 409
        )
    BidderRepository().update_status(
        bid, "evidence_failed", error_code="mock", error_message="mock"
    )
    readonly = env.client.get(f"/api/bidders/{bid}").json()
    assert readonly["snapshot_kind"] == "partial" and readonly["revision"] is None
    assert not readonly["editable"] and readonly["library"]
    assert env.client.get(f"/api/bidders/{bid}/export").status_code == 409


def test_corrupt_snapshot_is_not_empty_library(env):
    bid, _file, _ = ready(env)
    library_path(bid, env.settings).unlink()
    detail = env.client.get(f"/api/bidders/{bid}").json()
    assert (
        detail["library"] is None
        and detail["warning"]["code"] == "evidence_library_unavailable"
    )
    assert not detail["editable"] and detail["files"]
    assert env.client.get("/api/bidders").json()["items"][0]["material_count"] is None
    assert env.client.get(f"/api/bidders/{bid}/export").status_code == 500


def test_preview_and_images_are_scoped_versioned_and_paired(env):
    bid, file, _ = ready(env)
    other, _, _ = ready(env, name="其他资料库")
    buffer = BytesIO()
    Image.new("RGB", (12, 12), "blue").save(buffer, format="PNG")
    image = AssetStore(
        file_asset_root(env.settings.storage.data_root, file.file_id)
    ).save_image(buffer.getvalue())
    persist_parse_result(
        ParsedDocument(
            document=document_ref_from_record(file),
            schema_version=2,
            parse_status="parsed",
            sections=[
                ParsedSection(
                    section_id="s",
                    title="",
                    blocks=[
                        ParsedBlock(kind="image", images=[image], source_block_index=0)
                    ],
                )
            ],
        )
    )
    base = f"/api/bidders/{bid}/files/{file.file_id}/preview"
    response = env.client.get(base)
    assert (
        response.status_code == 200 and response.headers["cache-control"] == "no-store"
    )
    preview = response.json()
    url = preview["images"][0]["url"]
    assert (
        "/preview?image_name=" in url
        and env.client.get(url).content == buffer.getvalue()
    )
    assert env.client.get(url).headers["content-type"] == "image/png"
    for query in (
        "?image_name=1.png",
        "?version=" + "a" * 64,
        "?image_name=&version=" + "a" * 64,
        "?image_name=1.png&version=../bad",
    ):
        assert env.client.get(base + query).status_code == 422
    assert (
        env.client.get(
            base + "?image_name=missing.png&version=" + preview["parse_version"]
        ).status_code
        == 404
    )
    assert (
        env.client.get(base + "?image_name=1.png&version=" + "f" * 64).status_code
        == 404
    )
    assert env.client.get(url.replace(f"/{bid}/", f"/{other}/")).status_code == 404
    assert (
        env.client.get(
            f"/api/bidders/{other}/files/{file.file_id}/download"
        ).status_code
        == 404
    )


def test_trace_cursor_scope_sanitization_and_restart(env):
    bid = upload(env)[0].json()["bidder_id"]
    other = upload(env, name="其他资料库")[0].json()["bidder_id"]
    trace = EvidenceTrace(bid)
    span = trace.start(
        "tool",
        "保存",
        input={
            "messages": ["secret"],
            "api_key": "secret",
            "text": "sk-abcdefghi",
            "image": "data:image/png;base64,abc",
        },
    )
    trace.finish(span, output="已保存")
    running = trace.start("model", "模型调用")
    page = env.client.get(
        f"/api/bidders/{bid}/progress?include_trace=true&limit=1"
    ).json()["trace"]
    assert page["has_more"] and page["next_cursor"] == 1
    tail = env.client.get(
        f"/api/bidders/{bid}/progress?include_trace=true&after_seq=1"
    ).json()["trace"]
    assert len(tail["changes"]) == 2
    detail = env.client.get(f"/api/bidders/{bid}/trace/{span}").json()
    assert "secret" not in str(detail) and "sk-abcdefghi" not in str(detail)
    assert env.client.get(f"/api/bidders/{other}/trace/{span}").status_code == 404
    update_management(bid, phase="ingesting")
    mark_interrupted_bidders()
    data = env.client.get(f"/api/bidders/{bid}/progress?include_trace=true").json()
    assert data["status"] == "evidence_failed" and data["failed_phase"] == "ingesting"
    assert (
        env.client.get(f"/api/bidders/{bid}/trace/{running}").json()["node"]["status"]
        == "interrupted"
    )


def test_observation_storage_failure_is_non_fatal(env, monkeypatch):
    bid = upload(env)[0].json()["bidder_id"]

    def fail(*_):
        raise OSError("trace disk failure")

    monkeypatch.setattr("app.evidence.local_trace.append_event", fail)
    trace = EvidenceTrace(bid)
    trace.finish(trace.start("tool", "test"), output="saved")
    trace.close()
    assert trace.damaged
    assert (
        env.client.get(f"/api/bidders/{bid}/progress?include_trace=true").json()[
            "trace"
        ]["trace_status"]
        == "partial"
    )


def test_failed_receipt_does_not_recreate_or_leak_exception(env, monkeypatch):
    def fail(*_, **__):
        raise OSError("private path /srv/credentials")

    with monkeypatch.context() as patch:
        patch.setattr(BidderRepository, "create", fail)
        first, request_id = upload(env)
    assert first.status_code == 500
    repeated, _ = upload(env, request_id=request_id)
    assert repeated.status_code == 500
    assert repeated.json()["error"]["code"] == "upload_failed"
    assert "/srv/credentials" not in repeated.text
    assert not env.queued
    assert env.client.get("/api/bidders").json()["total"] == 0


def test_failed_file_save_reuses_failed_bidder_without_queue(env, monkeypatch):
    def fail(*_, **__):
        raise OSError("disk failure")

    with monkeypatch.context() as patch:
        patch.setattr("app.storage.file_store.FileStore.save", fail)
        first, request_id = upload(env)
    assert first.status_code == 500
    repeated, _ = upload(env, request_id=request_id)
    assert repeated.status_code == 200 and repeated.json()["reused"]
    assert repeated.json()["status"] == "evidence_failed"
    assert not env.queued
    assert env.client.get("/api/bidders").json()["total"] == 1


@pytest.mark.parametrize("failed", [True, False])
def test_image_trace_projection_does_not_change_tool_result(failed):
    trace = EvidenceTrace("bidder-test")
    token = current_trace.set(trace)
    result = ToolMessage(
        content="图片范围不合法"
        if failed
        else [
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,private"}}
        ],
        status="error" if failed else "success",
        tool_call_id="call-1",
    )
    before = result.model_dump()
    observation = {}
    try:
        observe_tool_result(
            observation,
            SimpleNamespace(history={}, file_id="file-1", current=None),
            {"name": "view_image", "args": {"path": "file-unknown/images/1.png"}},
            result,
            None,
        )
    finally:
        current_trace.reset(token)
    assert result.model_dump() == before
    assert "base64" not in str(observation)
    if failed:
        assert observation["status"] == "failed" and not trace.damaged
    else:
        assert observation["output"] is None and trace.damaged
