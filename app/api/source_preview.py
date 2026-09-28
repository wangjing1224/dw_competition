"""正文和图片共用路由，但保留不同响应类型与缓存规则。"""

from fastapi.responses import JSONResponse, Response

from app.core.errors import BusinessError


def preview_response(service, owner_id, file_id, image_name, version):
    if (image_name is None) != (version is None):
        raise BusinessError(
            "invalid_image_parameters", "图片名称和版本必须同时提供", http_status=422
        )
    if image_name is None:
        return JSONResponse(
            service.preview(owner_id, file_id), headers={"Cache-Control": "no-store"}
        )
    if not version:
        raise BusinessError(
            "invalid_parse_version", "解析版本不能为空", http_status=422
        )
    data, mime = service.image(owner_id, file_id, image_name, version)
    return Response(
        data,
        media_type=mime,
        headers={
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, max-age=86400",
            "Content-Disposition": f'inline; filename="{image_name}"',
        },
    )
