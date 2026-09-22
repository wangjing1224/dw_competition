"""图片路径由解析层统一生成，不由 Agent 或 Markdown 消费端补文件前缀。"""

import base64
import hashlib
import io
import json

import pytest
from PIL import Image

from app.models.documents import (
    DocumentRef,
    PageSource,
    ParsedBlock,
    ParsedDocument,
    ParsedImage,
)
from app.parsers import common, service
from app.parsers.assets import AssetStore, build_image_content, file_asset_root
from app.parsers.image_parser import ImageParser


class Repository:
    def update_parse_status(self, file_id, status):
        pass


@pytest.fixture
def parsed_image(tmp_path, temp_settings, monkeypatch):
    for module in (common, service):
        monkeypatch.setattr(module, "get_settings", lambda: temp_settings)
    source = tmp_path / "source.png"
    Image.new("RGB", (30, 20), "red").save(source)
    document = DocumentRef(
        file_id="file-1",
        file_name=source.name,
        source_path=source.name,
        project_id="project-1",
        data_role="rule_source",
    )
    assets = AssetStore(file_asset_root(temp_settings.storage.data_root, "file-1"))
    parsed = ImageParser().parse(document, source, assets=assets)
    return parsed, assets, source


@pytest.mark.parametrize(
    "path",
    [
        "images/1.png",
        "/file-1/images/1.png",
        "../file-1/images/1.png",
        "file-1/images/../../secret.png",
        "file-1//images/1.png",
        "file-1/images/./1.png",
        "file-1/images/1.png/",
        "file-1\\images\\1.png",
        "https://host/file-1/images/1.png",
        "file-1/other/1.png",
    ],
)
def test_noncanonical_paths_rejected_in_images_and_page_sources(path):
    with pytest.raises(ValueError):
        ParsedImage(path=path, mime_type="image/png", width=1, height=1)
    with pytest.raises(ValueError):
        PageSource(page_number=1, image_path=path)


def test_same_number_in_two_files_is_not_the_same_asset(parsed_image):
    parsed, first, _ = parsed_image
    second = AssetStore(first.root.parent / "file-2")
    buffer = io.BytesIO()
    Image.new("RGB", (30, 20), "blue").save(buffer, format="PNG")
    other = second.save_image(buffer.getvalue())
    image = parsed.sections[0].blocks[0].images[0]
    assert image.path == "file-1/images/1.png"
    assert other.path == "file-2/images/1.png"
    assert first.read_image(image) != second.read_image(other)
    content = build_image_content(parsed, image.path, first)
    payload = base64.b64decode(content["image_url"]["url"].split(",", 1)[1])
    with Image.open(io.BytesIO(payload)) as decoded:
        assert decoded.getpixel((0, 0)) == (255, 0, 0)
    with pytest.raises(ValueError, match="file_id"):
        first.read_image(other)
    with pytest.raises(ValueError, match="未登记"):
        build_image_content(parsed, other.path, first)
    payload = parsed.model_dump()
    payload["sections"][0]["blocks"][0]["images"][0]["path"] = other.path
    with pytest.raises(ValueError, match="file_id"):
        ParsedDocument.model_validate(payload)


