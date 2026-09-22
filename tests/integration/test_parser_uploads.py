"""新格式通过存储白名单、注册器和原生解析；不启动业务模型。"""

import zipfile
from io import BytesIO

import pymupdf
import pytest
from PIL import Image

from app.core.errors import BusinessError
from app.models.documents import document_ref_from_record
from app.parsers.service import load_parsed_document, prepare_document
from app.storage.database import initialize_database
from app.storage.file_store import FileOwner, FileStore
from app.storage.repositories import ProjectRepository


@pytest.fixture
def environment(monkeypatch, temp_settings):
    monkeypatch.setattr(
        "app.storage.database.database_path",
        lambda: temp_settings.storage.database_path,
    )
    for name in [
        "app.storage.file_store",
        "app.parsers.common",
        "app.parsers.service",
        "app.parsers.pdf_parser",
    ]:
        monkeypatch.setattr(name + ".get_settings", lambda: temp_settings)
    initialize_database(temp_settings.storage.database_path)
    return temp_settings


@pytest.mark.parametrize("extension", [".pdf", ".png", ".jpg", ".jpeg"])
@pytest.mark.asyncio
async def test_upload_native_parse_and_persist_new_formats(environment, extension):
    if extension == ".pdf":
        with pymupdf.open() as pdf:
            pdf.new_page().insert_text((30, 40), "Native PDF")
            payload = pdf.tobytes()
    else:
        buffer = BytesIO()
        Image.new("RGB", (100, 80), "white").save(
            buffer, format="PNG" if extension == ".png" else "JPEG"
        )
        payload = buffer.getvalue()
    store = FileStore()
    project = ProjectRepository().create()
    record = store.save(
        owner=FileOwner(project_id=project.project_id),
        data_role="rule_source",
        stream=BytesIO(payload),
        file_name="材料" + extension,
        relative_path="目录/材料" + extension,
    )
    result = await prepare_document(
        document_ref_from_record(record), file_store=store, vision_enabled=False
    )
    assert result.schema_version == 2
    assert result.parse_status == ("parsed" if extension == ".pdf" else "ocr_required")
    loaded = load_parsed_document(record.file_id)
    assert loaded.model_dump() == result.model_dump()
    assert store.repository.require(record.file_id).parse_status == result.parse_status
    parsed_root = environment.storage.data_root / "parsed"
    snapshot = parsed_root / result.markdown_path
    preview = parsed_root / f"{record.file_id}.md"
    assert snapshot.read_bytes() == preview.read_bytes()
    for section in loaded.sections:
        for block in section.blocks:
            for image in block.images:
                assert image.path.startswith(f"{record.file_id}/images/")
                assert (preview.parent / image.path).is_file()
                assert image.path in snapshot.read_text()


def test_zip_accepts_nested_paths_for_new_formats_and_rejects_unsupported(
    environment, tmp_path
):
    store = FileStore()
    archive = tmp_path / "sources.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("资料/一级/scan.pdf", b"test")
        zipped.writestr("资料/一级/image.png", b"test")
    files = store.extract_zip_safely(archive, tmp_path / "expanded")
    assert len(files) == 2
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("payload.exe", b"bad")
    with pytest.raises(BusinessError):
        store.extract_zip_safely(archive, tmp_path / "rejected")
