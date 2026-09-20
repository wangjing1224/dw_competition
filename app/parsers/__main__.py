"""独立验证公共解析：python -m app.parsers file-42 [--vision]。"""

import argparse
import asyncio

from app.models.documents import document_ref_from_record
from app.parsers.service import prepare_document
from app.storage.repositories import FileRepository


def main():
    parser = argparse.ArgumentParser(
        description="重新解析一个已上传文件；不执行规则抽取或审核"
    )
    parser.add_argument("file_id")
    parser.add_argument(
        "--vision", action="store_true", help="允许调用独立视觉端点（可能计费）"
    )
    args = parser.parse_args()
    document = document_ref_from_record(FileRepository().require(args.file_id))
    result = asyncio.run(prepare_document(document, vision_enabled=args.vision))
    print(
        f"file_id={document.file_id} status={result.parse_status} markdown={result.markdown_path}"
    )
    for warning in result.warnings:
        print(f"warning: {warning}")


if __name__ == "__main__":
    main()