def test_canonical_paths_survive_all_published_formats(parsed_image):
    parsed, assets, _ = parsed_image
    image = parsed.sections[0].blocks[0].images[0]
    # 表内图走 HTML，回查图走 Markdown 链接；三种输出必须指向同一资产。
    parsed.sections[0].blocks.append(
        ParsedBlock(
            kind="table",
            source_block_index=1,
            content=f'<table><tr><td><img src="{image.path}"></td></tr></table>',
            images=[image],
        )
    )
    parsed.sections[0].blocks.append(
        ParsedBlock(
            kind="table",
            source_block_index=2,
            content="| A | B |\n|---|---|\n| 1 | 2 |",
            images=[image.model_copy(update={"purpose": "reference"})],
            page_sources=[PageSource(page_number=1, image_path=image.path)],
        )
    )
    saved = service.persist_parse_result(parsed, Repository())
    assert saved.parser_version == common.PARSER_VERSION
    snapshot = assets.root.parent / saved.markdown_path
    preview = assets.root.with_suffix(".md")
    markdown = preview.read_text()
    assert markdown == snapshot.read_text() == (assets.root / "document.md").read_text()
    assert "![图片](file-1/images/1.png)" in markdown
    assert 'src="file-1/images/1.png"' in markdown
    assert "[表格原图：" in markdown
    assert "](../../images/" not in markdown and 'src="images/' not in markdown
    assert (preview.parent / image.path).read_bytes() == assets.read_image(image)
    loaded = service.load_parsed_document("file-1")
    assert loaded.model_dump() == saved.model_dump()
    assert loaded.sections[0].blocks[-1].page_sources[0].image_path == image.path
    assert (
        snapshot.with_name("parsed.json").read_bytes()
        == assets.root.with_suffix(".json").read_bytes()
    )
    assert (
        service.persist_parse_result(parsed, Repository()).markdown_path
        == saved.markdown_path
    )


@pytest.mark.parametrize("legacy_name", ["1.png", "hash"])
def test_reparse_reuses_legacy_assets_without_rewriting_snapshots(
    parsed_image, legacy_name
):
    parsed, assets, source = parsed_image
    image = parsed.sections[0].blocks[0].images[0]
    payload = assets.read_image(image)
    digest = hashlib.sha256(payload).hexdigest()
    name = f"{digest}.png" if legacy_name == "hash" else legacy_name
    local_path = f"images/{name}"
    (assets.root / local_path).write_bytes(payload)
    manifest = assets.root / "assets.json"
    manifest.write_text(json.dumps({"last_id": 1, "images": {digest: local_path}}))
    old_version = assets.root / "versions" / "old"
    old_version.mkdir(parents=True)
    old_markdown = old_version / "document.md"
    old_markdown.write_text("![图片](../../images/1.png)")
    old_json = parsed.model_dump()
    old_json["parser_version"] = "2.1"
    old_json["sections"][0]["blocks"][0]["images"][0]["path"] = local_path
    (old_version / "parsed.json").write_text(json.dumps(old_json))
    before = {p: p.read_bytes() for p in old_version.iterdir()}
    reparsed = ImageParser().parse(parsed.document, source, assets=assets)
    saved = service.persist_parse_result(reparsed, Repository())
    new_image = saved.sections[0].blocks[0].images[0]
    assert new_image.path == f"file-1/{local_path}"
    assert assets.read_image(new_image) == payload
    assert json.loads(manifest.read_text()) == {
        "last_id": 1,
        "images": {digest: new_image.path},
    }
    assert all(path.read_bytes() == data for path, data in before.items())
    assert assets.read_image(image) == payload


def test_legacy_image_json_requires_reparse_and_is_not_rewritten(parsed_image):
    parsed, assets, _ = parsed_image
    payload = parsed.model_dump()
    payload["sections"][0]["blocks"][0]["images"][0]["path"] = "images/1.png"
    entry = assets.root.with_suffix(".json")
    entry.write_text(json.dumps(payload))
    before = entry.read_bytes()
    with pytest.raises(ValueError, match="重新解析"):
        service.load_parsed_document("file-1")
    assert entry.read_bytes() == before


def test_publish_revalidates_mutated_paths_before_reading_or_writing(parsed_image):
    parsed, assets, _ = parsed_image
    service.persist_parse_result(parsed, Repository())
    entry = assets.root.with_suffix(".json")
    before = entry.read_bytes()
    block = parsed.sections[0].blocks[0]
    block.images[0] = block.images[0].model_copy(
        update={"path": "file-2/images/1.png"}
    )
    with pytest.raises(ValueError, match="file_id"):
        service.persist_parse_result(parsed, Repository())
    assert entry.read_bytes() == before


def test_file_asset_root_rejects_sibling_symlink(tmp_path):
    root = tmp_path / "parsed"
    (root / "file-1").mkdir(parents=True)
    (root / "file-2").symlink_to(root / "file-1", target_is_directory=True)
    with pytest.raises(ValueError, match="越界"):
        file_asset_root(tmp_path, "file-2")
