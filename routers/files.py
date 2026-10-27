"""Read-only file browsing for the phone's Files view.

``GET /files/roots`` lists the shared roots by name, ``GET /files/list``
lists one folder, ``GET /files/raw`` serves one file (with Range support,
so videos stream), and ``GET /files/locate`` maps a path seen in a chat to
a root. All rules live in ``services/file_roots.py``; this router only
validates, calls it and translates its errors.
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request
from fastapi.responses import FileResponse, JSONResponse

from config import ANOTHER_BRIDGE_RATE_LIMIT_FILES, ANOTHER_BRIDGE_RATE_LIMIT_FILES_RAW
from services.file_roots import FileBrowser, NotAllowed, NotFound
from services.rate_limiter import limiter

router = APIRouter()

_PATH_MAX = 4096


def _browser(request: Request) -> FileBrowser:
    return getattr(request.app.state, "file_browser", None) or FileBrowser([])


def _error(exc: Exception) -> JSONResponse:
    if isinstance(exc, NotAllowed):
        return JSONResponse({"detail": "that path is not shared", "code": "not_shared"}, status_code=403)
    return JSONResponse({"detail": "not found", "code": "not_found"}, status_code=404)


@router.get("/files/roots")
@limiter.limit(ANOTHER_BRIDGE_RATE_LIMIT_FILES)
def list_roots(request: Request) -> dict:
    return {"roots": _browser(request).roots()}


@router.get("/files/list")
@limiter.limit(ANOTHER_BRIDGE_RATE_LIMIT_FILES)
def list_folder(
    request: Request,
    root: str = Query(min_length=1, max_length=64),
    path: str = Query(default="", max_length=_PATH_MAX),
):
    try:
        return _browser(request).list_dir(root, path)
    except (NotAllowed, NotFound) as exc:
        return _error(exc)


@router.get("/files/raw")
@limiter.limit(ANOTHER_BRIDGE_RATE_LIMIT_FILES_RAW)
def raw_file(
    request: Request,
    root: str = Query(min_length=1, max_length=64),
    path: str = Query(min_length=1, max_length=_PATH_MAX),
):
    try:
        real, media_type = _browser(request).resolve_file(root, path)
    except (NotAllowed, NotFound) as exc:
        return _error(exc)
    return FileResponse(
        real,
        media_type=media_type,
        filename=real.name,
        content_disposition_type="inline",
        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, max-age=300"},
    )


@router.get("/files/locate")
@limiter.limit(ANOTHER_BRIDGE_RATE_LIMIT_FILES)
def locate(request: Request, path: str = Query(min_length=1, max_length=_PATH_MAX)):
    try:
        return _browser(request).locate(path)
    except (NotAllowed, NotFound) as exc:
        return _error(exc)
