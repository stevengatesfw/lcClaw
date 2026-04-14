# -*- coding: utf-8 -*-
import mimetypes
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from starlette.responses import FileResponse

from ..auth import _get_platform_key, verify_lcagent_token
from ...constant import USERS_DIR

router = APIRouter(prefix="/files", tags=["files"])


def _ensure_workspace_preview_auth(request: Request, user_id: str) -> None:
    """When LCAgent JWT is configured, only the owner may read ``users/{user_id}/...``."""
    if not _get_platform_key():
        return

    uid = getattr(request.state, "user", None)
    if uid is None:
        auth = request.headers.get("Authorization", "")
        uid = verify_lcagent_token(auth) if auth else None
    if uid is None:
        q_token = request.query_params.get("token") or request.query_params.get("_token")
        uid = verify_lcagent_token(q_token or "") if q_token else None
    if uid is None:
        raise HTTPException(status_code=401, detail="Unauthorized")
    if str(uid) != str(user_id):
        raise HTTPException(status_code=403, detail="Forbidden")


@router.api_route(
    "/preview/{user_id}/workspaces/{workspace}/{rel_path:path}",
    methods=["GET", "HEAD"],
    summary="Preview file under users/<user_id>/workspaces/<ws>/ (send_file URLs)",
)
async def preview_workspace_file(
    request: Request,
    user_id: str,
    workspace: str,
    rel_path: str,
):
    """Serve bytes for ``/copaw/api/files/preview/<user_id>/workspaces/<ws>/<rel>``."""
    _ensure_workspace_preview_auth(request, user_id)
    root = (Path(USERS_DIR) / user_id / "workspaces" / workspace).resolve()
    target = (root / rel_path).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise HTTPException(status_code=404, detail="Not found") from None
    if not target.is_file():
        raise HTTPException(status_code=404, detail="Not found")
    media, _ = mimetypes.guess_type(str(target))
    return FileResponse(
        path=str(target),
        media_type=media or "application/octet-stream",
        filename=target.name,
        content_disposition_type="inline",
    )


@router.api_route(
    "/preview/{filepath:path}",
    methods=["GET", "HEAD"],
    summary="Preview file (legacy path)",
)
async def preview_file(
    filepath: str,
):
    """Preview file."""
    path = Path(filepath)
    if not path.is_absolute():
        path = Path("/" + filepath)
    path = path.resolve()
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(path, filename=path.name)
